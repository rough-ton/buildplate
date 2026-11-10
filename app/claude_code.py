"""Claude Code CLI management for the Claude subscription connection.

Buildplate runs the unmodified Claude Code binary as published by Anthropic. The user
signs in through Claude Code's own login (see terminal.py); Claude Code keeps its
credentials in its own configuration folder. Buildplate never reads, copies or stores
them: it only asks the CLI for its status with `claude auth status`.

The binary is installed on demand: a pinned version is downloaded from Anthropic's
release bucket into /data/tools, checked against the SHA-256 in the release manifest,
and never modified. Set BUILDPLATE_CLAUDE_BIN to use a binary installed some other way
(for example baked into the image), or BUILDPLATE_CLAUDE_CODE_VERSION to pin another
version.

Each subscription connection gets its own Claude Code configuration folder
(CLAUDE_CONFIG_DIR), so several Claude accounts can be signed in side by side.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform as _platform
import re
import shutil
import stat
from pathlib import Path
from typing import Any

import httpx

from . import config

PINNED_VERSION = "2.1.311"
RELEASES = "https://downloads.claude.ai/claude-code-releases"
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[A-Za-z0-9.]+)?$")

# Claude Code resolves these aliases to the newest model of each tier it supports,
# so the list stays valid as the pinned CLI is updated. A full model id can also be
# entered as a custom model.
MODELS = ["sonnet", "opus", "haiku", "fable"]

# Variables passed through to Claude Code. Everything else is dropped, so a stray
# ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, apiKeyHelper or
# cloud-provider flag in Buildplate's environment can never silently switch billing.
_PASS_ENV = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "TZ")


class ClaudeCodeError(Exception):
    """Shown to the user as is."""


def version() -> str:
    v = os.environ.get("BUILDPLATE_CLAUDE_CODE_VERSION", "").strip() or PINNED_VERSION
    if not _VERSION_RE.match(v):
        raise ClaudeCodeError(f"BUILDPLATE_CLAUDE_CODE_VERSION '{v}' is not a version number.")
    return v


def tools_dir() -> Path:
    return config.DATA_DIR / "tools" / "claude-code"


def profiles_dir() -> Path:
    return config.DATA_DIR / "claude-code"


def config_dir(connection_id: str) -> Path:
    """Claude Code's configuration folder for one connection (its CLAUDE_CONFIG_DIR)."""
    if not re.fullmatch(r"c_[A-Za-z0-9_]{1,40}", connection_id):
        raise ClaudeCodeError("Invalid connection id.")
    return profiles_dir() / connection_id


def _managed_binary() -> Path:
    return tools_dir() / version() / "claude"


BAKED_BINARY = Path("/opt/claude-code/claude")  # docker build --build-arg CLAUDE_CODE_VERSION=...


def _external_binary() -> Path | None:
    """A binary Buildplate does not manage: BUILDPLATE_CLAUDE_BIN, or one baked into the image."""
    override = os.environ.get("BUILDPLATE_CLAUDE_BIN", "").strip()
    if override:
        return Path(override)
    return BAKED_BINARY if BAKED_BINARY.is_file() else None


def binary() -> Path | None:
    """The Claude Code binary to run, or None when it is not installed."""
    p = _external_binary() or _managed_binary()
    return p if p.is_file() and os.access(p, os.X_OK) else None


def platform_id() -> str:
    """Anthropic's platform key: linux-x64, linux-arm64 (with -musl on musl libc), darwin-*."""
    system = _platform.system().lower()
    machine = _platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if system not in ("linux", "darwin") or arch is None:
        raise ClaudeCodeError(f"Claude Code is not available for {system}/{machine}.")
    if system == "linux" and (Path("/lib/libc.musl-x86_64.so.1").exists() or Path("/lib/libc.musl-aarch64.so.1").exists()):
        return f"linux-{arch}-musl"
    return f"{system}-{arch}"


