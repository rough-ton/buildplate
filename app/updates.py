"""Check GitHub for a newer Buildplate release (issue #35).

Settings shows whether this install is up to date next to its version. The check asks
GitHub's API for the newest published release (drafts and pre-releases are skipped by
that endpoint) and compares it with config.VERSION, which release images get from
their git tag.

It never raises: offline, rate limited, a bad reply or a build without a release
version (local builds report "dev", main-branch builds "edge-<sha>") all give a result
with latest None, and Settings then shows only the version. Answers are cached so
opening Settings does not call GitHub each time (unauthenticated calls are limited to
60 an hour per IP address, which many installs may share).
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx

from . import config

REPO = "rough-ton/buildplate"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
TIMEOUT = 5.0
CACHE_SECONDS = 6 * 3600  # after a successful check
RETRY_SECONDS = 30 * 60  # after a failed one

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_cache: dict[str, Any] = {}  # {"at": monotonic time, "release": {...} | None}
_lock = asyncio.Lock()


def parse(version: str | None) -> tuple[int, int, int] | None:
    """(major, minor, patch) for "1.2.3" or "v1.2.3"; None for anything else."""
    m = _VERSION.match((version or "").strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


async def _fetch() -> dict[str, Any] | None:
    """The newest release as {"version", "url"}, or None if GitHub can't say."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"Buildplate/{config.VERSION}",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            r = await client.get(LATEST_URL, headers=headers)
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    tag = str(data.get("tag_name") or "")
    if not parse(tag):
        return None
    url = str(data.get("html_url") or "")
    if not url.startswith("https://github.com/"):
        url = f"{RELEASES_URL}/tag/{tag}"
    return {"version": tag.lstrip("v"), "url": url}


async def _latest() -> dict[str, Any] | None:
    async with _lock:
        now = time.monotonic()
        if _cache:
            ttl = CACHE_SECONDS if _cache["release"] else RETRY_SECONDS
            if now - _cache["at"] < ttl:
                return _cache["release"]
        release = await _fetch()
        _cache.update(at=time.monotonic(), release=release)
        return release


def clear_cache() -> None:
    _cache.clear()


async def status(enabled: bool = True) -> dict[str, Any]:
    """Whether a newer release than the running one exists.

    {"current", "checked", "latest", "update_available", "url"}. checked is False
    when the check is turned off or the running version isn't a release version;
    latest is None whenever GitHub gave no usable answer.
    """
    current = config.VERSION
    out: dict[str, Any] = {"current": current, "checked": False, "latest": None, "update_available": False, "url": None}
    mine = parse(current)
    if not enabled or not mine:
        return out
    out["checked"] = True
    release = await _latest()
    if not release:
        return out
    newer = parse(release["version"]) > mine
    out.update(latest=release["version"], update_available=newer, url=release["url"] if newer else None)
    return out
