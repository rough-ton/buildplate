"""GitHub Copilot plan, through GitHub's official Copilot SDK (issue #12).

The user creates a fine-grained personal access token with GitHub's "Copilot requests"
permission (GitHub documents these tokens for the Copilot SDK and CLI) and pastes it into
a connection; it is stored encrypted like any other key. Design requests then run on the
user's own Copilot plan through the SDK, which drives GitHub's Copilot runtime. The SDK
downloads that runtime from GitHub's releases on first use and checks it against GitHub's
published SHA-256 sums; Buildplate keeps it in /data/tools/copilot.

Each run uses the SDK's "empty" mode with an allowlist of only Buildplate's own tools
(check_design, list_parts, submit_design), so Copilot gets no shell, file or web access,
and nothing from the server's environment, custom instructions or other credentials.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
import time
from typing import Any, AsyncIterator

from . import config, db, providers
from . import parts as parts_lib
from .auth_methods import MethodError

MANAGE_URL = "https://github.com/settings/copilot/features"
TOOL_NAMES = ("check_design", "list_parts", "submit_design")
log = logging.getLogger("buildplate.copilot")
TOKEN_PREFIXES = ("github_pat_", "gho_", "ghu_")
RUN_TIMEOUT = 15 * 60  # seconds for one design request
_DONE = object()


class CopilotError(MethodError):
    """A user-facing Copilot problem. connection_state, when set, is saved on the
    connection (for example "error" when GitHub rejects the token)."""

    def __init__(self, message: str, connection_state: str | None = None, manage_url: str | None = None):
        super().__init__(message)
        self.connection_state = connection_state
        self.manage_url = manage_url


# ---- the runtime --------------------------------------------------------------------
def runtime_root() -> str:
    return str(config.DATA_DIR / "tools" / "copilot")


def _use_data_folder() -> None:
    """Keep the SDK's downloaded runtime in the data folder, one folder per version."""
    try:
        from copilot._cli_version import CLI_VERSION  # pinned by the SDK release
    except Exception:  # pragma: no cover - SDK layout changed
        CLI_VERSION = None
    os.environ.setdefault("COPILOT_CLI_EXTRACT_DIR", os.path.join(runtime_root(), CLI_VERSION or "current"))


def child_env(connection_id: str) -> dict[str, str]:
    """A minimal environment for the Copilot runtime: its own home folder and nothing
    inherited, so no other GitHub credential (GH_TOKEN, GITHUB_TOKEN...) can leak in."""
    home = config.DATA_DIR / "copilot" / connection_id
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _new_client(token: str, connection_id: str, workdir: str) -> Any:
    """Create a CopilotClient (downloads the runtime the first time; call in a thread)."""
    _use_data_folder()
    from copilot import CopilotClient

    base = config.DATA_DIR / "copilot" / connection_id / "state"
    base.mkdir(parents=True, exist_ok=True)
    return CopilotClient(
        github_token=token, use_logged_in_user=False, mode="empty", base_directory=str(base),
        working_directory=workdir, env=child_env(connection_id), log_level="error",
    )


async def _client(token: str, connection_id: str, workdir: str) -> Any:
    """A started CopilotClient. One place, so tests can replace it."""
    try:
        client = await asyncio.to_thread(_new_client, token, connection_id, workdir)
        await client.start()
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise CopilotError("The Copilot SDK is not installed in this container.") from exc
    except CopilotError:
        raise
    except Exception as exc:
        raise CopilotError(f"Couldn't start GitHub's Copilot runtime: {exc}") from exc
    return client


def check_token(token: str) -> None:
    token = (token or "").strip()
    if token.startswith("ghp_"):
        raise MethodError(
            "Classic tokens (ghp_) don't work with Copilot. Use Create token on GitHub to make a "
            "fine-grained token with the Copilot requests permission."
        )
    if not token.startswith(TOKEN_PREFIXES):
        raise MethodError("That doesn't look like a GitHub fine-grained token. It should start with github_pat_.")


def validate(values: dict[str, Any]) -> None:
    if values.get("token"):
        check_token(values["token"])