def child_env(connection_id: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """A minimal environment for Claude Code: its own config folder, no inherited
    credentials, and no self-updates (the pinned version stays in place)."""
    home = os.environ.get("HOME") or str(config.DATA_DIR / ".home")
    cdir = config_dir(connection_id)
    cdir.mkdir(parents=True, exist_ok=True)
    os.chmod(cdir, 0o700)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "LANG": "C.UTF-8",
        "CLAUDE_CONFIG_DIR": str(cdir),
        "DISABLE_UPDATES": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    env.update({k: os.environ[k] for k in _PASS_ENV if os.environ.get(k)})
    # The run's MCP server is on loopback; never send that through a proxy.
    for key in ("NO_PROXY", "no_proxy"):
        if key in env:
            env[key] = env[key] + ",127.0.0.1,localhost"
    env.update(extra or {})
    return env


# ---- installation --------------------------------------------------------------
INSTALL: dict[str, Any] = {"running": False, "done": 0, "total": 0, "error": "", "version": ""}
_install_lock = asyncio.Lock()


def info() -> dict[str, Any]:
    b = binary()
    try:
        plat = platform_id()
    except ClaudeCodeError as exc:
        plat = str(exc)
    return {
        "installed": b is not None,
        "version": version(),
        "managed": _external_binary() is None,
        "platform": plat,
        "install": dict(INSTALL),
    }


async def install() -> Path:
    """Download the pinned Claude Code release for this platform, verify its SHA-256
    against Anthropic's release manifest, and make it executable."""
    if _external_binary() is not None:
        raise ClaudeCodeError("Claude Code is provided by the image or BUILDPLATE_CLAUDE_BIN, so Buildplate does not install it.")
    async with _install_lock:
        existing = binary()
        if existing:
            INSTALL["running"] = False
            return existing
        ver, plat = version(), platform_id()
        INSTALL.update(running=True, done=0, total=0, error="", version=ver)
        dest = _managed_binary()
        tmp = dest.with_name("claude.download")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=120), follow_redirects=True) as client:
                r = await client.get(f"{RELEASES}/{ver}/manifest.json")
                r.raise_for_status()
                entry = (r.json().get("platforms") or {}).get(plat) or {}
                checksum = str(entry.get("checksum", ""))
                if not re.fullmatch(r"[a-f0-9]{64}", checksum):
                    raise ClaudeCodeError(f"Claude Code {ver} has no build for {plat}.")
                INSTALL["total"] = int(entry.get("size") or 0)
                digest = hashlib.sha256()
                with open(tmp, "wb") as fh:
                    async with client.stream("GET", f"{RELEASES}/{ver}/{plat}/claude") as resp:
                        resp.raise_for_status()
                        async for chunk in resp.aiter_bytes(1 << 20):
                            fh.write(chunk)
                            digest.update(chunk)
                            INSTALL["done"] += len(chunk)
            if digest.hexdigest() != checksum:
                raise ClaudeCodeError("The Claude Code download did not match its published checksum, so it was discarded.")
            os.chmod(tmp, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
            tmp.replace(dest)
            await _check_runs(dest)
            # keep only the pinned version
            for other in tools_dir().iterdir():
                if other.is_dir() and other.name != ver:
                    shutil.rmtree(other, ignore_errors=True)
            return dest
        except httpx.HTTPError as exc:
            INSTALL["error"] = f"Could not download Claude Code: {exc.__class__.__name__}. Check the container's internet access."
            raise ClaudeCodeError(INSTALL["error"]) from exc
        except (OSError, ClaudeCodeError) as exc:
            INSTALL["error"] = str(exc)
            raise ClaudeCodeError(str(exc)) from exc
        finally:
            INSTALL["running"] = False
            tmp.unlink(missing_ok=True)


async def _check_runs(path: Path) -> None:
    """Make sure the installed binary can execute here; remove it if not."""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(path), "--version", stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={"PATH": "/usr/bin:/bin", "DISABLE_UPDATES": "1"},
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 60)
        ok = proc.returncode == 0
    except (OSError, asyncio.TimeoutError) as exc:
        ok, out = False, str(exc).encode()
    if not ok:
        path.unlink(missing_ok=True)
        raise ClaudeCodeError(
            "Claude Code was downloaded but cannot run in this container. If the data folder is mounted with "
            "noexec, build the image with --build-arg CLAUDE_CODE_VERSION or set BUILDPLATE_CLAUDE_BIN. "
            f"({out.decode(errors='replace').strip()[:200]})"
        )


# ---- status and sign-out ----------------------------------------------------------
async def _run(connection_id: str, *args: str, timeout: float = 30) -> tuple[int, str, str]:
    b = binary()
    if b is None:
        raise ClaudeCodeError("Claude Code is not installed yet.")
    proc = await asyncio.create_subprocess_exec(
        str(b), *args,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=child_env(connection_id), cwd=str(config_dir(connection_id)),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ClaudeCodeError("Claude Code did not respond in time.") from None
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def auth_status(connection_id: str) -> dict[str, Any]:
    """Ask Claude Code whether this connection is signed in. Returns
    {state, detail}: connected, needs_sign_in, not_installed or error."""
    if binary() is None:
        return {"state": "not_installed", "detail": "Install Claude Code to use this connection."}
    code, out, err = await _run(connection_id, "auth", "status")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        data = {}
    method = str(data.get("authMethod") or "")
    if code != 0 or method in ("", "none"):
        return {"state": "needs_sign_in", "detail": "Not signed in. Select Sign in with Claude."}
    if method != "claude.ai":
        return {"state": "error", "detail": f"Claude Code is signed in with '{method}', not a Claude plan. Sign out and sign in again."}
    plan = str(data.get("subscriptionType") or data.get("plan") or "").strip()
    return {"state": "connected", "detail": f"Signed in ({plan.capitalize()} plan)" if plan else "Signed in to your Claude plan"}


async def logout(connection_id: str) -> None:
    """Sign this connection out through Claude Code itself."""
    if binary() is None:
        return
    await _run(connection_id, "auth", "logout")


async def forget(connection_id: str) -> None:
    """Sign out and remove the connection's Claude Code folder (used when the
    connection is deleted)."""
    try:
        await logout(connection_id)
    except ClaudeCodeError:
        pass
    shutil.rmtree(config_dir(connection_id), ignore_errors=True)
