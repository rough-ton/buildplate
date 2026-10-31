"""Live settings store.

Settings are persisted to /data/settings.json and read on every use, so changes made
in the web UI (API keys, models, printer profile, limits) take effect immediately with
no restart or rebuild. Secrets are encrypted at rest with a per-install key.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
import secrets
import threading
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from . import config

SECRET_FIELDS = {"api_key", "mcp_token"}

PROVIDERS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "needs_key": True,
        "needs_base_url": False,
        "default_base_url": "https://api.anthropic.com",
        "key_url": "https://console.anthropic.com/settings/keys",
    },
    "openai": {
        "label": "OpenAI",
        "needs_key": True,
        "needs_base_url": False,
        "default_base_url": "https://api.openai.com/v1",
        "key_url": "https://platform.openai.com/api-keys",
    },
    "gemini": {
        "label": "Google Gemini",
        "needs_key": True,
        "needs_base_url": False,
        "default_base_url": "https://generativelanguage.googleapis.com",
        "key_url": "https://aistudio.google.com/apikey",
    },
    "openrouter": {
        "label": "OpenRouter",
        "needs_key": True,
        "needs_base_url": False,
        "default_base_url": "https://openrouter.ai/api/v1",
        "key_url": "https://openrouter.ai/keys",
    },
    "ollama": {
        "label": "Ollama (local)",
        "needs_key": False,
        "needs_base_url": True,
        "default_base_url": "http://host.docker.internal:11434",
        "key_url": "",
    },
    "openai_compatible": {
        "label": "OpenAI-compatible (LM Studio, vLLM, LocalAI...)",
        "needs_key": False,
        "needs_base_url": True,
        "default_base_url": "http://host.docker.internal:1234/v1",
        "key_url": "",
    },
}

DEFAULTS: dict[str, Any] = {
    "providers": {
        pid: {"enabled": False, "api_key": "", "base_url": meta["default_base_url"], "models": []}
        for pid, meta in PROVIDERS.items()
    },
    "agent": {
        "provider": "",
        "model": "",
        "max_iterations": 6,
        "temperature": 0.2,
        "max_output_tokens": 8192,
        "vision": "auto",  # auto | on | off
        "preview_size": 768,
        "extra_instructions": "",
    },
    # Saved printer profiles. The active one is exposed as settings["printer"].
    "printers": [],
    "active_printer": "",
    "render": {
        "backend": "auto",  # auto | manifold | cgal
        "timeout": 180,
        "max_parallel": 2,
        "colorscheme": "Tomorrow",
    },
    "security": {
        "ui_password_hash": "",
        "mcp_token": "",
        "session_hours": 168,
    },
    "pricing": {"overrides": {}},
    "server": {"public_url": ""},
}


DEFAULT_PRINTER: dict[str, Any] = {
    "id": "default",
    "name": "My printer",
    "shape": "rect",
    "bed_x": 256,
    "bed_y": 256,
    "bed_z": 256,
    "nozzle": 0.4,
    "layer_height": 0.2,
    "clearance": 0.25,
    "max_overhang": 45,
}
PRINTER_FIELDS = ("bed_x", "bed_y", "bed_z", "nozzle", "layer_height", "clearance", "max_overhang")


def _migrate_printers(s: dict[str, Any]) -> bool:
    """Older settings had a single "printer" object. Convert it to a saved profile."""
    changed = False
    legacy = s.pop("printer", None)
    if not s.get("printers"):
        base = dict(DEFAULT_PRINTER)
        if isinstance(legacy, dict):
            base.update({k: v for k, v in legacy.items() if k in PRINTER_FIELDS or k == "name"})
        s["printers"] = [base]
        changed = True
    ids = [p["id"] for p in s["printers"]]
    if s.get("active_printer") not in ids:
        s["active_printer"] = ids[0]
        changed = True
    return changed or legacy is not None


def active_printer(s: dict[str, Any]) -> dict[str, Any]:
    for p in s["printers"]:
        if p["id"] == s["active_printer"]:
            return p
    return s["printers"][0]


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class SettingsStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: dict[str, Any] | None = None
        self._mtime: float = -1
        self._secret = self._load_secret()
        self._fernet = Fernet(self._secret)

    # ---- secret handling -------------------------------------------------
    def _load_secret(self) -> bytes:
        env = os.environ.get("BUILDPLATE_SECRET_KEY")
        if env:
            return base64.urlsafe_b64encode(hashlib.sha256(env.encode()).digest())
        config.ensure_dirs()
        if config.SECRET_FILE.exists():
            return config.SECRET_FILE.read_bytes().strip()
        key = Fernet.generate_key()
        config.SECRET_FILE.write_bytes(key)
        os.chmod(config.SECRET_FILE, 0o600)
        return key

    @property
    def signing_key(self) -> bytes:
        return hashlib.sha256(b"session:" + self._secret).digest()

    def _enc(self, value: str) -> str:
        if not value:
            return ""
        return "enc:" + self._fernet.encrypt(value.encode()).decode()

    def _dec(self, value: str) -> str:
        if not value or not value.startswith("enc:"):
            return value or ""
        try:
            return self._fernet.decrypt(value[4:].encode()).decode()
        except InvalidToken:
            return ""

    # ---- load / save -----------------------------------------------------
    def _read(self) -> dict[str, Any]:
        with self._lock:
            try:
                mtime = config.SETTINGS_FILE.stat().st_mtime
            except FileNotFoundError:
                mtime = 0
            if self._cache is None or mtime != self._mtime:
                raw = {}
                if config.SETTINGS_FILE.exists():
                    try:
                        raw = json.loads(config.SETTINGS_FILE.read_text())
                    except json.JSONDecodeError:
                        raw = {}
                self._cache = _deep_merge(DEFAULTS, raw)
                self._mtime = mtime
                if _migrate_printers(self._cache) and config.SETTINGS_FILE.exists():
                    self._write(self._cache)
                self._apply_env_bootstrap()
            return self._cache

    def _apply_env_bootstrap(self) -> None:
        """First-run convenience: seed values from env vars if not already configured."""
        s = self._cache
        assert s is not None
        changed = False
        env_keys = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }
        for pid, var in env_keys.items():
            val = os.environ.get(var)
            if val and not s["providers"][pid]["api_key"]:
                s["providers"][pid]["api_key"] = self._enc(val)
                s["providers"][pid]["enabled"] = True
                changed = True
        if os.environ.get("OLLAMA_BASE_URL") and not s["providers"]["ollama"]["enabled"]:
            s["providers"]["ollama"]["base_url"] = os.environ["OLLAMA_BASE_URL"]
            s["providers"]["ollama"]["enabled"] = True
            changed = True
        if os.environ.get("BUILDPLATE_ADMIN_PASSWORD") and not s["security"]["ui_password_hash"]:
            s["security"]["ui_password_hash"] = hash_password(os.environ["BUILDPLATE_ADMIN_PASSWORD"])
            changed = True
        if os.environ.get("BUILDPLATE_MCP_TOKEN") and not s["security"]["mcp_token"]:
            s["security"]["mcp_token"] = self._enc(os.environ["BUILDPLATE_MCP_TOKEN"])
            changed = True
        if changed:
            self._write(s)

    def _write(self, data: dict[str, Any]) -> None:
        config.ensure_dirs()
        tmp = config.SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(config.SETTINGS_FILE)
        self._cache = data
        self._mtime = config.SETTINGS_FILE.stat().st_mtime

    # ---- public API --------------------------------------------------------
    def get(self) -> dict[str, Any]:
        """Full settings with secrets decrypted. Server-side use only."""
        data = copy.deepcopy(self._read())
        for p in data["providers"].values():
            p["api_key"] = self._dec(p.get("api_key", ""))
        data["security"]["mcp_token"] = self._dec(data["security"].get("mcp_token", ""))
        data["printer"] = copy.deepcopy(active_printer(data))
        return data

    def public(self) -> dict[str, Any]:
        """Settings safe to send to the browser: secrets are masked."""
        data = self.get()
        for pid, p in data["providers"].items():
            key = p.pop("api_key", "")
            p["api_key_set"] = bool(key)
            p["api_key_hint"] = ("****" + key[-4:]) if len(key) >= 8 else ("set" if key else "")
            p["meta"] = PROVIDERS[pid]
        sec = data["security"]
        sec["password_enabled"] = bool(sec.pop("ui_password_hash", ""))
        token = sec.pop("mcp_token", "")
        sec["mcp_token_set"] = bool(token)
        sec["mcp_token_hint"] = ("****" + token[-4:]) if token else ""
        return data

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge a partial update. Secret fields are encrypted; an empty string clears them,
        and a missing field leaves them untouched."""
        with self._lock:
            current = copy.deepcopy(self._read())
            patch = copy.deepcopy(patch)
            patch.get("security", {}).pop("ui_password_hash", None)
            for pid, p in (patch.get("providers") or {}).items():
                if pid not in PROVIDERS:
                    raise ValueError(f"Unknown provider: {pid}")
                if "api_key" in p:
                    p["api_key"] = self._enc(p["api_key"].strip()) if p["api_key"] else ""
            if "mcp_token" in patch.get("security", {}):
                tok = patch["security"]["mcp_token"]
                patch["security"]["mcp_token"] = self._enc(tok) if tok else ""
            legacy_printer = patch.pop("printer", None)
            merged = _deep_merge(current, patch)
            if isinstance(legacy_printer, dict):
                # Older API shape: a single "printer" object edits the active profile.
                target = active_printer(merged)
                target.update({k: v for k, v in legacy_printer.items() if k in PRINTER_FIELDS or k == "name"})
            if "overrides" in (patch.get("pricing") or {}):
                merged["pricing"]["overrides"] = patch["pricing"]["overrides"]
            _validate(merged)
            self._write(merged)
        return self.public()

    def set_password(self, password: str) -> None:
        with self._lock:
            current = copy.deepcopy(self._read())
            current["security"]["ui_password_hash"] = hash_password(password) if password else ""
            self._write(current)

    def check_password(self, password: str) -> bool:
        stored = self._read()["security"].get("ui_password_hash", "")
        return bool(stored) and verify_password(password, stored)

    def password_enabled(self) -> bool:
        return bool(self._read()["security"].get("ui_password_hash"))

    def generate_mcp_token(self) -> str:
        token = "bp_" + secrets.token_urlsafe(32)
        self.update({"security": {"mcp_token": token}})
        return token


