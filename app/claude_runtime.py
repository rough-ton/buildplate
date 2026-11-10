"""Design runs through Claude Code (the Claude subscription connection).

Claude Code drives the loop: Buildplate starts `claude -p` with every built-in tool
removed, in an empty working folder, and gives it exactly two MCP tools on a
run-scoped endpoint (/mcp-run): check_design and submit_design. Each run gets its own
bearer token, valid only while the run lasts, so tool calls are tied to the run: they
stream check and preview events to Studio and record the submitted design.

Claude Code's stream-json output is translated into the same events the LiteLLM loop
produces (status, assistant, tool, code, check, preview, usage), so Studio, Usage and
MCP generate_design do not care which runtime ran.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import secrets
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from mcp.server.mcpserver import Context, Image, MCPServer

from . import claude_code, config, db

SERVER_NAME = "buildplate"
TOOL_NAMES = ("check_design", "submit_design")
_DONE = object()


@dataclass
class RunState:
    run_id: str
    vision: bool
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    final: dict[str, Any] | None = None
    last_good: dict[str, Any] | None = None


_RUNS: dict[str, RunState] = {}


def token_valid(token: str) -> bool:
    return bool(token) and token in _RUNS


def _bearer(ctx: Context) -> str:
    req = ctx.request_context.request
    header = req.headers.get("authorization", "") if req is not None else ""
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def _state(ctx: Context) -> RunState:
    run = _RUNS.get(_bearer(ctx))
    if run is None:
        raise ValueError("This design run has ended.")
    return run


# ---- the run-scoped MCP server ---------------------------------------------------------
run_mcp = MCPServer(
    name=SERVER_NAME,
    title="Buildplate design run",
    version=config.VERSION,
    instructions="Render and validate OpenSCAD with check_design, then save the final design with submit_design.",
)


@run_mcp.tool(
    description=(
        "Render OpenSCAD code, validate the resulting mesh for 3D printing (size, watertightness, bed placement, "
        "build volume) and return a report with a preview image (iso, front, right and top views). "
        "Call this before submitting and after every fix."
    ),
    structured_output=False,
)
async def check_design(code: str, ctx: Context) -> list[Any]:
    from . import agent

    run = _state(ctx)
    await run.queue.put({"type": "tool", "name": "check_design"})
    await run.queue.put({"type": "code", "code": code})
    text, check, img = await agent._check(code, run.vision)
    for e in check["events"]:
        await run.queue.put(e)
    if check["ok"]:
        run.last_good = {"code": code, "stats": check["stats"]}
    out: list[Any] = [text]
    if img:
        out.append(Image(data=base64.b64decode(img), format="png"))
    return out


@run_mcp.tool(
    description="Save the final, checked design. Ends the task. The code is checked again and only saved if it renders.",
    structured_output=False,
)
async def submit_design(code: str, title: str, summary: str, ctx: Context, tags: list[str] | None = None) -> list[Any]:
    from . import agent

    run = _state(ctx)
    await run.queue.put({"type": "tool", "name": "submit_design"})
    await run.queue.put({"type": "code", "code": code})
    text, check, _ = await agent._check(code, False)
    for e in check["events"]:
        await run.queue.put(e)
    if not check["ok"]:
        return ["Not saved, fix these problems first.\n" + text]
    run.last_good = {"code": code, "stats": check["stats"]}
    run.final = {"code": code, "title": title, "summary": summary, "tags": list(tags or [])}
    return ["Saved. The task is complete; reply with one short sentence and stop."]


# ---- running Claude Code ------------------------------------------------------------------
def _usage_from(u: dict[str, Any] | None) -> dict[str, Any]:
    u = u or {}
    inp = int(u.get("input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
        "cache_write_tokens": int(u.get("cache_creation_input_tokens") or 0),
        "total_tokens": inp + out,
        "cost_usd": 0.0,  # included in the plan
        "cost_known": True,
    }


def friendly_error(text: str) -> tuple[str, str | None]:
    """Map Claude Code's error text to a message for the user, plus a connection
    state to record (needs_sign_in, expired) when the cause is the sign-in."""
    low = text.lower()
    if "login expired" in low or "token expired" in low or "oauth token has expired" in low:
        return "Your Claude sign-in has expired. Open Settings > AI connections and select Sign in with Claude.", "expired"
    if "not logged in" in low or "please run /login" in low or "invalid api key" in low or "authentication" in low:
        return "Claude Code is not signed in. Open Settings > AI connections and select Sign in with Claude.", "needs_sign_in"
    if "usage limit" in low or "limit reached" in low or "rate limit" in low or "rate_limit" in low or "429" in low:
        return ("Your Claude plan's usage limit has been reached. Wait for it to reset, or pick a model on another "
                "connection for this request."), None
    if "overloaded" in low or "529" in low:
        return "Claude is overloaded right now. Try again in a moment.", None
    return (text.strip() or "Claude Code stopped unexpectedly.")[:800], None


def build_args(binary: str, model: str, system: str, mcp_config_path: str, max_turns: int) -> list[str]:
    allowed = ",".join(f"mcp__{SERVER_NAME}__{t}" for t in TOOL_NAMES)
    return [
        binary, "-p",
        "--model", model,
        "--system-prompt", system,
        "--tools", "",  # no built-in tools: no shell, no file access, no web
        "--restricted",  # no user settings, no command execution, no bypass modes
        "--strict-mcp-config", "--mcp-config", mcp_config_path,
        "--allowedTools", allowed,
        "--disallowedTools", "Bash,Edit,Write,Read,WebFetch,WebSearch,NotebookEdit,Task",
        "--permission-mode", "dontAsk",
        "--max-turns", str(max_turns),
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence",
    ]


SYSTEM_SUFFIX = """