async def list_models(values: dict[str, Any], connection_id: str = "check") -> list[str]:
    """Check the token with GitHub and list the models the user's Copilot plan allows."""
    check_token(values.get("token", ""))
    workdir = tempfile.mkdtemp(prefix="bp-copilot-", dir=str(config.CACHE_DIR))
    client = await _client(values["token"].strip(), connection_id, workdir)
    try:
        status = await client.get_auth_status()
        if not getattr(status, "isAuthenticated", False):
            raise CopilotError(
                "GitHub didn't accept this token. Check it hasn't expired and has the Copilot requests permission.",
                "error",
            )
        models = await client.list_models()
    except CopilotError:
        raise
    except Exception as exc:
        raise _friendly(str(exc)) from exc
    finally:
        with contextlib.suppress(Exception):
            await client.stop()
        shutil.rmtree(workdir, ignore_errors=True)
    ids = [m.id for m in models if getattr(getattr(m, "policy", None), "state", "enabled") == "enabled"]
    if not ids:
        raise CopilotError("Your Copilot plan has no models enabled. Turn some on in your Copilot settings.", None, MANAGE_URL)
    return ids


def _friendly(text: str, status: int | None = None, kind: str = "") -> CopilotError:
    """Turn a Copilot runtime error into a message for Studio and the connection card."""
    low = f"{kind} {text}".lower()
    if status == 401 or "unauthorized" in low or "bad credentials" in low or "not authenticated" in low:
        return CopilotError(
            "GitHub rejected the Copilot token. It may have expired or been revoked. "
            "Edit the connection and paste a new token.",
            "error",
        )
    if status in (402, 429) or "quota" in low or "rate limit" in low or "rate_limit" in low or "premium request" in low:
        return CopilotError(
            "You've reached your Copilot plan's limit for now. Check your usage on GitHub, or pick another model.",
            None, MANAGE_URL,
        )
    if status == 403 or "policy" in low or "not enabled" in low or "not licensed" in low or "no copilot" in low:
        return CopilotError(
            "Your GitHub account can't use this through Copilot. Check you have an active Copilot plan and that "
            "the model is enabled in your Copilot settings.",
            None, MANAGE_URL,
        )
    return CopilotError(f"Copilot stopped: {text}" if text else "Copilot stopped without an answer.")


# ---- one design request ----------------------------------------------------------------
SYSTEM_SUFFIX = """

You are running inside Buildplate. Your only tools are check_design, list_parts and submit_design.
Pass the complete OpenSCAD source every time. Do not ask the user questions; make
sensible assumptions and state them briefly in the summary."""

_SCHEMAS: dict[str, tuple[str, dict[str, Any]]] = {
    "check_design": (
        "Render OpenSCAD code, validate the resulting mesh for 3D printing (size, watertightness, bed placement, "
        "build volume) and return a report with a preview image (iso, front, right and top views). "
        "Call this before submitting and after every fix.",
        {"type": "object", "properties": {"code": {"type": "string", "description": "Complete OpenSCAD source"}}, "required": ["code"]},
    ),
    "list_parts": (
        "Search the user's parts inventory and the hardware catalog (screws, heat-set inserts, nuts, washers, magnets, "
        "bearings, standoffs...). Returns each part's id or key, quantity on hand, and the hole, pocket and nut-trap "
        "sizes to model it with.",
        {"type": "object", "properties": {"query": {"type": "string"}, "category": {"type": "string"}}},
    ),
    "submit_design": (
        "Save the final, checked design. Ends the task. The code is checked again and only saved if it renders. "
        "parts lists every hardware part the design uses as {part: id, key or name, quantity, use}; hardware_note "
        "explains unavoidable hardware when none was asked for.",
        {
            "type": "object",
            "properties": {
                "code": {"type": "string"}, "title": {"type": "string"}, "summary": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "parts": {"type": "array", "items": {"type": "object"}},
                "hardware_note": {"type": "string"},
            },
            "required": ["code", "title", "summary"],
        },
    ),
}


def _usage(data: Any) -> dict[str, Any]:
    """An assistant.usage event in Buildplate's terms. Copilot reports usage the OpenAI
    way: input_tokens is all input, with cache reads and writes as parts of it, which is
    also how Buildplate counts."""
    g = lambda k: int(getattr(data, k, None) or 0)  # noqa: E731
    u = {
        "input_tokens": g("input_tokens"), "output_tokens": g("output_tokens"),
        "cache_read_tokens": g("cache_read_tokens"), "cache_write_tokens": g("cache_write_tokens"),
        "cache_write_1h_tokens": 0,
        "cost_usd": 0.0,  # plan requests cost nothing extra; est_cost_usd holds the API-price value
        "cost_known": True,
    }
    u["total_tokens"] = u["input_tokens"] + u["output_tokens"]
    return u