def _validate_printers(s: dict[str, Any]) -> None:
    printers = s.get("printers") or []
    if not printers:
        raise ValueError("Keep at least one printer profile.")
    seen: set[str] = set()
    clean = []
    for p in printers:
        pid = str(p.get("id") or "").strip() or secrets.token_hex(4)
        if pid in seen:
            pid = f"{pid}-{secrets.token_hex(2)}"
        seen.add(pid)
        out = {"id": pid, "name": (str(p.get("name") or "").strip() or "Printer")[:60]}
        for k in PRINTER_FIELDS:
            out[k] = float(p.get(k, DEFAULT_PRINTER[k]))
        for k in ("bed_x", "bed_y", "bed_z"):
            if not 20 <= out[k] <= 2000:
                raise ValueError(f"{out['name']}: build volume must be between 20 and 2000 mm.")
        if not 0.1 <= out["nozzle"] <= 2.0:
            raise ValueError(f"{out['name']}: nozzle must be between 0.1 and 2 mm.")
        if p.get("preset"):
            out["preset"] = str(p["preset"])[:80]
        out["shape"] = "round" if p.get("shape") == "round" else "rect"
        clean.append(out)
    s["printers"] = clean
    if s.get("active_printer") not in seen:
        s["active_printer"] = clean[0]["id"]


def _validate(s: dict[str, Any]) -> None:
    a = s["agent"]
    a["max_iterations"] = max(1, min(int(a["max_iterations"]), 25))
    a["temperature"] = max(0.0, min(float(a["temperature"]), 2.0))
    a["max_output_tokens"] = max(512, min(int(a["max_output_tokens"]), 64000))
    a["preview_size"] = max(256, min(int(a["preview_size"]), 2048))
    if a["vision"] not in ("auto", "on", "off"):
        a["vision"] = "auto"
    if a["provider"] and a["provider"] not in PROVIDERS:
        raise ValueError("Unknown active provider")
    r = s["render"]
    r["timeout"] = max(10, min(int(r["timeout"]), 1800))
    r["max_parallel"] = max(1, min(int(r["max_parallel"]), 16))
    if r["backend"] not in ("auto", "manifold", "cgal"):
        r["backend"] = "auto"
    s.pop("printer", None)
    _validate_printers(s)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(dk.hex(), dk_hex)


store = SettingsStore()
