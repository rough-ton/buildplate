"""AI provider registry: builds LiteLLM call arguments from live settings, lists models
and computes per-call token usage and cost."""

from __future__ import annotations

from typing import Any

import httpx

from .settings import PROVIDERS, store

LOCAL_PROVIDERS = {"ollama", "openai_compatible"}
_PREFIX = {
    "anthropic": "anthropic/",
    "openai": "openai/",
    "gemini": "gemini/",
    "openrouter": "openrouter/",
    "ollama": "ollama_chat/",
    "openai_compatible": "openai/",
}


class ProviderError(Exception):
    pass


def resolve(provider: str | None = None, model: str | None = None) -> tuple[str, str, dict[str, Any]]:
    """Return (provider_id, model_id, litellm_kwargs) for the requested or active model."""
    s = store.get()
    provider = provider or s["agent"]["provider"]
    model = model or s["agent"]["model"]
    if not provider or not model:
        raise ProviderError("No AI model selected. Open Settings > AI Providers, add a provider and choose a model.")
    if provider not in PROVIDERS:
        raise ProviderError(f"Unknown provider '{provider}'.")
    p = s["providers"][provider]
    if not p.get("enabled"):
        raise ProviderError(f"{PROVIDERS[provider]['label']} is disabled. Enable it in Settings.")
    meta = PROVIDERS[provider]
    if meta["needs_key"] and not p.get("api_key"):
        raise ProviderError(f"No API key saved for {meta['label']}.")
    kwargs: dict[str, Any] = {"model": _PREFIX[provider] + model}
    if p.get("api_key"):
        kwargs["api_key"] = p["api_key"]
    elif provider == "openai_compatible":
        kwargs["api_key"] = "not-needed"
    if provider in LOCAL_PROVIDERS:
        kwargs["api_base"] = p["base_url"].rstrip("/")
    return provider, model, kwargs


def supports_vision(provider: str, model: str) -> bool:
    mode = store.get()["agent"]["vision"]
    if mode == "on":
        return True
    if mode == "off":
        return False
    import litellm

    try:
        return bool(litellm.supports_vision(model=_PREFIX[provider] + model))
    except Exception:
        pass
    if provider in LOCAL_PROVIDERS:
        name = model.lower()
        return any(t in name for t in ("vl", "vision", "llava", "gemma3", "minicpm-v", "qwen2.5vl", "llama4", "mistral-small3"))
    return provider in ("anthropic", "openai", "gemini")


def usage_from_response(resp: Any, provider: str, model: str) -> dict[str, Any]:
    import litellm

    u = getattr(resp, "usage", None)
    inp = int(getattr(u, "prompt_tokens", 0) or 0)
    out = int(getattr(u, "completion_tokens", 0) or 0)
    cache_read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
    details = getattr(u, "prompt_tokens_details", None)
    if not cache_read and details is not None:
        cache_read = int(getattr(details, "cached_tokens", 0) or 0)
    cache_write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)

    overrides = store.get()["pricing"]["overrides"]
    ov = overrides.get(f"{provider}/{model}")
    cost, known = 0.0, True
    if ov:
        cost = inp / 1e6 * float(ov.get("input", 0)) + out / 1e6 * float(ov.get("output", 0))
    elif provider in LOCAL_PROVIDERS:
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


async def list_models(provider: str, override: dict[str, Any] | None = None) -> list[str]:
    """Query the provider for available models. Doubles as a connection test."""
    if provider not in PROVIDERS:
        raise ProviderError(f"Unknown provider '{provider}'.")
    p = dict(store.get()["providers"][provider])
    if override:
        p.update({k: v for k, v in override.items() if v})
    key = p.get("api_key", "")
    base = (p.get("base_url") or PROVIDERS[provider]["default_base_url"]).rstrip("/")
    if PROVIDERS[provider]["needs_key"] and not key:
        raise ProviderError("Enter an API key first.")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            if provider == "anthropic":
                r = await client.get(
                    "https://api.anthropic.com/v1/models?limit=100",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                )
                r.raise_for_status()
                return [m["id"] for m in r.json().get("data", [])]
            if provider == "openai":
                r = await client.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"})
                r.raise_for_status()
                ids = [m["id"] for m in r.json().get("data", [])]
                skip = ("embedding", "whisper", "tts", "dall-e", "image", "audio", "realtime", "moderation", "transcribe", "search", "davinci", "babbage")
                return sorted((i for i in ids if not any(s in i for s in skip)), reverse=True)
            if provider == "gemini":
                r = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                    headers={"x-goog-api-key": key},
                )
                r.raise_for_status()
                return [
                    m["name"].removeprefix("models/")
                    for m in r.json().get("models", [])
                    if "generateContent" in m.get("supportedGenerationMethods", [])
                ]
            if provider == "openrouter":
                r = await client.get("https://openrouter.ai/api/v1/models", headers={"Authorization": f"Bearer {key}"})
                r.raise_for_status()
                return sorted(
                    m["id"] for m in r.json().get("data", []) if "tools" in (m.get("supported_parameters") or [])
                )
            if provider == "ollama":
                r = await client.get(f"{base}/api/tags")
                r.raise_for_status()
                return [m["name"] for m in r.json().get("models", [])]
            if provider == "openai_compatible":
                headers = {"Authorization": f"Bearer {key}"} if key else {}
                r = await client.get(f"{base}/models", headers=headers)
                r.raise_for_status()
                return [m["id"] for m in r.json().get("data", [])]
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        hint = " (check the API key)" if code in (401, 403) else ""
        raise ProviderError(f"Provider returned HTTP {code}{hint}.") from exc
    except httpx.HTTPError as exc:
        raise ProviderError(f"Could not reach {base}: {exc.__class__.__name__}. Check the URL and network.") from exc
    return []
