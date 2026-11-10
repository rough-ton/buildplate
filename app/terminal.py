"""Browser terminal for Claude Code's own sign-in.

The WebSocket relays keystrokes to a pseudo-terminal that runs exactly one allowlisted
command: `claude auth login` or `claude auth logout`. It never runs a shell or the
interactive `claude` REPL (whose `!` prefix runs shell commands). The user follows
Claude Code's sign-in link, approves in their own browser, and pastes the code back
into Claude Code's prompt. Buildplate relays the bytes and stores nothing.

Protocol (JSON text frames):
  client -> server  {"type": "input", "data": "..."} | {"type": "resize", "cols": n, "rows": n}
  server -> client  {"type": "output", "data": "..."} | {"type": "error", "message": "..."}
The caller sends the final {"type": "exit", ...} frame once it has refreshed the status.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import json
import os
import pty
import struct
import termios
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from . import claude_code

COMMANDS = {"login": ("auth", "login"), "logout": ("auth", "logout")}
IDLE_TIMEOUT = 600  # seconds without input or output
MAX_SESSION = 1800


def _set_size(fd: int, cols: int, rows: int) -> None:
    cols = max(20, min(int(cols), 400))
    rows = max(5, min(int(rows), 200))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def session(ws: WebSocket, connection_id: str, action: str) -> int | None:
    """Run one allowlisted Claude Code command in a PTY and relay it over ws. Returns
    the command's exit code, or None if the session ended another way. The caller has
    already accepted and authorised the WebSocket."""
    if action not in COMMANDS:
        await ws.send_json({"type": "error", "message": "Unknown action."})
        return None
    binary = claude_code.binary()
    if binary is None:
        await ws.send_json({"type": "error", "message": "Claude Code is not installed yet."})
        return None

    master, slave = pty.openpty()
    _set_size(master, 100, 30)
    env = claude_code.child_env(connection_id, {"TERM": "xterm-256color", "COLUMNS": "100", "LINES": "30"})
    proc = await asyncio.create_subprocess_exec(
        str(binary), *COMMANDS[action],
        stdin=slave, stdout=slave, stderr=slave,
        env=env, cwd=str(claude_code.config_dir(connection_id)), start_new_session=True,
    )
    os.close(slave)
    os.set_blocking(master, False)
    loop = asyncio.get_running_loop()
    out_q: asyncio.Queue[bytes | None] = asyncio.Queue()
    last_activity = loop.time()
    started = loop.time()

    def on_readable() -> None:
        try:
            data = os.read(master, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(master)
            out_q.put_nowait(None)
        else:
            out_q.put_nowait(data)

    loop.add_reader(master, on_readable)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    async def pump_out() -> None:
        nonlocal last_activity
        while True:
            data = await out_q.get()
            if data is None:
                return
            last_activity = loop.time()
            text = decoder.decode(data)
            if text:
                await ws.send_json({"type": "output", "data": text})

    async def pump_in() -> None:
        nonlocal last_activity
        while True:
            msg: Any = json.loads(await ws.receive_text())
            last_activity = loop.time()
            if msg.get("type") == "input" and isinstance(msg.get("data"), str):
                os.write(master, msg["data"].encode()[:4096])
            elif msg.get("type") == "resize":
                _set_size(master, msg.get("cols", 100), msg.get("rows", 30))

    async def watchdog() -> None:
        while True:
            await asyncio.sleep(5)
            now = loop.time()
            if now - last_activity > IDLE_TIMEOUT or now - started > MAX_SESSION:
                await ws.send_json({"type": "error", "message": "The sign-in session timed out. Start it again."})
                return

    tasks = [asyncio.create_task(t) for t in (pump_out(), pump_in(), watchdog(), proc.wait())]
    code: int | None = None
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if tasks[0] in done or tasks[3] in done:
            # the command finished: flush remaining output, then report its exit code
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(tasks[0]), 2)
            code = await asyncio.wait_for(proc.wait(), 5)
    except (WebSocketDisconnect, RuntimeError, asyncio.TimeoutError):
        pass
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            loop.remove_reader(master)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
        with contextlib.suppress(OSError):
            os.close(master)
    return code
