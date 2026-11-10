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
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from mcp.server.mcpserver import Context, Image, MCPServer

from . import claude_code, config, db, providers
from . import parts as parts_lib

SERVER_NAME = "buildplate"
TOOL_NAMES = ("check_design", "submit_design", "list_parts")
_DONE = object()


@dataclass
class RunState:
    run_id: str
    vision: bool
    hardware_mode: str = "ask"
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
    description=(
        "Search the user's parts inventory and the hardware catalog (screws, heat-set inserts, nuts, washers, magnets, "
        "bearings, standoffs...). Returns each part's id or key, quantity on hand, and the hole, pocket and nut-trap "
        "sizes to model it with."
    ),
    structured_output=False,
)
async def list_parts(ctx: Context, query: str = "", category: str = "") -> list[Any]:
    run = _state(ctx)
    await run.queue.put({"type": "tool", "name": "list_parts"})
    return [parts_lib.search_for_agent(query, category)]


@run_mcp.tool(
    description=(
        "Save the final, checked design. Ends the task. The code is checked again and only saved if it renders. "
        "parts lists every hardware part the design uses as {part: id, key or name, quantity, use}; hardware_note "
        "explains unavoidable hardware when none was asked for."
    ),
    structured_output=False,
)
async def submit_design(code: str, title: str, summary: str, ctx: Context, tags: list[str] | None = None,
                        parts: list[dict[str, Any]] | None = None, hardware_note: str = "") -> list[Any]:
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
    problem = parts_lib.check_submission(run.hardware_mode, parts or [], hardware_note)
    if problem:
        return [problem]
    run.final = {"code": code, "title": title, "summary": summary, "tags": list(tags or []),
                 "parts": list(parts or []), "hardware_note": hardware_note}
    return ["Saved. The task is complete; reply with one short sentence and stop."]


# ---- running Claude Code ------------------------------------------------------------------
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cache_write_1h_tokens", "total_tokens")


