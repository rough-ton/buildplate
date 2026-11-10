"""Cloud and OAuth connection methods: Amazon Bedrock, Google Vertex AI, Azure OpenAI,
Microsoft Foundry, and Sign in with OpenRouter.

Each cloud method maps a connection's fields to LiteLLM arguments and lists the models
it can offer. Bedrock with IAM credentials lists the account's models through AWS;
the others suggest models from LiteLLM's price list (Vertex AI) or the deployments the
user names (Azure). A connection test then sends one tiny request (a few tokens) to
prove the credentials, region and model all work.

Azure Entra ID tokens are fetched with the OAuth 2.0 client-credentials grant and
cached until shortly before they expire, so no Azure SDK is needed.

Sign in with OpenRouter uses OpenRouter's PKCE flow: the browser goes to OpenRouter
and comes back to Buildplate's own callback with a code, which Buildplate exchanges
for an ordinary, user-owned OpenRouter API key.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import threading
import time
from typing import Any, Callable

import httpx

from .auth_methods import MethodError

PROBE_PROMPT = [{"role": "user", "content": "Reply with the word OK."}]


# ---- model suggestions from LiteLLM's price list ------------------------------------------
def _litellm_models(providers: tuple[str, ...], strip: str = "", contains: tuple[str, ...] = ()) -> list[str]:
    import litellm

    out = []
    for key, info in litellm.model_cost.items():
        if info.get("mode") != "chat" or info.get("litellm_provider") not in providers:
            continue
        if not info.get("supports_function_calling"):
            continue
        name = key.removeprefix(strip)
        if "/" in name or (contains and not any(c in name for c in contains)):
            continue
        out.append(name)
    return sorted(set(out), reverse=True)


# ---- Amazon Bedrock ----------------------------------------------------------------------------
BEDROCK_REGIONS = ("us-east-1", "us-east-2", "us-west-2", "ca-central-1", "eu-central-1", "eu-west-1", "eu-west-2",
                   "eu-west-3", "eu-north-1", "ap-northeast-1", "ap-northeast-2", "ap-south-1", "ap-southeast-1", "ap-southeast-2", "sa-east-1")


def bedrock_kwargs(v: dict[str, Any], model: str) -> dict[str, Any]:
    kw: dict[str, Any] = {"model": f"bedrock/{model}", "aws_region_name": v.get("region") or "us-east-1"}
    if v.get("api_key"):
        kw["api_key"] = v["api_key"]  # Bedrock API key (bearer token)
    if v.get("access_key_id"):
        kw["aws_access_key_id"] = v["access_key_id"]
        kw["aws_secret_access_key"] = v.get("secret_access_key", "")
        if v.get("session_token"):
            kw["aws_session_token"] = v["session_token"]
    if v.get("profile"):
        kw["aws_profile_name"] = v["profile"]
    return kw


def _bedrock_suggestions() -> list[str]:
    return _litellm_models(("bedrock_converse",), contains=("anthropic.", "amazon.nova", "meta.", "mistral.", "deepseek", "qwen", "openai."))


async def bedrock_models(v: dict[str, Any]) -> list[str]:
    """With IAM credentials or a profile, list the account's text models and
    inference profiles through AWS (free). A Bedrock API key cannot list models, so
    suggest models Bedrock offers and let the test request confirm access."""
    if v.get("api_key"):
        return _bedrock_suggestions()

    def list_models() -> list[str]:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError

        session = boto3.Session(
            aws_access_key_id=v.get("access_key_id") or None,
            aws_secret_access_key=v.get("secret_access_key") or None,
            aws_session_token=v.get("session_token") or None,
            profile_name=v.get("profile") or None,
            region_name=v.get("region") or "us-east-1",
        )
        client = session.client("bedrock")
        try:
            found = client.list_foundation_models(byOutputModality="TEXT")["modelSummaries"]
            ids = [m["modelId"] for m in found if "ON_DEMAND" in (m.get("inferenceTypesSupported") or [])]
            try:
                profiles = client.list_inference_profiles(maxResults=1000).get("inferenceProfileSummaries", [])
                ids += [p["inferenceProfileId"] for p in profiles]
            except ClientError:
                pass  # older permissions: foundation models only
            return sorted(set(ids), reverse=True)
        except ClientError as exc:
            msg = exc.response.get("Error", {}).get("Message") or str(exc)
            raise MethodError(f"AWS refused the request: {msg}") from exc
        except BotoCoreError as exc:
            raise MethodError(f"Could not use those AWS credentials: {exc}") from exc

    return await asyncio.to_thread(list_models)


# ---- Google Vertex AI ----------------------------------------------------------------------------
def vertex_kwargs(v: dict[str, Any], model: str) -> dict[str, Any]:
    return {
        "model": f"vertex_ai/{model}",
        "vertex_project": v.get("project", ""),
        "vertex_location": v.get("location") or "us-central1",
        "vertex_credentials": v.get("credentials_json", ""),
    }


def validate_vertex(v: dict[str, Any]) -> None:
    try:
        data = json.loads(v.get("credentials_json") or "")
    except json.JSONDecodeError:
        raise MethodError("The service account key must be the JSON file from Google Cloud.") from None
    if not isinstance(data, dict) or data.get("type") != "service_account" or not data.get("private_key"):
        raise MethodError("That JSON is not a service account key. Create a key for a service account and paste the whole file.")
    if not v.get("project"):
        v["project"] = data.get("project_id", "")


async def vertex_models(v: dict[str, Any]) -> list[str]:
    gemini = _litellm_models(("vertex_ai-language-models",), contains=("gemini",))
    claude = _litellm_models(("vertex_ai-anthropic_models",), strip="vertex_ai/")
    return gemini + claude


# ---- Azure OpenAI and Microsoft Foundry ------------------------------------------------------------
AZURE_API_VERSION = "2024-10-21"
_tokens: dict[tuple[str, str], tuple[str, float]] = {}
_token_lock = threading.Lock()


def _entra_token(tenant: str, client_id: str, secret: str) -> str:
    """An Entra ID access token for Azure AI services, cached until a few minutes
    before it expires."""
    key = (tenant, client_id)
    with _token_lock:
        cached = _tokens.get(key)
        if cached and cached[1] - time.time() > 300:
            return cached[0]
        r = httpx.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret,
                  "scope": "https://cognitiveservices.azure.com/.default"},
            timeout=20,
        )
        if r.status_code != 200:
            detail = (r.json().get("error_description") or "").split("\r\n")[0] if r.headers.get("content-type", "").startswith("application/json") else ""
            raise MethodError(f"Entra ID refused the service principal: {detail or r.status_code}")
        data = r.json()
        _tokens[key] = (data["access_token"], time.time() + float(data.get("expires_in", 3600)))
        return data["access_token"]


def azure_token_provider(v: dict[str, Any]) -> Callable[[], str]:
    tenant, client_id, secret = v["tenant_id"], v["client_id"], v["client_secret"]
    return lambda: _entra_token(tenant, client_id, secret)


def azure_kwargs(v: dict[str, Any], model: str) -> dict[str, Any]:
    kw: dict[str, Any] = {"model": f"azure/{model}", "api_base": v.get("endpoint", "").rstrip("/"),
                          "api_version": v.get("api_version") or AZURE_API_VERSION}
    if v.get("api_key"):
        kw["api_key"] = v["api_key"]
    else:
        kw["azure_ad_token_provider"] = azure_token_provider(v)
    return kw


def validate_azure(v: dict[str, Any]) -> None:
    if not str(v.get("endpoint", "")).startswith("https://"):
        raise MethodError("The endpoint is the https:// address of your Azure OpenAI resource.")
    entra = [bool(v.get(k)) for k in ("tenant_id", "client_id", "client_secret")]
    if not v.get("api_key") and not all(entra):
        raise MethodError("Enter an API key, or all three of tenant ID, client ID and client secret for an Entra ID service principal.")
    if not split_list(v.get("deployments")):
        raise MethodError("Enter at least one deployment name.")


def foundry_kwargs(v: dict[str, Any], model: str) -> dict[str, Any]:
    return {"model": f"azure_ai/{model}", "api_base": v.get("endpoint", "").rstrip("/"), "api_key": v.get("api_key", "")}


def validate_foundry(v: dict[str, Any]) -> None:
    if not str(v.get("endpoint", "")).startswith("https://"):
        raise MethodError("The endpoint is the https:// models address of your Foundry project.")
    if not split_list(v.get("models")):
        raise MethodError("Enter at least one model or deployment name.")


def split_list(value: Any) -> list[str]:
    return [x.strip() for x in str(value or "").replace("\n", ",").split(",") if x.strip()]


async def azure_models(v: dict[str, Any]) -> list[str]:
    return split_list(v.get("deployments"))


async def foundry_models(v: dict[str, Any]) -> list[str]:
    return split_list(v.get("models"))


# ---- the connection test request ----------------------------------------------------------------------
_CHEAP = ("nova-micro", "nova-lite", "haiku", "flash-lite", "flash", "mini", "small", "nano")


def pick_probe_model(models: list[str]) -> str:
    """The cheapest-looking model in the list, for the one-off test request."""
    for hint in _CHEAP:
        for m in models:
            if hint in m.lower():
                return m
    return models[0]


async def probe(kwargs: dict[str, Any]) -> None:
    """Send one tiny request to prove the credentials, region and model work."""
    import litellm

    litellm.suppress_debug_info = True
    try:
        await litellm.acompletion(**kwargs, messages=PROBE_PROMPT, max_tokens=5, temperature=0)
    except MethodError:
        raise
    except Exception as exc:  # LiteLLM wraps every provider error differently
        msg = str(exc)
        low = msg.lower()
        if "accessdenied" in low or "not authorized" in low or "403" in low:
            hint = "The credentials work but are not allowed to use this model. In the cloud console, grant model access or pick another model."
        elif "401" in low or ("invalid" in low and "key" in low) or "unauthorized" in low or "credentials" in low:
            hint = "The cloud provider rejected the credentials."
        elif "not found" in low or "404" in low or "does not exist" in low:
            hint = "That model or deployment was not found in this region or resource."
        else:
            hint = msg.split("\n")[0][:300]
        raise MethodError(hint) from exc


# ---- Sign in with OpenRouter (PKCE) ------------------------------------------------------------------------
OPENROUTER_AUTH = "https://openrouter.ai/auth"
OPENROUTER_EXCHANGE = "https://openrouter.ai/api/v1/auth/keys"
_pending: dict[str, dict[str, Any]] = {}
PENDING_TTL = 600


def openrouter_start(connection_id: str, callback_url: str) -> str:
    """The OpenRouter authorize URL for a connection. The PKCE verifier and state stay
    on the server for ten minutes."""
    now = time.time()
    for k in [k for k, p in _pending.items() if now - p["at"] > PENDING_TTL]:
        _pending.pop(k, None)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    _pending[state] = {"verifier": verifier, "connection": connection_id, "at": now}
    q = httpx.QueryParams({"callback_url": callback_url, "code_challenge": challenge, "code_challenge_method": "S256",
                           "state": state, "key_label": "Buildplate"})
    return f"{OPENROUTER_AUTH}?{q}"


async def openrouter_finish(state: str, code: str) -> tuple[str, str]:
    """Exchange the code for a key. Returns (connection id, key)."""
    pending = _pending.pop(state, None)
    if not pending or time.time() - pending["at"] > PENDING_TTL:
        raise MethodError("This sign-in link has expired or was already used. Start Sign in with OpenRouter again.")
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(OPENROUTER_EXCHANGE, json={"code": code, "code_verifier": pending["verifier"], "code_challenge_method": "S256"})
    if r.status_code != 200:
        raise MethodError(f"OpenRouter did not accept the sign-in (HTTP {r.status_code}). Try again.")
    key = (r.json() or {}).get("key")
    if not key:
        raise MethodError("OpenRouter did not return a key. Try again.")
    return pending["connection"], key
