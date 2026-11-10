"""Static runtime configuration. Everything a user can change lives in settings.py instead."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Buildplate"
# Release images get this from their git tag; local and untagged builds report "dev".
VERSION = os.environ.get("BUILDPLATE_VERSION", "") or "dev"

DATA_DIR = Path(os.environ.get("BUILDPLATE_DATA", "/data")).resolve()
DESIGNS_DIR = DATA_DIR / "designs"
CACHE_DIR = DATA_DIR / "cache"
USER_LIB_DIR = DATA_DIR / "libraries"
SETTINGS_FILE = DATA_DIR / "settings.json"
SECRET_FILE = DATA_DIR / ".secret"
DB_FILE = DATA_DIR / "buildplate.db"

BUNDLED_LIB_DIR = Path(os.environ.get("BUILDPLATE_BUNDLED_LIBS", "/opt/openscad-libraries"))
STATIC_DIR = Path(__file__).parent / "static"
DOCS_DIR = Path(os.environ.get("BUILDPLATE_DOCS", Path(__file__).parent.parent / "docs"))

HOST = os.environ.get("BUILDPLATE_HOST", "0.0.0.0")
PORT = int(os.environ.get("BUILDPLATE_PORT", "8080"))


def ensure_dirs() -> None:
    for d in (DATA_DIR, DESIGNS_DIR, CACHE_DIR, USER_LIB_DIR):
        d.mkdir(parents=True, exist_ok=True)