def _usage_from(u: dict[str, Any] | None) -> dict[str, Any]:
    """Anthropic API usage in Buildplate's terms. Anthropic's input_tokens excludes
    cached tokens; Buildplate's input_tokens is all input (as LiteLLM reports it), with
    cache reads and writes as subsets of it."""
    u = u or {}
    uncached = int(u.get("input_tokens") or 0)
    read = int(u.get("cache_read_input_tokens") or 0)
    write = int(u.get("cache_creation_input_tokens") or 0)
    write_1h = int((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    return {
        "input_tokens": uncached + read + write,
        "output_tokens": out,
        "cache_read_tokens": read,
        "cache_write_tokens": write,
        "cache_write_1h_tokens": write_1h,
        "total_tokens": uncached + read + write + out,
        "cost_usd": 0.0,  # included in the plan
        "cost_known": True,
    }


def _model_usage_totals(model_usage: dict[str, Any]) -> tuple[dict[str, Any], float | None]:
    """Whole-run totals from the result's modelUsage (covers every model the run used).
    Returns (usage, estimated API cost); the cost is None if any model was unpriced."""
    tot = {k: 0 for k in TOKEN_KEYS}
    cost: float | None = 0.0
    for mu in model_usage.values():
        read = int(mu.get("cacheReadInputTokens") or 0)
        write = int(mu.get("cacheCreationInputTokens") or 0)
        inp = int(mu.get("inputTokens") or 0) + read + write
        out = int(mu.get("outputTokens") or 0)
        tot["input_tokens"] += inp
        tot["output_tokens"] += out
        tot["cache_read_tokens"] += read
        tot["cache_write_tokens"] += write
        tot["total_tokens"] += inp + out
        if mu.get("costBasis") == "unknown" or mu.get("costUSD") is None:
            cost = None
        elif cost is not None:
            cost += float(mu["costUSD"])
    return tot, cost


def _plan_limit_message(info: dict[str, Any]) -> str:
    when = ""
    if info.get("resetsAt"):
        when = " It resets at " + time.strftime("%H:%M on %d %b", time.localtime(float(info["resetsAt"]))) + "."
    if info.get("errorCode") == "credits_required":
        return ("Your Claude plan's included usage is used up." + when +
                " You can buy usage credits in Claude, or pick a model on another connection for this request.")
    return "Your Claude plan's usage limit has been reached." + when + " Wait for it to reset, or pick a model on another connection for this request."


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
        "--include-partial-messages",  # per-step output tokens arrive in message_delta
        "--no-session-persistence",
    ]


SYSTEM_SUFFIX = """

You are running inside Buildplate. Your only tools are check_design, list_parts and submit_design.
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
    hardware_mode: str = "ask",
    images: list[str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run Claude Code for one design request, yielding Studio events. On return,
    state["final"], state["last_good"] and state["totals"] hold the outcome; an error
    is raised as claude_code.ClaudeCodeError with a user-facing message. images are
    base64 PNGs sent with the prompt (a reference file's picture)."""
    binary = claude_code.binary()
    if binary is None:
        raise claude_code.ClaudeCodeError("Claude Code is not installed. Open Settings > AI connections and install it.")

    token = "bprun_" + secrets.token_urlsafe(32)
    run = RunState(run_id=run_id, vision=vision, hardware_mode=hardware_mode)
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
    if images:
        # a plain-text prompt cannot carry a picture: send one stream-json user message instead
        args.insert(args.index("--output-format"), "--input-format")
        args.insert(args.index("--output-format"), "stream-json")
        stdin_data = json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": prompt_text},
            *({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b}} for b in images),
        ]}}) + "\n"
    else:
        stdin_data = prompt_text
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=claude_code.child_env(connection_id), cwd=workdir, limit=32 * 1024 * 1024,
    )
    totals = state["totals"]
    for k in TOKEN_KEYS:
        totals.setdefault(k, 0)
    result: dict[str, Any] = {}
    stderr_tail: list[str] = []
    resolved_model = model
    # One entry per API response (message id), in order. Claude Code's assistant
    # messages carry a placeholder output count, so a step's usage comes from the
    # API stream: message_start for input and cache, message_delta for the real output.
    steps: dict[str, dict[str, Any]] = {}
    current: str | None = None
    rate_limit: dict[str, Any] = {}

    def step_entry(mid: str) -> dict[str, Any]:
        return steps.setdefault(mid, {"usage": {}, "tools": [], "t0": time.time(), "t1": None, "recorded": False, "final": False})

    def record_step(mid: str) -> dict[str, Any] | None:
        st = steps[mid]
        if st["recorded"]:
            return None
        st["recorded"] = True
        usage = _usage_from(st["usage"])
        usage["est_cost_usd"] = providers.estimate_cost("anthropic", resolved_model, usage)
        n = sum(1 for x in steps.values() if x["recorded"])
        db.add_step(run_id, n, usage, int(((st["t1"] or time.time()) - st["t0"]) * 1000), st["tools"])
        for k in TOKEN_KEYS:
            totals[k] += usage[k]
        return usage

    async def pump_stdout() -> None:
        nonlocal current, resolved_model
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
            if ev.get("parent_tool_use_id"):
                continue  # subagent traffic; whole-run totals come from modelUsage
            if kind == "system" and ev.get("subtype") == "init":
                resolved_model = ev.get("model") or model
                if resolved_model != model:
                    db.set_run_model(run_id, resolved_model)
                await run.queue.put({"type": "status", "message": f"Claude Code is working ({resolved_model})"})
            elif kind == "stream_event":
                e = ev.get("event") or {}
                etype = e.get("type")
                if etype == "message_start":
                    # the previous response is complete, tools included
                    for mid in list(steps):
                        if steps[mid]["final"]:
                            record_step(mid)
                    msg = e.get("message") or {}
                    current = msg.get("id")
                    if current:
                        st = step_entry(current)
                        st["usage"] = dict(msg.get("usage") or {})
                        st["t0"] = time.time()
                        await run.queue.put({"type": "status", "message": f"Thinking (step {len(steps)})"})
                elif etype == "message_delta" and current:
                    st = steps[current]
                    st["usage"].update({k: v for k, v in (e.get("usage") or {}).items() if v is not None})
                    st["final"], st["t1"] = True, time.time()
                    usage = _usage_from(st["usage"])
                    live = {k: totals[k] + usage[k] for k in TOKEN_KEYS}
                    await run.queue.put({"type": "usage", "step": len(steps), "usage": usage, "totals": {**totals, **live}})
            elif kind == "assistant":
                msg = ev.get("message") or {}
                mid = msg.get("id")
                for block in msg.get("content") or []:
                    if block.get("type") == "text" and block.get("text", "").strip():
                        await run.queue.put({"type": "assistant", "text": block["text"].strip()})
                if mid:
                    st = step_entry(mid)
                    if not st["usage"] and msg.get("usage"):
                        st["usage"] = dict(msg["usage"])  # no stream events: best available
                        st["final"] = True
                    for b in msg.get("content") or []:
                        if b.get("type") == "tool_use":
                            name = b.get("name", "").removeprefix(f"mcp__{SERVER_NAME}__")
                            if name not in st["tools"]:
                                st["tools"].append(name)
            elif kind == "rate_limit_event":
                rate_limit.update(ev.get("rate_limit_info") or {})
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
            proc.stdin.write(stdin_data.encode())
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
        # Always save what the run used, even when it was stopped or crashed.
        _save_totals(run_id, steps, record_step, result, totals, resolved_model, rate_limit, connection_id)
    state["final"], state["last_good"] = run.final, run.last_good

    if run.final is not None:
        return
    stopped_at_limit = result.get("subtype") == "error_max_turns"
    failed = bool(result.get("is_error")) or (proc.returncode or 0) != 0 or not result
    if run.last_good and (stopped_at_limit or not failed):
        return  # no submit_design: the caller saves the last design that rendered
    text = str(result.get("result") or "") or "\n".join(stderr_tail[-8:])
    if rate_limit.get("status") == "rejected":
        message, conn_state = _plan_limit_message(rate_limit), None
    else:
        message, conn_state = friendly_error(text if failed else "Claude did not produce a design that renders. Try rephrasing the request.")
    err = claude_code.ClaudeCodeError(message)
    err.connection_state = conn_state  # type: ignore[attr-defined]
    raise err


