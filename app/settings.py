"""Live settings store.

Settings are persisted to /data/settings.json and read on every use, so changes made
in the web UI (AI connections, models, printer profile, limits) take effect immediately
with no restart or rebuild. Secrets are encrypted at rest with a per-install key.

AI access is configured as a list of connections (see connections.py and
auth_methods.py). Settings files from before connections had one "providers" entry per
provider; they are converted on load.
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

from . import auth_methods, config

SETTINGS_VERSION = 2

# Pre-connections provider metadata. Only used to serve the read-only compatibility
# view of the old "providers" settings shape for one release.
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
    # Each: {id, method, label, enabled, fields, secrets, models, status}. secrets values
    # are encrypted on disk; see connections.py.
    "connections": [],
    "agent": {
        "connection": "",
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
    "type": "generic",
    "bed_x": 256,
    "bed_y": 256,
    "bed_z": 256,
    "nozzle": 0.4,
    "layer_height": 0.2,
    "clearance": 0.25,
    "max_overhang": 45,
}
PRINTER_TYPES = ("bedslinger", "cantilever", "corexy_open", "corexy_enclosed", "delta", "generic")
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
    for p in s["printers"]:
        if p.get("type") not in PRINTER_TYPES:
            p["type"] = _infer_type(p)
            changed = True
    ids = [p["id"] for p in s["printers"]]
    if s.get("active_printer") not in ids:
        s["active_printer"] = ids[0]
        changed = True
    return changed or legacy is not None


def _infer_type(p: dict[str, Any]) -> str:
    """Best guess for profiles saved before printer types existed."""
    if p.get("shape") == "round":
        return "delta"
    try:
        from . import printers as catalog

        if p.get("preset") and catalog.find(p["preset"]):
            return catalog.find(p["preset"])["type"]
        name = str(p.get("name", "")).lower()
        best = None
        for c in catalog.catalog()["printers"]:
            disp = c["display"].lower()
            if name.startswith(disp) and (best is None or len(disp) > len(best["display"])):
                best = c
        if best:
            return best["type"]
    except Exception:
        pass
    return "generic"


def active_printer(s: dict[str, Any]) -> dict[str, Any]:
    for p in s["printers"]:
        if p["id"] == s["active_printer"]:
            return p
    return s["printers"][0]


def _migrate_connections(s: dict[str, Any]) -> bool:
    """Settings version 1 had one "providers" entry per provider id and an agent
    provider. Turn every provider that has a saved key or was enabled into a
    connection (keeping its already-encrypted key), and point the agent at it."""
    if int(s.get("settings_version") or 1) >= SETTINGS_VERSION:
        return False
    old = s.pop("providers", None) or {}
    active = s["agent"].pop("provider", "") or ""
    conns = s.setdefault("connections", [])
    have = {c["id"] for c in conns}
    for pid, p in old.items():
        method_id = auth_methods.LEGACY_PROVIDERS.get(pid)
        if not method_id or not isinstance(p, dict) or not (p.get("api_key") or p.get("enabled")):
            continue
        method = auth_methods.METHODS[method_id]
        cid = f"c_{pid}"
        if cid in have:
            continue
        names = {f.name for f in method.fields}
        conns.append(
            {
                "id": cid,
                "method": method_id,
                "label": method.family_label,
                "enabled": bool(p.get("enabled")),
                "fields": {"base_url": p["base_url"]} if "base_url" in names and p.get("base_url") else {},
                "secrets": {"api_key": p["api_key"]} if "api_key" in names and p.get("api_key") else {},
                "models": list(p.get("models") or []),
                "status": {"state": "connected" if p.get("models") else "untested", "detail": "", "checked_at": 0},
            }
        )
    ids = {c["id"] for c in conns}
    if not s["agent"].get("connection") and f"c_{active}" in ids:
        s["agent"]["connection"] = f"c_{active}"
    s["settings_version"] = SETTINGS_VERSION
    return True


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
                migrated = _migrate_connections(self._cache)
                migrated = _migrate_printers(self._cache) or migrated
                if migrated and config.SETTINGS_FILE.exists():
                    self._write(self._cache)
                self._apply_env_bootstrap()
            return self._cache

    def _apply_env_bootstrap(self) -> None:
        """First-run convenience: seed values from env vars if not already configured."""
        s = self._cache
        assert s is not None
        changed = False
        methods_in_use = {c["method"] for c in s["connections"]}

        def seed(method_id: str, cid: str, fields: dict[str, str], secrets_: dict[str, str]) -> None:
            nonlocal changed
            if method_id in methods_in_use or any(c["id"] == cid for c in s["connections"]):
                return
            s["connections"].append(
                {
                    "id": cid,
                    "method": method_id,
                    "label": auth_methods.METHODS[method_id].family_label,
                    "enabled": True,
                    "fields": fields,
                    "secrets": {k: self._enc(v) for k, v in secrets_.items()},
                    "models": [],
                    "status": {"state": "untested", "detail": "Added from an environment variable", "checked_at": 0},
                }
            )
            changed = True

        for var, method_id in ENV_KEYS.items():
            if os.environ.get(var):
                seed(method_id, f"c_env_{method_id.split('.')[0]}", {}, {"api_key": os.environ[var]})
        if os.environ.get("OLLAMA_BASE_URL"):
            seed("ollama.local", "c_env_ollama", {"base_url": os.environ["OLLAMA_BASE_URL"]}, {})
        # Cloud accounts, from the variables each cloud's own tools use
        env = os.environ
        aws_region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1"
        if env.get("AWS_BEARER_TOKEN_BEDROCK"):
            seed("bedrock.api_key", "c_env_bedrock_key", {"region": aws_region}, {"api_key": env["AWS_BEARER_TOKEN_BEDROCK"]})
        elif env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY"):
            secrets_ = {"access_key_id": env["AWS_ACCESS_KEY_ID"], "secret_access_key": env["AWS_SECRET_ACCESS_KEY"]}
            if env.get("AWS_SESSION_TOKEN"):
                secrets_["session_token"] = env["AWS_SESSION_TOKEN"]
            seed("bedrock.iam", "c_env_bedrock", {"region": aws_region}, secrets_)
        creds_file = env.get("GOOGLE_APPLICATION_CREDENTIALS")
        if creds_file and os.path.isfile(creds_file):
            try:
                creds = open(creds_file).read()
                project = env.get("GOOGLE_CLOUD_PROJECT") or env.get("VERTEXAI_PROJECT") or json.loads(creds).get("project_id", "")
                seed("vertex.service_account", "c_env_vertex",
                     {"project": project, "location": env.get("VERTEXAI_LOCATION") or env.get("GOOGLE_CLOUD_LOCATION") or "us-central1"},
                     {"credentials_json": creds})
            except (OSError, ValueError):
                pass
        if env.get("AZURE_OPENAI_ENDPOINT") and (env.get("AZURE_OPENAI_API_KEY") or env.get("AZURE_CLIENT_SECRET")):
            fields = {"endpoint": env["AZURE_OPENAI_ENDPOINT"], "deployments": env.get("AZURE_OPENAI_DEPLOYMENTS", ""),
                      "api_version": env.get("AZURE_OPENAI_API_VERSION") or env.get("OPENAI_API_VERSION") or "2024-10-21"}
            secrets_ = {"api_key": env["AZURE_OPENAI_API_KEY"]} if env.get("AZURE_OPENAI_API_KEY") else {"client_secret": env["AZURE_CLIENT_SECRET"]}
            if not env.get("AZURE_OPENAI_API_KEY"):
                fields |= {"tenant_id": env.get("AZURE_TENANT_ID", ""), "client_id": env.get("AZURE_CLIENT_ID", "")}
            seed("azure.openai", "c_env_azure", fields, secrets_)
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
    def encrypt(self, value: str) -> str:
        return self._enc(value)

    def get(self) -> dict[str, Any]:
        """Full settings with secrets decrypted. Server-side use only."""
        data = copy.deepcopy(self._read())
        for c in data["connections"]:
            c["secrets"] = {k: self._dec(v) for k, v in (c.get("secrets") or {}).items()}
        data["security"]["mcp_token"] = self._dec(data["security"].get("mcp_token", ""))
        data["printer"] = copy.deepcopy(active_printer(data))
        return data

    def public(self) -> dict[str, Any]:
        """Settings safe to send to the browser: secrets are masked."""
        data = self.get()
        data["connections"] = [public_connection(c) for c in data["connections"]]
        data["providers"] = _legacy_providers_view(data)
        active = next((c for c in data["connections"] if c["id"] == data["agent"]["connection"]), None)
        data["agent"]["provider"] = active["family"] if active else ""
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
            # Connections change only through connections.py, which handles encryption.
            patch.pop("connections", None)
            patch.pop("settings_version", None)
            if "providers" in patch:
                raise ValueError("AI providers are now connections. Use /api/connections.")
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

    def set_connections(self, conns: list[dict[str, Any]]) -> None:
        """Replace the connection list. secrets values are plain text and get encrypted."""
        with self._lock:
            current = copy.deepcopy(self._read())
            current["connections"] = [
                {**c, "secrets": {k: self._enc(v) for k, v in (c.get("secrets") or {}).items() if v}} for c in conns
            ]
            ids = {c["id"] for c in conns}
            if current["agent"].get("connection") not in ids:
                current["agent"]["connection"] = ""
            self._write(current)

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
        kind = p.get("type")
        out["type"] = kind if kind in PRINTER_TYPES else _infer_type(out)
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
    a.pop("provider", None)
    if a.get("connection") and a["connection"] not in {c["id"] for c in s["connections"]}:
        raise ValueError("Unknown connection for the design agent")
    r = s["render"]
    r["timeout"] = max(10, min(int(r["timeout"]), 1800))
    r["max_parallel"] = max(1, min(int(r["max_parallel"]), 16))
    if r["backend"] not in ("auto", "manifold", "cgal"):
        r["backend"] = "auto"
    s.pop("printer", None)
    _validate_printers(s)


ENV_KEYS = {
    "ANTHROPIC_API_KEY": "anthropic.api_key",
    "OPENAI_API_KEY": "openai.api_key",
    "GEMINI_API_KEY": "gemini.api_key",
    "OPENROUTER_API_KEY": "openrouter.api_key",
    "XAI_API_KEY": "xai.api_key",
    "MISTRAL_API_KEY": "mistral.api_key",
    "GROQ_API_KEY": "groq.api_key",
    "DEEPSEEK_API_KEY": "deepseek.api_key",
}


def mask(value: str) -> str:
    return ("****" + value[-4:]) if len(value) >= 8 else ("set" if value else "")


def public_connection(c: dict[str, Any]) -> dict[str, Any]:
    """A connection safe for the browser: secrets become {set, hint}."""
    method = auth_methods.METHODS.get(c["method"])
    out = {k: copy.deepcopy(v) for k, v in c.items() if k != "secrets"}
    out["secrets"] = {}
    for name in sorted((method.secret_fields if method else set()) | set(c.get("secrets") or {})):
        val = (c.get("secrets") or {}).get(name, "")
        out["secrets"][name] = {"set": bool(val), "hint": mask(val)}
    out["family"] = method.family if method else ""
    out["billing"] = method.billing if method else ""
    out["method_label"] = method.label if method else c["method"]
    return out


def _legacy_providers_view(data: dict[str, Any]) -> dict[str, Any]:
    """The pre-connections "providers" shape, derived from the first connection of each
    legacy method. Read-only, kept for one release for API clients and the UI."""
    out = {}
    for pid, meta in PROVIDERS.items():
        method_id = auth_methods.LEGACY_PROVIDERS[pid]
        c = next((x for x in data["connections"] if x["method"] == method_id), None)
        key = (c or {}).get("secrets", {}).get("api_key", {})
        out[pid] = {
            "enabled": bool(c and c.get("enabled")),
            "base_url": ((c or {}).get("fields") or {}).get("base_url") or meta["default_base_url"],
            "models": list((c or {}).get("models") or []),
            "api_key_set": bool(key.get("set")),
            "api_key_hint": key.get("hint", ""),
            "connection": c["id"] if c else "",
            "meta": meta,
        }
    # labels for the newer provider families too, so the UI can name any run's provider
    for m in auth_methods.METHODS.values():
        out.setdefault(m.family, {"enabled": False, "base_url": "", "models": [], "api_key_set": False, "api_key_hint": "", "connection": "", "meta": {"label": m.family_label}})
    return out


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
