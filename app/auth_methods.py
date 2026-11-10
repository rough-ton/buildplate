"""Registry of the ways Buildplate can connect to an AI model.

An AuthMethod describes one way to reach a provider family: which credential fields it
needs (and which of those are secret), who runs the design loop (the runtime), how usage
is billed, and hooks to list models and build LiteLLM call arguments. A user's
Connection (see connections.py) picks one method and holds its field values.

Phase 1 covers the methods Buildplate supported before connections existed, plus
OpenAI-compatible presets. Subscription, OAuth and cloud methods are added later.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

import httpx

KINDS = ("api_key", "oauth_key", "oauth_tokens", "cloud", "cli_session", "none")
RUNTIMES = ("litellm",)
BILLING = {
    "api": "Pay per use",
    "subscription": "Uses your plan",
    "cloud": "Cloud account",
    "local": "Local",
}


class MethodError(Exception):
    """A connection could not be used or tested; the message is shown to the user."""


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    type: str = "text"  # text | password | url
    secret: bool = False
    required: bool = False
    default: str = ""
    help: str = ""
    placeholder: str = ""


ModelLister = Callable[[dict[str, Any]], Awaitable[list[str]]]


@dataclass(frozen=True)
class AuthMethod:
    id: str
    family: str  # provider family, used for pricing keys and run records
    family_label: str
    label: str
    description: str
    kind: str
    billing: str
    fields: tuple[Field, ...]
    litellm_prefix: str
    list_models: ModelLister = field(repr=False, compare=False)
    runtime: str = "litellm"
    help_url: str = ""
    compliance: str = ""
    # Model name hints for vision support when LiteLLM does not know the model.
    vision_hints: tuple[str, ...] = ()
    vision_default: bool = False
    # Extra fixed LiteLLM kwargs, such as a preset's api_base.
    fixed: tuple[tuple[str, str], ...] = ()

    @property
    def secret_fields(self) -> set[str]:
        return {f.name for f in self.fields if f.secret}

    def litellm_kwargs(self, values: dict[str, Any], model: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": self.litellm_prefix + model, **dict(self.fixed)}
        if values.get("api_key"):
            kwargs["api_key"] = values["api_key"]
        elif self.kind == "none" and self.litellm_prefix == "openai/":
            kwargs["api_key"] = "not-needed"  # OpenAI client insists on a key
        if values.get("base_url"):
            kwargs["api_base"] = values["base_url"].rstrip("/")
        return kwargs

    def validate(self, values: dict[str, Any]) -> None:
        for f in self.fields:
            if f.required and not str(values.get(f.name) or "").strip():
                raise MethodError(f"{f.label} is required.")

    def public(self) -> dict[str, Any]:
        out = asdict(self)
        out.pop("list_models", None)
        out.pop("fixed", None)
        out["billing_label"] = BILLING[self.billing]
        return out


# ---- model listing -------------------------------------------------------------
async def _get(url: str, headers: dict[str, str] | None = None) -> Any:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=headers or {})
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        hint = " (check the API key)" if code in (401, 403) else ""
        raise MethodError(f"Provider returned HTTP {code}{hint}.") from exc
    except httpx.HTTPError as exc:
        base = url.split("/", 3)[:3]
        raise MethodError(f"Could not reach {'/'.join(base)}: {exc.__class__.__name__}. Check the URL and network.") from exc


_OPENAI_SKIP = ("embedding", "whisper", "tts", "dall-e", "image", "audio", "realtime", "moderation", "transcribe", "search", "davinci", "babbage")


async def _anthropic(v: dict[str, Any]) -> list[str]:
    data = await _get("https://api.anthropic.com/v1/models?limit=100", {"x-api-key": v["api_key"], "anthropic-version": "2023-06-01"})
    return [m["id"] for m in data.get("data", [])]


async def _openai(v: dict[str, Any]) -> list[str]:
    data = await _get("https://api.openai.com/v1/models", {"Authorization": f"Bearer {v['api_key']}"})
    ids = [m["id"] for m in data.get("data", [])]
    return sorted((i for i in ids if not any(s in i for s in _OPENAI_SKIP)), reverse=True)


async def _gemini(v: dict[str, Any]) -> list[str]:
    data = await _get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", {"x-goog-api-key": v["api_key"]})
    return [
        m["name"].removeprefix("models/")
        for m in data.get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
    ]


async def _openrouter(v: dict[str, Any]) -> list[str]:
    data = await _get("https://openrouter.ai/api/v1/models", {"Authorization": f"Bearer {v['api_key']}"})
    return sorted(m["id"] for m in data.get("data", []) if "tools" in (m.get("supported_parameters") or []))


async def _ollama(v: dict[str, Any]) -> list[str]:
    data = await _get(f"{v['base_url'].rstrip('/')}/api/tags")
    return [m["name"] for m in data.get("models", [])]


def _openai_style(default_base: str = "") -> ModelLister:
    async def lister(v: dict[str, Any]) -> list[str]:
        base = (v.get("base_url") or default_base).rstrip("/")
        key = v.get("api_key")
        data = await _get(f"{base}/models", {"Authorization": f"Bearer {key}"} if key else {})
        return sorted(m["id"] for m in data.get("data", []))

    return lister


# ---- registry ------------------------------------------------------------------
def _key(url: str, label: str = "API key", required: bool = True) -> Field:
    return Field(
        "api_key", label, "password", secret=True, required=required, help=url,
        placeholder="Paste your API key" if required else "Optional API key",
    )


_VISION_LOCAL = ("vl", "vision", "llava", "gemma3", "minicpm-v", "qwen2.5vl", "llama4", "mistral-small3")

_METHODS: list[AuthMethod] = [
    AuthMethod(
        id="anthropic.api_key", family="anthropic", family_label="Anthropic", label="Claude API key",
        description="Pay-per-use API key from the Claude Console.",
        kind="api_key", billing="api", fields=(_key("https://console.anthropic.com/settings/keys"),),
        litellm_prefix="anthropic/", list_models=_anthropic, help_url="https://console.anthropic.com/settings/keys",
        vision_default=True,
    ),
    AuthMethod(
        id="openai.api_key", family="openai", family_label="OpenAI", label="OpenAI API key",
        description="Pay-per-use API key from the OpenAI Platform.",
        kind="api_key", billing="api", fields=(_key("https://platform.openai.com/api-keys"),),
        litellm_prefix="openai/", list_models=_openai, help_url="https://platform.openai.com/api-keys",
        vision_default=True,
    ),
    AuthMethod(
        id="gemini.api_key", family="gemini", family_label="Google Gemini", label="Gemini API key",
        description="API key from Google AI Studio.",
        kind="api_key", billing="api", fields=(_key("https://aistudio.google.com/apikey"),),
        litellm_prefix="gemini/", list_models=_gemini, help_url="https://aistudio.google.com/apikey",
        vision_default=True,
    ),
    AuthMethod(
        id="openrouter.api_key", family="openrouter", family_label="OpenRouter", label="OpenRouter API key",
        description="One key for hundreds of models. Only models with tool support are listed.",
        kind="api_key", billing="api", fields=(_key("https://openrouter.ai/keys"),),
        litellm_prefix="openrouter/", list_models=_openrouter, help_url="https://openrouter.ai/keys",
    ),
    AuthMethod(
        id="ollama.local", family="ollama", family_label="Ollama", label="Ollama",
        description="Models running in Ollama on your network.",
        kind="none", billing="local",
        fields=(Field("base_url", "Base URL", "url", required=True, default="http://host.docker.internal:11434",
                      help="Where Ollama listens. From inside Docker, use your host's LAN IP or host.docker.internal."),),
        litellm_prefix="ollama_chat/", list_models=_ollama, vision_hints=_VISION_LOCAL,
    ),
    AuthMethod(
        id="openai_compatible.local", family="openai_compatible", family_label="OpenAI-compatible",
        label="OpenAI-compatible server",
        description="LM Studio, vLLM, LocalAI or any server with an OpenAI-compatible /v1 endpoint.",
        kind="none", billing="local",
        fields=(
            Field("base_url", "Base URL", "url", required=True, default="http://host.docker.internal:1234/v1",
                  help="The server's OpenAI-compatible /v1 endpoint."),
            _key("", required=False),
        ),
        litellm_prefix="openai/", list_models=_openai_style(), vision_hints=_VISION_LOCAL,
    ),
]

# Hosted OpenAI-compatible APIs with a fixed endpoint. LiteLLM knows each by its own
# prefix, which gives correct pricing.
_PRESETS = [
    ("xai", "xAI", "Grok models from xAI.", "https://api.x.ai/v1", "https://console.x.ai", True),
    ("mistral", "Mistral", "Mistral and Codestral models from La Plateforme.", "https://api.mistral.ai/v1", "https://console.mistral.ai/api-keys", False),
    ("groq", "Groq", "Open models on Groq's fast inference.", "https://api.groq.com/openai/v1", "https://console.groq.com/keys", False),
    ("deepseek", "DeepSeek", "DeepSeek chat and reasoning models.", "https://api.deepseek.com", "https://platform.deepseek.com/api_keys", False),
]
for _fam, _label, _desc, _base, _url, _vision in _PRESETS:
    _METHODS.append(
        AuthMethod(
            id=f"{_fam}.api_key", family=_fam, family_label=_label, label=f"{_label} API key", description=_desc,
            kind="api_key", billing="api", fields=(_key(_url),), litellm_prefix=f"{_fam}/",
            list_models=_openai_style(_base), help_url=_url, vision_default=_vision,
        )
    )

METHODS: dict[str, AuthMethod] = {m.id: m for m in _METHODS}

# Settings saved before connections existed had one entry per provider id.
LEGACY_PROVIDERS: dict[str, str] = {
    "anthropic": "anthropic.api_key",
    "openai": "openai.api_key",
    "gemini": "gemini.api_key",
    "openrouter": "openrouter.api_key",
    "ollama": "ollama.local",
    "openai_compatible": "openai_compatible.local",
}


def get(method_id: str) -> AuthMethod:
    try:
        return METHODS[method_id]
    except KeyError:
        raise MethodError(f"Unknown connection method '{method_id}'.") from None


def catalog() -> list[dict[str, Any]]:
    return [m.public() for m in _METHODS]