def estimate(model: str, usage: dict[str, Any]) -> float | None:
    """API list price for the same tokens. Copilot names models like claude-sonnet-4.5;
    LiteLLM's price list uses claude-sonnet-4-5."""
    for name in dict.fromkeys((model, model.replace(".", "-"))):
        est = providers.estimate_cost("github", name, usage)
        if est is not None:
            return est
    return None


async def design_loop(
    connection_id: str,
    token: str,
    run_id: str,
    model: str,
    system: str,
    prompt_text: str,
    vision: bool,
    max_iterations: int,
    state: dict[str, Any],
    hardware_mode: str = "ask",
    images: list[str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run one design request on Copilot, yielding Studio events. On return state["final"]
    and state["last_good"] hold the outcome; problems are raised as CopilotError."""
    from . import agent

    queue: asyncio.Queue = asyncio.Queue()
    out: dict[str, Any] = {"final": None, "last_good": None, "checks": 0, "error": None, "tools": [], "steps": 0, "t0": time.time()}
    totals = state["totals"]

    def put(ev: Any) -> None:
        queue.put_nowait(ev)

    async def check_design(args: dict[str, Any]) -> Any:
        from copilot import ToolBinaryResult, ToolResult

        code = str(args.get("code") or "")
        out["tools"].append("check_design")
        out["checks"] += 1
        put({"type": "tool", "name": "check_design"})
        put({"type": "code", "code": code})
        text, check, img = await agent._check(code, vision)
        for e in check["events"]:
            put(e)
        if check["ok"]:
            out["last_good"] = {"code": code, "stats": check["stats"]}
        if out["checks"] > max_iterations + 1:
            text += "\n\nThat was the last check allowed for this request. Submit the best version now."
        binary = [ToolBinaryResult(data=img, mime_type="image/png", type="image", description="Four-view preview")] if img else None
        return ToolResult(text_result_for_llm=text, binary_results_for_llm=binary)

    async def list_parts(args: dict[str, Any]) -> Any:
        from copilot import ToolResult

        out["tools"].append("list_parts")
        put({"type": "tool", "name": "list_parts"})
        return ToolResult(text_result_for_llm=parts_lib.search_for_agent(str(args.get("query") or ""), str(args.get("category") or "")))

    async def submit_design(args: dict[str, Any]) -> Any:
        from copilot import ToolResult

        code = str(args.get("code") or "")
        out["tools"].append("submit_design")
        put({"type": "tool", "name": "submit_design"})
        put({"type": "code", "code": code})
        text, check, _ = await agent._check(code, False)
        for e in check["events"]:
            put(e)
        if not check["ok"]:
            return ToolResult(text_result_for_llm="Not saved, fix these problems first.\n" + text, result_type="failure")
        out["last_good"] = {"code": code, "stats": check["stats"]}
        problem = parts_lib.check_submission(hardware_mode, args.get("parts") or [], str(args.get("hardware_note") or ""))
        if problem:
            return ToolResult(text_result_for_llm=problem, result_type="failure")
        out["final"] = {
            "code": code, "title": str(args.get("title") or ""), "summary": str(args.get("summary") or ""),
            "tags": list(args.get("tags") or []), "parts": list(args.get("parts") or []),
            "hardware_note": str(args.get("hardware_note") or ""),
        }
        return ToolResult(text_result_for_llm="Saved. The task is complete.")

    handlers = {"check_design": check_design, "list_parts": list_parts, "submit_design": submit_design}

    def make_tool(name: str) -> Any:
        from copilot import Tool, ToolResult

        desc, schema = _SCHEMAS[name]

        async def handler(inv: Any) -> Any:
            try:
                return await handlers[name](dict(getattr(inv, "arguments", None) or {}))
            except Exception as exc:  # report to the model, keep the run going
                return ToolResult(text_result_for_llm=f"Tool error: {exc}", result_type="failure", error=str(exc))

        return Tool(name=name, description=desc, parameters=schema, handler=handler, skip_permission=True,
                    is_terminal=name == "submit_design")

    def on_event(event: Any) -> None:
        try:
            handle_event(event)
        except Exception:  # a bookkeeping problem must never stop the run
            log.exception("Copilot event handling failed")

    def handle_event(event: Any) -> None:
        kind = getattr(getattr(event, "type", None), "value", getattr(event, "type", ""))
        data = getattr(event, "data", None)
        if getattr(event, "agent_id", None):
            return  # sub-agent traffic
        if kind == "assistant.usage" and data is not None:
            usage = _usage(data)
            used_model = getattr(data, "model", None) or model
            usage["est_cost_usd"] = estimate(used_model, usage)
            out["steps"] += 1
            dur = getattr(data, "duration", None)
            ms = int(dur.total_seconds() * 1000) if dur else int((time.time() - out["t0"]) * 1000)
            db.add_step(run_id, out["steps"], usage, ms, list(dict.fromkeys(out["tools"])))
            out["tools"], out["t0"] = [], time.time()
            for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens"):
                totals[k] = totals.get(k, 0) + usage[k]
            if used_model != model:
                db.set_run_model(run_id, used_model)
            put({"type": "usage", "step": out["steps"], "usage": usage, "totals": dict(totals)})
            put({"type": "status", "message": f"Thinking (step {out['steps'] + 1})"})
        elif kind == "assistant.message" and data is not None:
            text = (getattr(data, "content", "") or "").strip()
            if text:
                put({"type": "assistant", "text": text})
        elif kind == "session.error" and data is not None:
            out["error"] = _friendly(getattr(data, "message", "") or "", getattr(data, "status_code", None), getattr(data, "error_type", "") or "")

    def deny(*_a: Any, **_k: Any) -> Any:
        from copilot.generated.rpc import PermissionDecisionReject

        return PermissionDecisionReject(feedback="Only Buildplate's design tools are available.")

    workdir = tempfile.mkdtemp(prefix="bp-run-", dir=str(config.CACHE_DIR))
    os.chmod(workdir, 0o700)
    put({"type": "status", "message": "Starting Copilot"})
    client = None
    session = None
    task: asyncio.Task | None = None
    try:
        client = await _client(token, connection_id, workdir)
        session = await client.create_session(
            on_permission_request=deny, model=model, client_name="Buildplate",
            system_message={"mode": "append", "content": system + SYSTEM_SUFFIX},
            tools=[make_tool(n) for n in TOOL_NAMES], available_tools=[f"custom:{n}" for n in TOOL_NAMES],
            working_directory=workdir, streaming=False,
        )
        session.on(on_event)
        put({"type": "status", "message": f"Copilot is working ({model})"})

        async def drive() -> None:
            try:
                attachments = [{"type": "blob", "data": b, "mimeType": "image/png", "displayName": "reference.png"}
                               for b in images or []]
                extra = {"attachments": attachments} if attachments else {}
                await session.send_and_wait(prompt_text, timeout=RUN_TIMEOUT, **extra)
            finally:
                put(_DONE)

        task = asyncio.create_task(drive())
        while True:
            ev = await queue.get()
            if ev is _DONE:
                break
            yield ev
            if out["checks"] > max_iterations + 2 and out["final"] is None:
                with contextlib.suppress(Exception):
                    await session.abort()  # too many attempts: keep the last design that rendered
        while not queue.empty():
            ev = queue.get_nowait()
            if ev is not _DONE:
                yield ev
        exc = task.exception() if task.done() and not task.cancelled() else None
        if exc is not None and out["final"] is None and not out["last_good"]:
            raise out["error"] or (exc if isinstance(exc, CopilotError) else _friendly(str(exc)))
    finally:
        if task is not None and not task.done():
            task.cancel()
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.abort()
        if session is not None:
            with contextlib.suppress(Exception):
                await session.disconnect()
        if client is not None:
            with contextlib.suppress(Exception):
                await client.stop()
        shutil.rmtree(workdir, ignore_errors=True)
        state["final"], state["last_good"] = out["final"], out["last_good"]

    if out["final"] is None and not out["last_good"] and out["error"] is not None:
        raise out["error"]


def forget(connection_id: str) -> None:
    """Remove the connection's Copilot runtime state (sessions, logs)."""
    shutil.rmtree(config.DATA_DIR / "copilot" / connection_id, ignore_errors=True)