def _save_totals(run_id: str, steps: dict[str, Any], record_step, result: dict[str, Any], totals: dict[str, Any],
                 model: str, rate_limit: dict[str, Any], connection_id: str) -> None:
    """Record any steps not yet written, then the run's totals. Totals come from the
    result's modelUsage (whole run, every model), else its usage, else the sum of the
    steps (a stopped or crashed run, whose result may be missing or zeroed). The API
    cost estimate is Claude Code's own (it knows each request's cache TTL), unless a
    pricing override applies or a model was unpriced."""
    for mid in list(steps):
        record_step(mid)
    est: float | None = None
    source = None
    if result.get("modelUsage"):
        usage, est = _model_usage_totals(result["modelUsage"])
        source = usage if usage["total_tokens"] else None
    if source is None and result.get("usage"):
        usage = _usage_from(result["usage"])
        if usage["total_tokens"]:
            source = usage
            est = float(result["total_cost_usd"]) if result.get("total_cost_usd") else None
    if source is not None:
        source["cache_write_1h_tokens"] = totals.get("cache_write_1h_tokens", 0)
        for k in TOKEN_KEYS:
            totals[k] = source[k]
    override = store_override(model)
    if est is None or override:
        est = providers.estimate_cost("anthropic", model, totals)
    totals["est_cost_usd"] = est
    db.set_run_totals(run_id, totals, est, sum(1 for x in steps.values() if x["recorded"]))
    if rate_limit:
        from . import connections

        connections.set_plan_limit(connection_id, rate_limit)


def store_override(model: str) -> bool:
    from .settings import store

    return bool(store.get()["pricing"]["overrides"].get(f"anthropic/{model}"))

