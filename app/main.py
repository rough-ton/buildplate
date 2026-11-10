"""Buildplate web application: REST API, streaming design agent, MCP endpoint and web UI."""

from __future__ import annotations

import csv
import io
import json
import platform
import re
import contextlib
import shutil
import time
from contextlib import asynccontextmanager
from importlib import metadata
from typing import Any

import asyncio

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from . import agent, auth, auth_methods, claude_code, claude_runtime, config, connections, db, openscad, parts, printers, search, terminal
from . import reference as references
from .mcp_server import mcp
from .settings import public_connection, store
from . import updates

mcp_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
# Run-scoped MCP endpoint for Claude Code design runs (see claude_runtime.py).
run_mcp_app = claude_runtime.run_mcp.streamable_http_app(
    streamable_http_path="/mcp-run",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    db.init()
    store.get()
    async with mcp.session_manager.run(), claude_runtime.run_mcp.session_manager.run():
        yield


app = FastAPI(title="Buildplate", version=config.VERSION, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
for route in [*run_mcp_app.routes, *mcp_app.routes]:
    app.router.routes.append(route)


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/mcp-run"):
        # Only Claude Code runs started by this server, from this server, with their run token.
        if not (auth.is_loopback(request) and claude_runtime.token_valid(auth.bearer(request))):
            return JSONResponse({"error": "Not a valid design run"}, status_code=401)
    elif path.startswith("/mcp"):
        if not auth.mcp_allowed(request):
            return JSONResponse({"error": "Missing or invalid bearer token"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
    elif path.startswith("/api/") and not path.startswith("/api/docs") and path != "/api/openapi.json":
        if not auth.api_allowed(request):
            return JSONResponse({"detail": "Login required"}, status_code=401)
    return await call_next(request)


# ---- models ------------------------------------------------------------------
class LoginBody(BaseModel):
    password: str


class PasswordBody(BaseModel):
    current: str = ""
    new: str = ""


class GenerateBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    design_id: str | None = None
    code: str | None = None
    connection: str | None = None
    model: str | None = None
    provider: str | None = None  # pre-connections: a provider family
    hardware_mode: str | None = None  # ask | inventory | order | none; default from settings
    reference: str | None = None  # id from POST /api/references


class PartBody(BaseModel):
    catalog_key: str = ""
    category: str | None = None
    name: str | None = None
    size: str | None = None
    type: str | None = None
    material: str | None = None
    quantity: int | None = None
    low_stock: int | None = None
    bambu_url: str | None = None
    amazon_url: str | None = None
    other_url: str | None = None
    notes: str | None = None
    dims: dict[str, Any] | None = None
    holes: dict[str, Any] | None = None


class AdjustBody(BaseModel):
    delta: int = Field(ge=-1_000_000, le=1_000_000)


class CsvBody(BaseModel):
    csv: str = Field(max_length=2_000_000)


class RenderBody(BaseModel):
    code: str
    params: dict[str, Any] | None = None
    format: str = "stl"
    view: str = "iso"


class ParamValuesBody(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class CodeBody(BaseModel):
    code: str
    title: str = "Untitled"
    summary: str = ""
    tags: list[str] | None = None


class TagsBody(BaseModel):
    tags: list[str]


class ConnectionBody(BaseModel):
    method: str
    label: str = ""
    values: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ConnectionPatch(BaseModel):
    label: str | None = None
    values: dict[str, Any] | None = None
    enabled: bool | None = None


# ---- auth ----------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {"ok": True, "version": config.VERSION, "openscad": openscad.detect()["available"]}


@app.get("/api/auth/status")
async def auth_status(request: Request):
    enabled = store.password_enabled()
    return {"password_enabled": enabled, "authenticated": (not enabled) or auth.valid_session(request.cookies.get(auth.COOKIE))}


@app.post("/api/auth/login")
async def login(body: LoginBody):
    if not store.check_password(body.password):
        raise HTTPException(401, "Incorrect password")
    value, max_age = auth.make_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.COOKIE, value, max_age=max_age, httponly=True, samesite="lax")
    return resp


@app.post("/api/auth/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


# ---- settings ------------------------------------------------------------------
@app.get("/api/settings")
async def get_settings():
    return store.public()


@app.patch("/api/settings")
async def patch_settings(patch: dict[str, Any]):
    sec = patch.pop("security", None) or {}
    if "session_hours" in sec:
        patch["security"] = {"session_hours": max(1, min(int(sec["session_hours"]), 8760))}
    try:
        return store.update(patch)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/settings/password")
async def set_password(body: PasswordBody):
    if store.password_enabled() and not store.check_password(body.current):
        raise HTTPException(403, "Current password is incorrect")
    if body.new and len(body.new) < 8:
        raise HTTPException(400, "Use at least 8 characters")
    store.set_password(body.new)
    resp = JSONResponse({"ok": True, "password_enabled": bool(body.new)})
    if body.new:
        value, max_age = auth.make_session()
        resp.set_cookie(auth.COOKIE, value, max_age=max_age, httponly=True, samesite="lax")
    return resp


@app.post("/api/settings/mcp-token")
async def new_mcp_token():
    return {"token": store.generate_mcp_token()}


@app.delete("/api/settings/mcp-token")
async def clear_mcp_token():
    store.update({"security": {"mcp_token": ""}})
    return {"ok": True}


# ---- AI connections --------------------------------------------------------------
def _admin(request: Request, method_id: str | None = None, cid: str | None = None) -> None:
    """Gate connection changes. Claude plan connections need a password session."""
    if cid and not method_id:
        try:
            method_id = connections.get(cid)["method"]
        except KeyError:
            raise HTTPException(404, "No connection with that id")
    method = auth_methods.METHODS.get(method_id or "")
    strict = bool(method and method.requires_password)
    if not auth.admin_allowed(request, strict=strict):
        raise HTTPException(403, auth.PASSWORD_REQUIRED if strict else auth.ADMIN_REQUIRED)


def _conn_errors(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError:
        raise HTTPException(404, "No connection with that id")
    except auth_methods.MethodError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/auth-methods")
async def list_auth_methods():
    return {"methods": auth_methods.catalog(), "billing": auth_methods.BILLING}


@app.get("/api/connections")
async def list_connections(request: Request):
    editable = auth.admin_allowed(request)
    return {
        "connections": connections.public_list(),
        "active": store.get()["agent"]["connection"],
        "editable": editable,
        "reason": "" if editable else auth.ADMIN_REQUIRED,
        # Claude plan connections need a signed-in password session
        "password_session": auth.admin_allowed(request, strict=True),
        "password_reason": auth.PASSWORD_REQUIRED,
        "claude_code": claude_code.info(),
    }


@app.post("/api/connections")
async def create_connection(body: ConnectionBody, request: Request):
    _admin(request, method_id=body.method)
    return _conn_errors(connections.create, body.method, body.label, body.values, body.enabled)


@app.patch("/api/connections/{cid}")
async def patch_connection(cid: str, body: ConnectionPatch, request: Request):
    _admin(request, cid=cid)
    return _conn_errors(connections.update, cid, body.label, body.values, body.enabled)


@app.delete("/api/connections/{cid}")
async def delete_connection(cid: str, request: Request):
    _admin(request, cid=cid)
    if connections.get(cid)["method"] == "anthropic.claude_code":
        await claude_code.forget(cid)  # sign out through Claude Code and drop its folder
    if connections.get(cid)["method"] == "openai.chatgpt":
        from . import chatgpt

        chatgpt.forget(cid, keep_registration=False)
    if connections.get(cid)["method"] == "github.copilot":
        from . import copilot

        copilot.forget(cid)
    _conn_errors(connections.delete, cid)
    return {"ok": True}


@app.post("/api/connections/{cid}/test")
async def test_connection(cid: str, request: Request):
    _admin(request, cid=cid)
    try:
        models = await connections.test(cid)
    except KeyError:
        raise HTTPException(404, "No connection with that id")
    except auth_methods.MethodError as exc:
        raise HTTPException(400, str(exc))
    return {"models": models, "count": len(models), "connection": public_connection(connections.get(cid))}


# ---- Claude Code (Claude plan connections) ---------------------------------------------
_install_task: asyncio.Task | None = None


@app.get("/api/claude-code")
async def claude_code_info():
    return claude_code.info()


@app.post("/api/claude-code/install")
async def claude_code_install(request: Request):
    """Start installing the pinned Claude Code release; poll GET /api/claude-code."""
    global _install_task
    _admin(request, method_id="anthropic.claude_code")
    if claude_code.binary() is None and not (_install_task and not _install_task.done()):
        async def go():
            try:
                await claude_code.install()
            except claude_code.ClaudeCodeError:
                pass  # recorded in claude_code.INSTALL["error"]

        claude_code.INSTALL.update(running=True, error="", done=0, total=0)
        _install_task = asyncio.create_task(go())
    return claude_code.info()


@app.post("/api/connections/{cid}/oauth/start")
async def oauth_start(cid: str, request: Request):
    """Start a sign-in that fills the connection's credentials (Sign in with
    OpenRouter). Returns the URL to send the browser to."""
    _admin(request, cid=cid)
    method = auth_methods.METHODS.get(connections.get(cid)["method"])
    if not method or method.oauth != "openrouter":
        raise HTTPException(400, "This connection does not sign in.")
    from . import cloud

    public = store.get()["server"]["public_url"].rstrip("/")
    base = public or str(request.base_url).rstrip("/")
    return {"url": cloud.openrouter_start(cid, f"{base}/api/oauth/openrouter/callback")}


@app.get("/api/oauth/openrouter/callback")
async def oauth_openrouter_callback(request: Request, state: str = "", code: str = ""):
    """OpenRouter sends the browser back here with a code. Exchange it for the key,
    save it on the connection, test it, and return to Settings."""
    from . import cloud

    if not code:
        return RedirectResponse("/#/settings/oauth-cancelled", status_code=303)
    try:
        cid, key = await cloud.openrouter_finish(state, code)
        connections.update(cid, data={"api_key": key})
        await connections.test(cid)
    except (auth_methods.MethodError, KeyError):
        return RedirectResponse("/#/settings/oauth-failed", status_code=303)
    return RedirectResponse("/#/settings/oauth-ok", status_code=303)


class ChatGPTFinishBody(BaseModel):
    url: str = Field(min_length=1, max_length=8000)


@app.post("/api/connections/{cid}/chatgpt/start")
async def chatgpt_start(cid: str, request: Request):
    """Start Sign in with ChatGPT. Returns OpenAI's sign-in URL; the browser comes back
    to a 127.0.0.1 address the user pastes into chatgpt/finish."""
    from . import chatgpt

    _admin(request, cid=cid)
    if connections.get(cid)["method"] != "openai.chatgpt":
        raise HTTPException(400, "This connection does not use Sign in with ChatGPT.")
    return {"url": chatgpt.start(cid), "redirect_uri": chatgpt.REDIRECT_URI}


@app.post("/api/connections/{cid}/chatgpt/finish")
async def chatgpt_finish(cid: str, body: ChatGPTFinishBody, request: Request):
    from . import chatgpt

    _admin(request, cid=cid)
    if connections.get(cid)["method"] != "openai.chatgpt":
        raise HTTPException(400, "This connection does not use Sign in with ChatGPT.")
    try:
        result = await chatgpt.finish(cid, body.url)
    except chatgpt.ChatGPTError as exc:
        raise HTTPException(400, str(exc))
    try:
        await connections.test(cid)
    except auth_methods.MethodError:
        pass  # recorded on the connection
    return {"first_time": result["first_time"], "connection": public_connection(connections.get(cid))}


@app.post("/api/connections/{cid}/sign-out")
async def sign_out_connection(cid: str, request: Request):
    _admin(request, cid=cid)
    if connections.get(cid)["method"] == "openai.chatgpt":
        from . import chatgpt

        chatgpt.forget(cid)
        connections.mark(cid, "needs_sign_in", "Signed out.")
        return {"connection": public_connection(connections.get(cid))}
    if connections.get(cid)["method"] != "anthropic.claude_code":
        raise HTTPException(400, "Only plan connections sign out.")
    try:
        await claude_code.logout(cid)
    except claude_code.ClaudeCodeError as exc:
        raise HTTPException(400, str(exc))
    connections.mark(cid, "needs_sign_in", "Signed out.")
    return {"connection": public_connection(connections.get(cid))}


@app.get("/api/connections/{cid}/plan-usage")
async def plan_usage(cid: str, refresh: bool = False):
    """The Claude plan's usage meters, from Claude Code's own /usage report."""
    try:
        conn = connections.get(cid)
    except KeyError:
        raise HTTPException(404, "No connection with that id")
    if conn["method"] == "openai.chatgpt":
        from . import chatgpt

        # ChatGPT does not share plan meters with apps; OpenAI asks apps to link to its usage page
        return {"report": None, "text": "", "manage_url": chatgpt.MANAGE_USAGE_URL, "fetched_at": time.time(), "plan_limit": None,
                "manage_text": "ChatGPT doesn't share plan limits with apps. View and manage your ChatGPT usage, and Buildplate's limit, in ChatGPT's settings."}
    if conn["method"] == "github.copilot":
        from . import copilot

        # premium-request usage lives on GitHub
        return {"report": None, "text": "", "manage_url": copilot.MANAGE_URL, "fetched_at": time.time(), "plan_limit": None,
                "manage_text": "GitHub doesn't share Copilot limits with apps. See your premium request usage and model settings on GitHub."}
    if conn["method"] != "anthropic.claude_code":
        raise HTTPException(400, "Only plan connections have plan usage.")
    try:
        report = await claude_code.plan_usage(cid, refresh=refresh)
    except claude_code.ClaudeCodeError as exc:
        raise HTTPException(400, str(exc))
    return {**report, "plan_limit": (conn.get("status") or {}).get("plan_limit")}


@app.websocket("/api/connections/{cid}/terminal")
async def connection_terminal(ws: WebSocket, cid: str, action: str = "login"):
    """Claude Code's own sign-in (or sign-out) in a browser terminal. Needs a password
    session and a same-origin browser; runs only `claude auth login|logout`."""
    ok = auth.password_session(ws.cookies) and auth.same_origin(ws.headers)
    try:
        conn = connections.get(cid)
    except KeyError:
        conn = None
    if not ok or conn is None or conn["method"] != "anthropic.claude_code" or action not in terminal.COMMANDS:
        await ws.close(code=4403)
        return
    await ws.accept()
    code = None
    try:
        code = await terminal.session(ws, cid, action)
    finally:
        try:
            await connections.test(cid)
        except auth_methods.MethodError:
            pass
        with contextlib.suppress(Exception):
            status = connections.get(cid)["status"]
            await ws.send_json({"type": "exit", "code": code, "status": status})
            await ws.close()


# ---- printers ------------------------------------------------------------------
class ActivePrinterBody(BaseModel):
    id: str


class CatalogItem(BaseModel):
    key: str
    nozzle: float


class AddPrintersBody(BaseModel):
    items: list[CatalogItem] = Field(min_length=1, max_length=100)
    activate: bool = False


@app.get("/api/printers/catalog")
async def printer_catalog():
    return printers.catalog()


@app.post("/api/printers/add")
async def add_printers(body: AddPrintersBody):
    s = store.get()
    profiles = list(s["printers"])
    have = {p["id"] for p in profiles}
    added = []
    for it in body.items:
        try:
            prof = printers.profile_from_catalog(it.key, it.nozzle)
        except KeyError:
            raise HTTPException(404, f"Unknown printer: {it.key}")
        if prof["id"] not in have:
            profiles.append(prof)
            have.add(prof["id"])
            added.append(prof["id"])
    patch: dict[str, Any] = {"printers": profiles}
    target = added[0] if added else printers.profile_from_catalog(body.items[0].key, body.items[0].nozzle)["id"]
    if body.activate:
        patch["active_printer"] = target
    store.update(patch)
    return {"added": added, "settings": store.public()}


@app.post("/api/printers/active")
async def set_active_printer(body: ActivePrinterBody):
    ids = [p["id"] for p in store.get()["printers"]]
    if body.id not in ids:
        raise HTTPException(404, "No printer profile with that id")
    store.update({"active_printer": body.id})
    return {"printer": store.get()["printer"]}


# ---- system --------------------------------------------------------------------
def _pkg(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return ""


@app.get("/api/system")
async def system():
    cache_bytes = sum(f.stat().st_size for f in config.CACHE_DIR.glob("*") if f.is_file())
    disk = shutil.disk_usage(config.DATA_DIR)
    return {
        "version": config.VERSION,
        "openscad": openscad.detect(),
        "libraries": openscad.list_libraries(),
        "data_dir": str(config.DATA_DIR),
        "user_library_dir": str(config.USER_LIB_DIR),
        "cache_bytes": cache_bytes,
        "disk_free_bytes": disk.free,
        "python": platform.python_version(),
        "packages": {p: _pkg(p) for p in ("litellm", "mcp", "fastapi", "trimesh", "manifold3d")},
        "mcp_path": "/mcp",
    }


@app.get("/api/system/update")
async def update_status():
    """Whether a newer Buildplate release is on GitHub (cached; never fails)."""
    return await updates.status(store.get()["updates"]["check"])


@app.post("/api/system/clear-cache")
async def clear_cache():
    n = 0
    for f in config.CACHE_DIR.glob("*"):
        if f.is_file():
            f.unlink()
            n += 1
    return {"removed": n}


# ---- design agent ----------------------------------------------------------------
@app.post("/api/generate")
async def generate(body: GenerateBody):
    async def stream():
        async for ev in agent.run(
            body.prompt, body.design_id, body.code, connection=body.connection, model=body.model, provider=body.provider,
            hardware_mode=body.hardware_mode, reference=body.reference,
        ):
            yield json.dumps(ev) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/references")
async def upload_reference(request: Request, name: str):
    """Upload a reference file for the next Studio request: the raw file is the request
    body and its name (for the type) is in ?name=. Returns its id and summary."""
    kind = references.kind_of(name)
    if kind is None:
        return JSONResponse({"error": "Attach a picture (PNG, JPG, WebP), an STL, 3MF, SCAD or G-code file."}, status_code=415)
    limit = references.MAX_BYTES[kind]
    if int(request.headers.get("content-length") or 0) > limit:
        return JSONResponse({"error": f"That file is too large (up to {limit / 1024**2:g} MB)."}, status_code=413)
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            return JSONResponse({"error": f"That file is too large (up to {limit / 1024**2:g} MB)."}, status_code=413)
    try:
        return await references.save(name, bytes(data))
    except references.ReferenceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)


@app.get("/api/references/{ref_id}/image")
async def reference_image(ref_id: str):
    p = references.image_path(ref_id)
    if p is None:
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


# ---- rendering (no AI, no tokens) ------------------------------------------------
_CACHE_NAME = re.compile(r"^[0-9a-f]{24}\.(stl|3mf|png|off)$")


@app.post("/api/render")
async def render(body: RenderBody):
    try:
        if body.format == "png":
            res = await openscad.render(body.code, "png", body.params, body.view)
            return {"url": f"/api/cache/{res.path.name}", "warnings": res.warnings}
        if body.format == "3mf":
            p = await openscad.export_3mf(body.code, body.params)
            return {"url": f"/api/cache/{p.name}"}
        res, stats = await openscad.validate(body.code, body.params)
        return {"url": f"/api/cache/{res.path.name}", "stats": stats, "warnings": res.warnings, "log": res.log[-3000:]}
    except openscad.RenderError as exc:
        return JSONResponse({"error": str(exc), "log": exc.log[-4000:]}, status_code=422)


@app.get("/api/cache/{name}")
async def cache_file(name: str):
    if not _CACHE_NAME.match(name):
        raise HTTPException(404)
    p = config.CACHE_DIR / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/parameters")
async def parameters(body: CodeBody):
    return {"params": openscad.parse_parameters(body.code)}


# ---- library -------------------------------------------------------------------
@app.get("/api/designs")
async def designs(
    q: str = "",
    limit: int = 200,
    sort: str = "",
    tags: str = "",
    provider: str = "",
    model: str = "",
    source: str = "",
    printer: str = "",
    min_size: float | None = None,
    max_size: float | None = None,
    after: str = "",
    before: str = "",
):
    try:
        f = search.filters(tags, provider, model, source, printer, min_size, max_size, after, before)
        rows = db.list_designs(limit, q, f, sort)
        for r in rows:
            r["hardware"] = parts.bom_summary(json.loads(r.pop("bom", None) or "null"))
        return {"designs": rows}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/designs/facets")
async def design_facets():
    return db.facets()


@app.post("/api/designs")
async def create_design(body: CodeBody):
    try:
        res, stats = await openscad.validate(body.code)
    except openscad.RenderError as exc:
        raise HTTPException(422, str(exc))
    did = db.create_design(body.title, "manual")
    v = db.add_version(did, body.code, body.summary or "Created manually.", stats, None)
    tags = db.set_tags(did, body.tags) if body.tags else []
    return {"design_id": did, "version": v, "stats": stats, "tags": tags}


@app.get("/api/designs/{design_id}")
async def design(design_id: str):
    d = db.get_design(design_id)
    if not d:
        raise HTTPException(404, "Design not found")
    v = db.get_version(design_id)
    d["code"] = v["scad"] if v else ""
    d["params"] = openscad.parse_parameters(d["code"])
    d["param_values"] = _param_values(v)
    for ver in d["versions"]:
        ver["bom"] = json.loads(ver.get("bom") or "null")
        ver["built"] = json.loads(ver.get("built") or "null")
        ver["param_values"] = _param_values(ver)
    return d


def _param_values(v: dict[str, Any] | None) -> dict[str, Any]:
    """The parameter values tuned in Studio for a version ({} when none)."""
    try:
        out = json.loads((v or {}).get("param_values") or "{}")
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


# ---- parts inventory ---------------------------------------------------------------
def _parts_errors(fn, *args):
    try:
        return fn(*args)
    except KeyError:
        raise HTTPException(404, "No part with that id")
    except parts.PartsError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/parts")
async def list_parts(q: str = "", category: str = ""):
    return {"parts": parts.list_inventory(q, category)}


@app.get("/api/parts/catalog")
async def parts_catalog():
    return parts.catalog()


@app.post("/api/parts")
async def create_part(body: PartBody):
    return _parts_errors(parts.create, body.model_dump(exclude_none=True))


@app.patch("/api/parts/{part_id}")
async def update_part(part_id: str, body: PartBody):
    return _parts_errors(parts.update, part_id, body.model_dump(exclude_unset=True))


@app.post("/api/parts/{part_id}/adjust")
async def adjust_part(part_id: str, body: AdjustBody):
    return _parts_errors(parts.adjust, part_id, body.delta)


@app.delete("/api/parts/{part_id}")
async def delete_part(part_id: str):
    _parts_errors(parts.delete, part_id)
    return {"ok": True}


@app.get("/api/parts/export.csv")
async def export_parts():
    return PlainTextResponse(parts.export_csv(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="buildplate-parts.csv"'})


@app.post("/api/parts/import")
async def import_parts(body: CsvBody):
    return _parts_errors(parts.import_csv, body.csv)


@app.get("/api/designs/{design_id}/bom")
async def design_bom(design_id: str, version: int | None = None):
    """The parts a design version uses, with what is on hand and where to buy the rest."""
    v = db.get_version(design_id, version)
    if not v:
        raise HTTPException(404, "Design not found")
    return {
        "version": v["version"],
        "hardware_mode": v.get("hardware_mode"),
        "hardware_note": v.get("hardware_note") or "",
        "built": json.loads(v.get("built") or "null"),
        "parts": parts.resolve_bom(json.loads(v.get("bom") or "[]")),
    }


@app.post("/api/designs/{design_id}/versions/{version}/built")
async def mark_built(design_id: str, version: int):
    """Take this version's parts out of the inventory."""
    return {"built": _parts_errors(parts.mark_built, design_id, version)}


@app.delete("/api/designs/{design_id}/versions/{version}/built")
async def unmark_built(design_id: str, version: int):
    _parts_errors(parts.unmark_built, design_id, version)
    return {"ok": True}


@app.get("/api/designs/{design_id}/versions/{version}")
async def design_version(design_id: str, version: int):
    v = db.get_version(design_id, version)
    if not v:
        raise HTTPException(404)
    v["params"] = openscad.parse_parameters(v["scad"])
    v["param_values"] = _param_values(v)
    return v


@app.put("/api/designs/{design_id}/versions/{version}/params")
async def save_param_values(design_id: str, version: int, body: ParamValuesBody):
    """Keep the values tuned with the Parameters sliders, so they come back next time."""
    v = db.get_version(design_id, version)
    if not v:
        raise HTTPException(404, "Design not found")
    known = {p["name"] for p in openscad.parse_parameters(v["scad"])}
    values = {}
    for name, val in body.values.items():
        if name not in known:
            continue  # a parameter the code no longer declares
        if not isinstance(val, (bool, int, float, str)) or (isinstance(val, str) and len(val) > 500):
            raise HTTPException(400, f"Unsupported value for {name}")
        values[name] = val
    db.set_param_values(design_id, version, values)
    return {"param_values": values}


@app.post("/api/designs/{design_id}/versions")
async def save_version(design_id: str, body: CodeBody):
    if not db.get_design(design_id):
        raise HTTPException(404)
    try:
        res, stats = await openscad.validate(body.code)
    except openscad.RenderError as exc:
        raise HTTPException(422, str(exc))
    v = db.add_version(design_id, body.code, body.summary or "Edited manually.", stats, None)
    return {"version": v, "stats": stats}


@app.patch("/api/designs/{design_id}")
async def rename(design_id: str, body: dict[str, str]):
    db.rename_design(design_id, body.get("title", "Untitled"))
    return {"ok": True}


@app.get("/api/designs/{design_id}/tags")
async def get_tags(design_id: str):
    if not db.get_design(design_id):
        raise HTTPException(404, "Design not found")
    return {"tags": db.get_tags(design_id)}


def _tag_op(fn, *args):
    try:
        return {"tags": fn(*args)}
    except KeyError:
        raise HTTPException(404, "Design not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.put("/api/designs/{design_id}/tags")
async def put_tags(design_id: str, body: TagsBody):
    return _tag_op(db.set_tags, design_id, body.tags)


@app.post("/api/designs/{design_id}/tags")
async def post_tags(design_id: str, body: TagsBody):
    return _tag_op(db.add_tags, design_id, body.tags)


@app.delete("/api/designs/{design_id}/tags/{tag}")
async def delete_tag(design_id: str, tag: str):
    return _tag_op(db.remove_tag, design_id, tag)


@app.delete("/api/designs/{design_id}")
async def delete(design_id: str):
    db.delete_design(design_id)
    return {"ok": True}


@app.get("/api/designs/{design_id}/download")
async def download(design_id: str, format: str = "stl", version: int | None = None, params: str | None = None):
    d = db.get_design(design_id)
    v = db.get_version(design_id, version)
    if not d or not v:
        raise HTTPException(404)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", d["title"]).strip("-").lower() or "design"
    stem = f"{stem}-v{v['version']}"
    if format == "scad":
        return Response(v["scad"], media_type="text/plain", headers={"Content-Disposition": f'attachment; filename="{stem}.scad"'})
    # Without explicit values, a download matches what Studio shows: the saved tuned values.
    overrides = json.loads(params) if params else (_param_values(v) or None)
    try:
        if format == "3mf":
            p = await openscad.export_3mf(v["scad"], overrides)
        elif format in ("stl", "png"):
            p = (await openscad.render(v["scad"], format, overrides)).path
        else:
            raise HTTPException(400, "format must be stl, 3mf, scad or png")
    except openscad.RenderError as exc:
        raise HTTPException(422, str(exc))
    return FileResponse(p, filename=f"{stem}.{format}")


# ---- usage ---------------------------------------------------------------------
@app.get("/api/usage/runs")
async def runs(limit: int = 50, offset: int = 0):
    rows, total = db.list_runs(min(limit, 500), offset)
    return {"runs": rows, "total": total}


@app.get("/api/usage/runs/{run_id}")
async def run_detail(run_id: str):
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(404)
    return r


@app.get("/api/usage/summary")
async def usage_summary(days: int = 30):
    return db.usage_summary(max(1, min(days, 3650)))


@app.get("/api/usage/export.csv")
async def usage_csv():
    rows = db.all_runs_for_export()
    buf = io.StringIO()
    cols = ["id", "started_at", "source", "connection_id", "auth_method", "billing", "provider", "model", "status",
            "iterations", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd",
            "cost_known", "est_cost_usd", "design_id", "prompt"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="buildplate-usage.csv"'})


# ---- web UI --------------------------------------------------------------------
if config.DOCS_DIR.exists():
    app.mount("/docs-content", StaticFiles(directory=config.DOCS_DIR), name="docs")
app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="ui")


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    run()
