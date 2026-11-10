"""Registry of the ways Buildplate can connect to an AI model.

An AuthMethod describes one way to reach a provider family: which credential fields it
needs (and which of those are secret), who runs the design loop (the runtime), how usage
is billed, and hooks to list models and build LiteLLM call arguments. A user's
Connection (see connections.py) picks one method and holds its field values.

Methods cover API keys, local servers, OpenAI-compatible presets, and a Claude plan
through the Claude Code CLI. OAuth and cloud methods are added later.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

import httpx

KINDS = ("api_key", "oauth_key", "oauth_tokens", "cloud", "cli_session", "none")
RUNTIMES = ("litellm", "cli:claude", "responses:chatgpt", "sdk:copilot")
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
    type: str = "text"  # text | password | url | json (multi-line, can load a file) | select | hidden
    secret: bool = False
    required: bool = False
    default: str = ""
    help: str = ""
    placeholder: str = ""
    options: tuple[tuple[str, str], ...] = ()  # for select: (value, label)
    link_label: str = ""  # text for a help link (help is a URL); default "Get a key"


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
    # Needs a UI password and a signed-in session to add or change, with no
    # exception for requests from the server itself.
    requires_password: bool = False
    # Builds LiteLLM kwargs from the field values when the generic api_key/base_url
    # mapping is not enough (cloud providers).
    kwargs_builder: Callable[[dict[str, Any], str], dict[str, Any]] | None = field(default=None, repr=False, compare=False)
    # Extra checks on the field values (raise MethodError).
    validator: Callable[[dict[str, Any]], None] | None = field(default=None, repr=False, compare=False)
    # Test by sending one tiny request after listing models (when listing alone does
    # not prove the credentials can use a model).
    probe: bool = False
    # Sign-in flow that fills the credentials ("openrouter").
    oauth: str = ""

    @property
    def secret_fields(self) -> set[str]:
        return {f.name for f in self.fields if f.secret}

    def litellm_kwargs(self, values: dict[str, Any], model: str) -> dict[str, Any]:
        if self.runtime != "litellm":
            return {}
        if self.kwargs_builder:
            return self.kwargs_builder(values, model)
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
        if self.validator:
            self.validator(values)

    def public(self) -> dict[str, Any]:
        out = asdict(self)
        for k in ("list_models", "fixed", "kwargs_builder", "validator"):
            out.pop(k, None)
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

async def _claude_code_models(v: dict[str, Any]) -> list[str]:
    from . import claude_code

    return list(claude_code.MODELS)


_METHODS.insert(1, AuthMethod(
    id="anthropic.claude_code", family="anthropic", family_label="Anthropic", label="Claude plan (Claude Code)",
    description=(
        "Use your Claude Pro, Max, Team or Enterprise plan. Buildplate runs Claude Code, Anthropic's own "
        "command-line app, and you sign in to it with your Claude account."
    ),
    kind="cli_session", billing="subscription", fields=(), litellm_prefix="", runtime="cli:claude",
    list_models=_claude_code_models, vision_default=True, requires_password=True,
    help_url="https://code.claude.com/docs/en/authentication",
    compliance=(
        "This connection runs the unmodified Claude Code app, installed from Anthropic. You sign in through "
        "Claude Code's own login, and Claude Code keeps your sign-in in its own files: Buildplate never sees or "
        "stores your Claude credentials. Requests use your plan's normal usage limits."
    ),
))

async def _copilot_models(v: dict[str, Any]) -> list[str]:
    from . import copilot

    return await copilot.list_models(v)


def _copilot_validate(v: dict[str, Any]) -> None:
    from . import copilot

    copilot.validate(v)


async def _no_models(v: dict[str, Any]) -> list[str]:
    return []  # listed per connection by chatgpt.list_models, which needs the connection's sign-in


def _register_cloud() -> None:
    from . import cloud

    regions = tuple((r, r) for r in cloud.BEDROCK_REGIONS)
    region = Field("region", "Region", "select", required=True, default="us-east-1", options=regions)
    _METHODS.extend([
        AuthMethod(
            id="bedrock.iam", family="bedrock", family_label="Amazon Bedrock", label="Bedrock with IAM access keys",
            description="An IAM user's access key. Buildplate lists the models your account can use. A user limited to Bedrock (and short-term keys) is safest.",
            kind="cloud", billing="cloud", litellm_prefix="bedrock/", list_models=cloud.bedrock_models,
            kwargs_builder=cloud.bedrock_kwargs, probe=True, vision_default=True,
            help_url="https://docs.aws.amazon.com/bedrock/latest/userguide/security-iam.html",
            fields=(region,
                    Field("access_key_id", "Access key ID", "password", secret=True, required=True, placeholder="AKIA..."),
                    Field("secret_access_key", "Secret access key", "password", secret=True, required=True),
                    Field("session_token", "Session token", "password", secret=True, placeholder="Only for temporary credentials")),
        ),
        AuthMethod(
            id="bedrock.api_key", family="bedrock", family_label="Amazon Bedrock", label="Bedrock API key",
            description="A Bedrock API key (short-term keys last up to 12 hours). It can call models but not list them, so pick a model after connecting.",
            kind="cloud", billing="cloud", litellm_prefix="bedrock/", list_models=cloud.bedrock_models,
            kwargs_builder=cloud.bedrock_kwargs, probe=True, vision_default=True,
            help_url="https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html",
            fields=(region, Field("api_key", "Bedrock API key", "password", secret=True, required=True)),
        ),
        AuthMethod(
            id="bedrock.profile", family="bedrock", family_label="Amazon Bedrock", label="Bedrock with an AWS profile",
            description="A named profile from an AWS config folder mounted into the container (for example ~/.aws at /data/.home/.aws).",
            kind="cloud", billing="cloud", litellm_prefix="bedrock/", list_models=cloud.bedrock_models,
            kwargs_builder=cloud.bedrock_kwargs, probe=True, vision_default=True,
            fields=(region, Field("profile", "Profile name", required=True, default="default")),
        ),
        AuthMethod(
            id="vertex.service_account", family="vertex_ai", family_label="Google Vertex AI", label="Vertex AI service account",
            description="A service account key for a Google Cloud project with the Vertex AI API on. Gemini and Claude on Vertex are offered.",
            kind="cloud", billing="cloud", litellm_prefix="vertex_ai/", list_models=cloud.vertex_models,
            kwargs_builder=cloud.vertex_kwargs, validator=cloud.validate_vertex, probe=True, vision_default=True,
            help_url="https://cloud.google.com/iam/docs/keys-create-delete",
            fields=(Field("credentials_json", "Service account key (JSON)", "json", secret=True, required=True,
                          help="Paste the key file, or load it. It needs the Vertex AI User role."),
                    Field("project", "Project ID", placeholder="Taken from the key if empty"),
                    Field("location", "Location", default="us-central1", help="Region for Gemini, or us-east5 / europe-west1 for Claude.")),
        ),
        AuthMethod(
            id="azure.openai", family="azure", family_label="Azure OpenAI", label="Azure OpenAI",
            description="Deployments in an Azure OpenAI resource, with the resource's API key or an Entra ID service principal.",
            kind="cloud", billing="cloud", litellm_prefix="azure/", list_models=cloud.azure_models,
            kwargs_builder=cloud.azure_kwargs, validator=cloud.validate_azure, probe=True, vision_default=True,
            help_url="https://learn.microsoft.com/azure/ai-services/openai/",
            fields=(Field("endpoint", "Endpoint", "url", required=True, placeholder="https://your-resource.openai.azure.com"),
                    Field("deployments", "Deployment names", placeholder="gpt-5, gpt-5-mini", help="Comma-separated. These become the models you can pick."),
                    Field("api_version", "API version", default=cloud.AZURE_API_VERSION),
                    Field("api_key", "API key", "password", secret=True, placeholder="Or use a service principal below"),
                    Field("tenant_id", "Tenant ID (Entra ID)"),
                    Field("client_id", "Client ID (Entra ID)"),
                    Field("client_secret", "Client secret (Entra ID)", "password", secret=True)),
        ),
        AuthMethod(
            id="azure.foundry", family="azure_ai", family_label="Microsoft Foundry", label="Microsoft Foundry models",
            description="Models deployed in a Microsoft Foundry project, such as Llama, Mistral, DeepSeek or Grok.",
            kind="cloud", billing="cloud", litellm_prefix="azure_ai/", list_models=cloud.foundry_models,
            kwargs_builder=cloud.foundry_kwargs, validator=cloud.validate_foundry, probe=True,
            help_url="https://learn.microsoft.com/azure/ai-foundry/",
            fields=(Field("endpoint", "Endpoint", "url", required=True, placeholder="https://your-project.services.ai.azure.com/models"),
                    Field("models", "Model or deployment names", required=True, placeholder="Llama-4-Maverick, DeepSeek-V3"),
                    Field("api_key", "API key", "password", secret=True, required=True)),
        ),
        AuthMethod(
            id="openai.chatgpt", family="openai", family_label="OpenAI", label="ChatGPT plan (Sign in with ChatGPT)",
            description="Use your ChatGPT Plus or Pro plan through OpenAI's Sign in with ChatGPT, instead of an API key.",
            kind="oauth_tokens", billing="subscription", fields=(), litellm_prefix="", runtime="responses:chatgpt",
            list_models=_no_models, vision_default=True, requires_password=True,
            help_url="https://help.openai.com/en/articles/20001542",
            compliance=(
                "You sign in on OpenAI's own page and choose what Buildplate may use. Requests use your ChatGPT plan; "
                "you can set a weekly limit for Buildplate or disconnect it in ChatGPT's settings. Buildplate keeps "
                "the sign-in encrypted on your server."
            ),
        ),
        AuthMethod(
            id="github.copilot", family="github", family_label="GitHub", label="Copilot plan (GitHub token)",
            description="Use your GitHub Copilot plan through GitHub's Copilot SDK, instead of an API key.",
            kind="api_key", billing="subscription", litellm_prefix="", runtime="sdk:copilot",
            list_models=_copilot_models, validator=_copilot_validate, vision_default=True, requires_password=True,
            help_url="https://docs.github.com/en/copilot/how-tos/copilot-sdk/auth/authenticate",
            fields=(Field(
                "token", "GitHub token", "password", secret=True, required=True, placeholder="github_pat_...",
                help="https://github.com/settings/personal-access-tokens/new?name=Buildplate"
                     "&description=Lets+Buildplate+use+my+Copilot+plan&expires_in=90&copilot_requests=write",
                link_label="Create token on GitHub",
            ),),
            compliance=(
                "Create a fine-grained token on GitHub with only the Copilot requests permission (the link fills it in), "
                "then paste it here. Requests use your Copilot plan's premium requests. Buildplate runs GitHub's own "
                "Copilot runtime with no shell, file or web access, and keeps the token encrypted on your server. "
                "You can revoke the token on GitHub at any time. The first Copilot connection downloads GitHub's "
                "Copilot runtime (about 90 MB), so connecting takes a minute."
            ),
        ),
        AuthMethod(
            id="openrouter.oauth", family="openrouter", family_label="OpenRouter", label="Sign in with OpenRouter",
            description="Sign in on OpenRouter and Buildplate gets a key that you own and can revoke there. Billed to your OpenRouter account.",
            kind="oauth_key", billing="api", litellm_prefix="openrouter/", list_models=_openrouter, oauth="openrouter",
            fields=(Field("api_key", "API key", "hidden", secret=True),),
        ),
    ])


_register_cloud()

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
