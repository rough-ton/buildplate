"""Buildplate web application: REST API, streaming design agent, MCP endpoint and web UI."""

from __future__ import annotations

import csv
import io
import json
import platform
import re
import shutil
from contextlib import asynccontextmanager
from importlib import metadata
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from . import agent, auth, auth_methods, config, connections, db, openscad, printers
from .mcp_server import mcp
from .settings import public_connection, store

mcp_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    db.init()
    store.get()
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Buildplate", version=config.VERSION, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
for route in mcp_app.routes:
    app.router.routes.append(route)


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/mcp"):
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


class RenderBody(BaseModel):
    code: str
    params: dict[str, Any] | None = None
    format: str = "stl"
    view: str = "iso"


class CodeBody(BaseModel):
    code: str
    title: str = "Untitled"
    summary: str = ""


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
def _admin(request: Request) -> None:
    if not auth.admin_allowed(request):
        raise HTTPException(403, auth.ADMIN_REQUIRED)


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
    }


@app.post("/api/connections")
async def create_connection(body: ConnectionBody, request: Request):
    _admin(request)
    return _conn_errors(connections.create, body.method, body.label, body.values, body.enabled)


@app.patch("/api/connections/{cid}")
async def patch_connection(cid: str, body: ConnectionPatch, request: Request):
    _admin(request)
    return _conn_errors(connections.update, cid, body.label, body.values, body.enabled)


@app.delete("/api/connections/{cid}")
async def delete_connection(cid: str, request: Request):
    _admin(request)
    _conn_errors(connections.delete, cid)
    return {"ok": True}


@app.post("/api/connections/{cid}/test")
async def test_connection(cid: str, request: Request):
    _admin(request)
    try:
        models = await connections.test(cid)
    except KeyError:
        raise HTTPException(404, "No connection with that id")
    except auth_methods.MethodError as exc:
        raise HTTPException(400, str(exc))
    return {"models": models, "count": len(models), "connection": public_connection(connections.get(cid))}


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
            body.prompt, body.design_id, body.code, connection=body.connection, model=body.model, provider=body.provider
        ):
            yield json.dumps(ev) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
async def designs(q: str = "", limit: int = 200):
    return {"designs": db.list_designs(limit, q)}


@app.post("/api/designs")
async def create_design(body: CodeBody):
    try:
        res, stats = await openscad.validate(body.code)
    except openscad.RenderError as exc:
        raise HTTPException(422, str(exc))
    did = db.create_design(body.title, "manual")
    v = db.add_version(did, body.code, body.summary or "Created manually.", stats, None)
    return {"design_id": did, "version": v, "stats": stats}


@app.get("/api/designs/{design_id}")
async def design(design_id: str):
    d = db.get_design(design_id)
    if not d:
        raise HTTPException(404, "Design not found")
    v = db.get_version(design_id)
    d["code"] = v["scad"] if v else ""
    d["params"] = openscad.parse_parameters(d["code"])
    return d


@app.get("/api/designs/{design_id}/versions/{version}")
async def design_version(design_id: str, version: int):
    v = db.get_version(design_id, version)
    if not v:
        raise HTTPException(404)
    v["params"] = openscad.parse_parameters(v["scad"])
    return v


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
    overrides = json.loads(params) if params else None
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
    cols = ["id", "started_at", "source", "provider", "model", "status", "iterations", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens", "cost_usd", "design_id", "prompt"]
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