You are running inside Buildplate. Your only tools are check_design and submit_design.
Pass the complete OpenSCAD source every time. Do not ask the user questions; make
sensible assumptions and state them briefly in the summary."""


async def design_loop(
    connection_id: str,
    run_id: str,
    model: str,
    system: str,
    prompt_text: str,
    vision: bool,
    max_iterations: int,
    state: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    """Run Claude Code for one design request, yielding Studio events. On return,
    state["final"], state["last_good"] and state["totals"] hold the outcome; an error
    is raised as claude_code.ClaudeCodeError with a user-facing message."""
    binary = claude_code.binary()
    if binary is None:
        raise claude_code.ClaudeCodeError("Claude Code is not installed. Open Settings > AI connections and install it.")

    token = "bprun_" + secrets.token_urlsafe(32)
    run = RunState(run_id=run_id, vision=vision)
    _RUNS[token] = run
    workdir = tempfile.mkdtemp(prefix="bp-run-", dir=str(config.CACHE_DIR))
    os.chmod(workdir, 0o700)
    mcp_path = os.path.join(workdir, ".mcp-run.json")
    with open(os.open(mcp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        json.dump({"mcpServers": {SERVER_NAME: {
            "type": "http",
            "url": f"http://127.0.0.1:{config.PORT}/mcp-run",
            "headers": {"Authorization": f"Bearer {token}"},
        }}}, fh)

    args = build_args(str(binary), model, system + SYSTEM_SUFFIX, mcp_path, max_iterations + 2)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=claude_code.child_env(connection_id), cwd=workdir, limit=32 * 1024 * 1024,
    )
    totals = state["totals"]
    result: dict[str, Any] = {}
    stderr_tail: list[str] = []
    seen_messages: set[str] = set()
    step = 0

    async def pump_stdout() -> None:
        nonlocal step
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            if kind == "system" and ev.get("subtype") == "init":
                resolved = ev.get("model")
                if resolved and resolved != model:
                    db.set_run_model(run_id, resolved)
                await run.queue.put({"type": "status", "message": f"Claude Code is working ({resolved or model})"})
            elif kind == "assistant":
                msg = ev.get("message") or {}
                for block in msg.get("content") or []:
                    if block.get("type") == "text" and block.get("text", "").strip():
                        await run.queue.put({"type": "assistant", "text": block["text"].strip()})
                mid = msg.get("id")
                if mid and mid not in seen_messages and msg.get("usage"):
                    seen_messages.add(mid)
                    step += 1
                    usage = _usage_from(msg["usage"])
                    tools = [b.get("name", "").removeprefix(f"mcp__{SERVER_NAME}__") for b in msg.get("content") or [] if b.get("type") == "tool_use"]
                    db.add_step(run_id, step, usage, 0, tools)
                    for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens"):
                        totals[k] += usage[k]
                    await run.queue.put({"type": "usage", "step": step, "usage": usage, "totals": dict(totals)})
                    await run.queue.put({"type": "status", "message": f"Thinking (step {step})"})
            elif kind == "result":
                result.update(ev)

    async def pump_stderr() -> None:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            stderr_tail.append(raw.decode(errors="replace").rstrip())
            del stderr_tail[:-40]

    async def supervise() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt_text.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            await asyncio.gather(pump_stdout(), pump_stderr())
            await proc.wait()
        finally:
            await run.queue.put(_DONE)

    task = asyncio.create_task(supervise())
    try:
        while True:
            ev = await run.queue.get()
            if ev is _DONE:
                break
            yield ev
        await task
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        if not task.done():
            task.cancel()
        _RUNS.pop(token, None)
        shutil.rmtree(workdir, ignore_errors=True)

    if result.get("usage"):
        final_usage = _usage_from(result["usage"])
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens"):
            totals[k] = final_usage[k]
    est = float(result.get("total_cost_usd") or 0.0)
    db.set_run_totals(run_id, totals, est, max(step, int(result.get("num_turns") or 0)))
    totals["est_cost_usd"] = round(est, 6)
    state["final"], state["last_good"] = run.final, run.last_good

    if run.final is not None:
        return
    stopped_at_limit = result.get("subtype") == "error_max_turns"
    failed = bool(result.get("is_error")) or (proc.returncode or 0) != 0 or not result
    if run.last_good and (stopped_at_limit or not failed):
        return  # no submit_design: the caller saves the last design that rendered
    text = str(result.get("result") or "") or "\n".join(stderr_tail[-8:])
    message, conn_state = friendly_error(text if failed else "Claude did not produce a design that renders. Try rephrasing the request.")
    err = claude_code.ClaudeCodeError(message)
    err.connection_state = conn_state  # type: ignore[attr-defined]
    raise err
