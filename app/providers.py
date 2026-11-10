"""Per-call token usage, cost and vision support for models reached through LiteLLM.
Connection handling lives in connections.py and auth_methods.py."""

from __future__ import annotations

from typing import Any

from .auth_methods import AuthMethod
from .settings import store


def supports_vision(method: AuthMethod, model: str) -> bool:
    mode = store.get()["agent"]["vision"]
    if mode == "on":
        return True
    if mode == "off":
        return False
    import litellm

    try:
        if litellm.supports_vision(model=method.litellm_prefix + model):
            return True
    except Exception:
        pass
    if method.vision_hints:
        name = model.lower()
        return any(t in name for t in method.vision_hints)
    return method.vision_default


def usage_from_response(resp: Any, method: AuthMethod, model: str) -> dict[str, Any]:
    import litellm

    u = getattr(resp, "usage", None)
    inp = int(getattr(u, "prompt_tokens", 0) or 0)
    out = int(getattr(u, "completion_tokens", 0) or 0)
    cache_read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
    details = getattr(u, "prompt_tokens_details", None)
    if not cache_read and details is not None:
        cache_read = int(getattr(details, "cached_tokens", 0) or 0)
    cache_write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)

    # Pricing overrides keep keying on <provider family>/<model>.
    ov = store.get()["pricing"]["overrides"].get(f"{method.family}/{model}")
    cost, known = 0.0, True
    if ov:
        cost = inp / 1e6 * float(ov.get("input", 0)) + out / 1e6 * float(ov.get("output", 0))
    elif method.billing == "local":
        cost = 0.0
    else:
        try:
            cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
        except Exception:
            cost, known = 0.0, False
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "total_tokens": inp + out,
        "cost_usd": round(cost, 6),
        "cost_known": known,
    }
