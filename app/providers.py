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
    if method.runtime != "litellm":
        return method.vision_default  # e.g. Claude Code: model names are aliases LiteLLM does not know
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
    elif method.billing in ("local", "subscription"):
        cost = 0.0  # free, or included in the user's plan (the estimate is kept separately)
    else:
        try:
            cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
        except Exception:
            cost, known = 0.0, False
    out_usage = {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "total_tokens": inp + out,
        "cost_usd": round(cost, 6),
        "cost_known": known,
    }
    if method.billing == "subscription":
        out_usage["est_cost_usd"] = estimate_cost(method.family, model, out_usage)
    return out_usage


def _price(family: str, model: str) -> dict[str, Any] | None:
    import litellm

    for key in (model, f"{family}/{model}"):
        info = litellm.model_cost.get(key)
        if info and info.get("input_cost_per_token") is not None:
            return info
    return None


def estimate_cost(family: str, model: str, usage: dict[str, Any]) -> float | None:
    """What a request would cost at API list prices, from Buildplate's token fields:
    input_tokens (all input, cached included), cache_read_tokens, cache_write_tokens
    (of which cache_write_1h_tokens were 1-hour writes) and output_tokens. A pricing
    override for <family>/<model> wins, as it does for pay-per-use requests. Returns
    None when the model's price is unknown."""
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    ov = store.get()["pricing"]["overrides"].get(f"{family}/{model}")
    if ov:
        return round(inp / 1e6 * float(ov.get("input", 0)) + out / 1e6 * float(ov.get("output", 0)), 6)
    info = _price(family, model)
    if info is None:
        return None
    read = int(usage.get("cache_read_tokens") or 0)
    write = int(usage.get("cache_write_tokens") or 0)
    write_1h = min(int(usage.get("cache_write_1h_tokens") or 0), write)
    base_in = float(info["input_cost_per_token"])
    cost = (
        max(inp - read - write, 0) * base_in
        + read * float(info.get("cache_read_input_token_cost") or base_in)
        + (write - write_1h) * float(info.get("cache_creation_input_token_cost") or base_in)
        + write_1h * float(info.get("cache_creation_input_token_cost_above_1hr") or info.get("cache_creation_input_token_cost") or base_in)
        + out * float(info.get("output_cost_per_token") or 0)
    )
    return round(cost, 6)

