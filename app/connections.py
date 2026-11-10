"""User connections to AI models.

A connection is a named use of one auth method (see auth_methods.py) with its field
values. Users can add any number, including several for one provider. Non-secret
values live in "fields", secret ones in "secrets" (encrypted at rest by the settings
store, masked when sent to the browser).
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

from . import auth_methods
from .auth_methods import AuthMethod, MethodError
from .settings import public_connection, store

STATES = ("connected", "needs_sign_in", "expired", "error", "untested")


@dataclass
class Resolved:
    connection: dict[str, Any]
    method: AuthMethod
    model: str
    kwargs: dict[str, Any]

    @property
    def id(self) -> str:
        return self.connection["id"]

    @property
    def family(self) -> str:
        return self.method.family


def all_() -> list[dict[str, Any]]:
    """Every connection with secrets decrypted. Server-side use only."""
    return store.get()["connections"]


def get(cid: str) -> dict[str, Any]:
    for c in all_():
        if c["id"] == cid:
            return c
    raise KeyError(cid)


def public_list() -> list[dict[str, Any]]:
    return [public_connection(c) for c in all_()]


def values(c: dict[str, Any]) -> dict[str, Any]:
    """Field values plus secrets, with method defaults filled in."""
    method = auth_methods.get(c["method"])
    out = {f.name: f.default for f in method.fields if f.default}
    out.update({k: v for k, v in (c.get("fields") or {}).items() if v not in (None, "")})
    out.update({k: v for k, v in (c.get("secrets") or {}).items() if v})
    return out


def _split(method: AuthMethod, data: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Sort submitted values into fields and secrets by the method's schema; drop unknowns."""
    fields, secrets_ = {}, {}
    for f in method.fields:
        if f.name in data and data[f.name] is not None:
            val = str(data[f.name]).strip()
            (secrets_ if f.secret else fields)[f.name] = val
    return fields, secrets_


def _label(label: str | None, method: AuthMethod) -> str:
    return (str(label or "").strip() or method.family_label)[:60]


def create(method_id: str, label: str | None = None, data: dict[str, Any] | None = None, enabled: bool = True) -> dict[str, Any]:
    method = auth_methods.get(method_id)
    fields, secrets_ = _split(method, data or {})
    conn = {
        "id": "c_" + secrets.token_hex(3),
        "method": method_id,
        "label": _label(label, method),
        "enabled": bool(enabled),
        "fields": fields,
        "secrets": {k: v for k, v in secrets_.items() if v},
        "models": [],
        "status": {"state": "untested", "detail": "", "checked_at": 0},
    }
    method.validate(values(conn))
    store.set_connections(all_() + [conn])
    return public_connection(get(conn["id"]))


def update(cid: str, label: str | None = None, data: dict[str, Any] | None = None, enabled: bool | None = None,
           models: list[str] | None = None) -> dict[str, Any]:
    """Change a connection. In data, a secret given as "" is cleared and a missing
    secret is kept; credential changes reset the status to untested."""
    conns = all_()
    conn = next((c for c in conns if c["id"] == cid), None)
    if conn is None:
        raise KeyError(cid)
    method = auth_methods.get(conn["method"])
    if label is not None:
        conn["label"] = _label(label, method)
    if enabled is not None:
        conn["enabled"] = bool(enabled)
    if models is not None:
        conn["models"] = [str(m) for m in models][:500]
    if data:
        fields, secrets_ = _split(method, data)
        conn["fields"].update(fields)
        for k, v in secrets_.items():
            if v:
                conn["secrets"][k] = v
            else:
                conn["secrets"].pop(k, None)
        if fields or secrets_:
            conn["status"] = {"state": "untested", "detail": "", "checked_at": 0}
    method.validate(values(conn))
    store.set_connections(conns)
    return public_connection(get(cid))


def delete(cid: str) -> None:
    conns = all_()
    if not any(c["id"] == cid for c in conns):
        raise KeyError(cid)
    store.set_connections([c for c in conns if c["id"] != cid])


def _set_status(cid: str, state: str, detail: str = "", models: list[str] | None = None) -> None:
    conns = all_()
    for c in conns:
        if c["id"] == cid:
            c["status"] = {"state": state, "detail": detail[:300], "checked_at": time.time()}
            if models is not None:
                c["models"] = models[:500]
    store.set_connections(conns)


async def test(cid: str) -> list[str]:
    """Check a connection by listing its models. Saves the models and the status."""
    conn = get(cid)
    method = auth_methods.get(conn["method"])
    try:
        method.validate(values(conn))
        models = await method.list_models(values(conn))
    except MethodError as exc:
        _set_status(cid, "error", str(exc))
        raise
    _set_status(cid, "connected", f"{len(models)} models", models)
    if not store.get()["agent"]["connection"] and models:
        store.update({"agent": {"connection": cid, "model": models[0]}})
    return models


def resolve(connection: str | None = None, model: str | None = None, provider: str | None = None) -> Resolved:
    """Pick the connection and model for a request: the given ones, else the design
    agent's defaults. provider is the pre-connections way to pick (a provider family);
    it selects the first enabled connection of that family."""
    s = store.get()
    conns = s["connections"]
    if not connection and provider:
        connection = next((c["id"] for c in conns if c.get("enabled") and auth_methods.METHODS.get(c["method"]) and auth_methods.METHODS[c["method"]].family == provider), "")
        if not connection:
            raise MethodError(f"No enabled connection for '{provider}'. Add one in Settings > AI connections.")
    connection = connection or s["agent"]["connection"]
    model = (model or (s["agent"]["model"] if connection == s["agent"]["connection"] else "") or "").strip()
    if not connection or not model:
        raise MethodError("No AI model selected. Open Settings > AI connections, add a connection and choose a model.")
    conn = next((c for c in conns if c["id"] == connection), None)
    if conn is None:
        raise MethodError("That AI connection no longer exists. Pick another model.")
    if not conn.get("enabled"):
        raise MethodError(f"The connection '{conn['label']}' is disabled. Enable it in Settings.")
    method = auth_methods.get(conn["method"])
    vals = values(conn)
    method.validate(vals)
    return Resolved(conn, method, model, method.litellm_kwargs(vals, model))
