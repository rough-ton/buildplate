import asyncio
import base64
import dataclasses
import hashlib
import importlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

SA_KEY = {"type": "service_account", "project_id": "maker-123", "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
          "client_email": "bp@maker-123.iam.gserviceaccount.com"}
CLOUD_ENV = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK", "AWS_REGION",
             "AWS_DEFAULT_REGION", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "VERTEXAI_PROJECT", "VERTEXAI_LOCATION",
             "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENTS", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID", "AZURE_CLIENT_ID"]


def _load(tmp_path, monkeypatch, env=None):
    for v in CLOUD_ENV + ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OLLAMA_BASE_URL"]:
        monkeypatch.delenv(v, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("BUILDPLATE_DATA", str(tmp_path))
    from app import config

    importlib.reload(config)
    from app import settings

    importlib.reload(settings)
    from app import auth, auth_methods, cloud, connections, db, providers

    for m in (auth_methods, cloud, connections, providers, auth, db):
        importlib.reload(m)
    db.init()
    return SimpleNamespace(store=settings.store, connections=connections, cloud=cloud, methods=auth_methods, db=db)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    return _load(tmp_path, monkeypatch)


def _probe_recorder(env, monkeypatch, fail=None):
    import litellm

    calls = []

    async def acompletion(**kw):
        calls.append(kw)
        if fail:
            raise Exception(fail)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="OK", tool_calls=None))], usage=None)

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    return calls


def test_methods_are_registered_and_public(env):
    cat = {m["id"]: m for m in env.methods.catalog()}
    for mid in ("bedrock.iam", "bedrock.api_key", "bedrock.profile", "vertex.service_account", "azure.openai", "azure.foundry", "openrouter.oauth"):
        assert mid in cat
    assert cat["bedrock.iam"]["billing"] == "cloud" and cat["bedrock.iam"]["probe"] is True
    assert ("eu-west-1", "eu-west-1") in [tuple(o) for o in cat["bedrock.iam"]["fields"][0]["options"]]
    assert "kwargs_builder" not in cat["azure.openai"] and "validator" not in cat["azure.openai"]
    assert cat["openrouter.oauth"]["oauth"] == "openrouter" and cat["openrouter.oauth"]["fields"][0]["type"] == "hidden"
    json.dumps(cat)  # serialisable for the API


def test_litellm_kwargs_per_method(env):
    M = env.methods.METHODS
    assert M["bedrock.iam"].litellm_kwargs({"region": "eu-west-1", "access_key_id": "AKIA1", "secret_access_key": "s", "session_token": "t"}, "anthropic.claude-x") == {
        "model": "bedrock/anthropic.claude-x", "aws_region_name": "eu-west-1", "aws_access_key_id": "AKIA1", "aws_secret_access_key": "s", "aws_session_token": "t"}
    assert M["bedrock.api_key"].litellm_kwargs({"region": "us-west-2", "api_key": "ABSK"}, "m") == {"model": "bedrock/m", "aws_region_name": "us-west-2", "api_key": "ABSK"}
    assert M["bedrock.profile"].litellm_kwargs({"region": "us-east-1", "profile": "work"}, "m")["aws_profile_name"] == "work"
    v = {"credentials_json": json.dumps(SA_KEY), "location": "europe-west1"}
    M["vertex.service_account"].validate(v)  # fills the project from the key
    assert M["vertex.service_account"].litellm_kwargs(v, "gemini-2.5-flash") == {
        "model": "vertex_ai/gemini-2.5-flash", "vertex_project": "maker-123", "vertex_location": "europe-west1", "vertex_credentials": json.dumps(SA_KEY)}
    az = M["azure.openai"].litellm_kwargs({"endpoint": "https://r.openai.azure.com/", "api_key": "k", "deployments": "gpt-5"}, "gpt-5")
    assert az == {"model": "azure/gpt-5", "api_base": "https://r.openai.azure.com", "api_version": "2024-10-21", "api_key": "k"}
    entra = M["azure.openai"].litellm_kwargs({"endpoint": "https://r.openai.azure.com", "tenant_id": "t", "client_id": "c", "client_secret": "s"}, "gpt-5")
    assert callable(entra["azure_ad_token_provider"]) and "api_key" not in entra
    assert M["azure.foundry"].litellm_kwargs({"endpoint": "https://p.services.ai.azure.com/models", "api_key": "k"}, "Llama-4") == {
        "model": "azure_ai/Llama-4", "api_base": "https://p.services.ai.azure.com/models", "api_key": "k"}


def test_validators(env):
    M, E = env.methods.METHODS, env.methods.MethodError
    with pytest.raises(E, match="JSON file"):
        M["vertex.service_account"].validate({"credentials_json": "not json"})
    with pytest.raises(E, match="not a service account key"):
        M["vertex.service_account"].validate({"credentials_json": json.dumps({"type": "authorized_user"})})
    with pytest.raises(E, match="API key, or all three"):
        M["azure.openai"].validate({"endpoint": "https://r.openai.azure.com", "deployments": "x", "tenant_id": "t"})
    with pytest.raises(E, match="deployment"):
        M["azure.openai"].validate({"endpoint": "https://r.openai.azure.com", "api_key": "k"})
    with pytest.raises(E, match="https://"):
        M["azure.foundry"].validate({"endpoint": "http://x", "models": "m", "api_key": "k"})
    assert env.cloud.split_list("gpt-5, gpt-5-mini\n o4 ,,") == ["gpt-5", "gpt-5-mini", "o4"]


def test_bedrock_api_key_connects_with_one_small_request(env, monkeypatch):
    calls = _probe_recorder(env, monkeypatch)
    c = env.connections.create("bedrock.api_key", "AWS", {"region": "us-west-2", "api_key": "ABSKdemo"})
    models = asyncio.run(env.connections.test(c["id"]))
    assert models and all("/" not in m for m in models)
    kw = calls[0]
    assert kw["model"].startswith("bedrock/") and kw["api_key"] == "ABSKdemo" and kw["aws_region_name"] == "us-west-2" and kw["max_tokens"] == 5
    picked = kw["model"].removeprefix("bedrock/")
    assert any(h in picked for h in env.cloud._CHEAP)  # the cheapest-looking model
    st = env.connections.get(c["id"])["status"]
    assert st["state"] == "connected" and st["detail"] == f"Tested with {picked}"


def test_cloud_errors_are_explained(env, monkeypatch):
    _probe_recorder(env, monkeypatch, fail="AccessDeniedException: You don't have access to the model")
    c = env.connections.create("bedrock.api_key", "AWS", {"region": "us-east-1", "api_key": "k"})
    with pytest.raises(env.methods.MethodError, match="not allowed to use this model"):
        asyncio.run(env.connections.test(c["id"]))
    assert env.connections.get(c["id"])["status"]["state"] == "error"
    _probe_recorder(env, monkeypatch, fail="DeploymentNotFound: The API deployment for this resource does not exist (404)")
    a = env.connections.create("azure.openai", "Azure", {"endpoint": "https://r.openai.azure.com", "api_key": "k", "deployments": "gpt-5"})
    with pytest.raises(env.methods.MethodError, match="not found"):
        asyncio.run(env.connections.test(a["id"]))


def test_bedrock_iam_lists_account_models(env, monkeypatch):
    import boto3

    seen = {}

    class Client:
        def list_foundation_models(self, **kw):
            seen["kw"] = kw
            return {"modelSummaries": [
                {"modelId": "anthropic.claude-sonnet-x", "inferenceTypesSupported": ["ON_DEMAND"]},
                {"modelId": "meta.llama-provisioned", "inferenceTypesSupported": ["PROVISIONED"]},
            ]}

        def list_inference_profiles(self, **kw):
            return {"inferenceProfileSummaries": [{"inferenceProfileId": "us.amazon.nova-micro-v1:0"}]}

    class Session:
        def __init__(self, **kw):
            seen["session"] = kw

        def client(self, name):
            return Client()

    monkeypatch.setattr(boto3, "Session", Session)
    calls = _probe_recorder(env, monkeypatch)
    c = env.connections.create("bedrock.iam", "AWS", {"region": "eu-central-1", "access_key_id": "AKIA", "secret_access_key": "s"})
    models = asyncio.run(env.connections.test(c["id"]))
    assert models == ["us.amazon.nova-micro-v1:0", "anthropic.claude-sonnet-x"]  # provisioned-only models left out
    assert seen["kw"] == {"byOutputModality": "TEXT"} and seen["session"]["region_name"] == "eu-central-1"
    assert calls[0]["model"] == "bedrock/us.amazon.nova-micro-v1:0" and calls[0]["aws_access_key_id"] == "AKIA"


def test_entra_token_is_cached(env, monkeypatch):
    posts = []

    def post(url, data=None, timeout=None):
        posts.append((url, data))
        return SimpleNamespace(status_code=200, headers={"content-type": "application/json"}, json=lambda: {"access_token": f"tok{len(posts)}", "expires_in": 3600})

    monkeypatch.setattr(env.cloud.httpx, "post", post)
    provider = env.cloud.azure_token_provider({"tenant_id": "ten", "client_id": "cli", "client_secret": "sec"})
    assert provider() == "tok1" and provider() == "tok1" and len(posts) == 1
    url, data = posts[0]
    assert url == "https://login.microsoftonline.com/ten/oauth2/v2.0/token"
    assert data == {"grant_type": "client_credentials", "client_id": "cli", "client_secret": "sec", "scope": "https://cognitiveservices.azure.com/.default"}


def test_sign_in_with_openrouter(env, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "store", env.store)
    exchanged = {}

    class Exchange:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            exchanged.update(url=url, body=json)
            return SimpleNamespace(status_code=200, json=lambda: {"key": "sk-or-v1-signedin-1234"})

    monkeypatch.setattr(env.cloud.httpx, "AsyncClient", Exchange)

    async def models(v):
        assert v["api_key"] == "sk-or-v1-signedin-1234"
        return ["anthropic/claude-sonnet-5-5", "openai/gpt-5"]

    m = env.methods.METHODS["openrouter.oauth"]
    monkeypatch.setitem(env.methods.METHODS, m.id, dataclasses.replace(m, list_models=models))

    lan = TestClient(main.app, follow_redirects=False)
    c = env.connections.create("openrouter.oauth", "OpenRouter", {})
    assert lan.post(f"/api/connections/{c['id']}/oauth/start").status_code == 403  # same gate as other changes
    with pytest.raises(env.methods.MethodError, match="Not signed in"):
        asyncio.run(env.connections.test(c["id"]))
    local = TestClient(main.app, client=("127.0.0.1", 5000), follow_redirects=False)
    url = local.post(f"/api/connections/{c['id']}/oauth/start").json()["url"]
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert url.startswith("https://openrouter.ai/auth?") and q["code_challenge_method"] == "S256" and q["key_label"] == "Buildplate"
    assert q["callback_url"] == "http://testserver/api/oauth/openrouter/callback"

    # a wrong state is refused; the right one exchanges the code with the PKCE verifier
    assert local.get("/api/oauth/openrouter/callback?state=forged&code=abc").headers["location"] == "/#/settings/oauth-failed"
    r = local.get(f"/api/oauth/openrouter/callback?state={q['state']}&code=the-code")
    assert r.status_code == 303 and r.headers["location"] == "/#/settings/oauth-ok"
    body = exchanged["body"]
    assert exchanged["url"] == "https://openrouter.ai/api/v1/auth/keys" and body["code"] == "the-code"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == q["code_challenge"]
    conn = env.connections.get(c["id"])
    assert conn["secrets"]["api_key"] == "sk-or-v1-signedin-1234" and conn["status"]["state"] == "connected"
    assert "sk-or-v1-signedin-1234" not in json.dumps(env.connections.public_list())
    # the state is single use
    assert local.get(f"/api/oauth/openrouter/callback?state={q['state']}&code=again").headers["location"] == "/#/settings/oauth-failed"
    assert local.get("/api/oauth/openrouter/callback").headers["location"] == "/#/settings/oauth-cancelled"


def test_cloud_env_bootstrap(tmp_path, monkeypatch):
    key = tmp_path / "sa.json"
    key.write_text(json.dumps(SA_KEY))
    e = _load(tmp_path / "d", monkeypatch, env={
        "AWS_ACCESS_KEY_ID": "AKIAENV", "AWS_SECRET_ACCESS_KEY": "secret", "AWS_REGION": "eu-west-1",
        "GOOGLE_APPLICATION_CREDENTIALS": str(key), "VERTEXAI_LOCATION": "us-east5",
        "AZURE_OPENAI_ENDPOINT": "https://r.openai.azure.com", "AZURE_OPENAI_API_KEY": "azkey", "AZURE_OPENAI_DEPLOYMENTS": "gpt-5,gpt-5-mini",
    })
    conns = {c["method"]: c for c in e.store.get()["connections"]}
    assert conns["bedrock.iam"]["fields"] == {"region": "eu-west-1"} and conns["bedrock.iam"]["secrets"]["access_key_id"] == "AKIAENV"
    assert conns["vertex.service_account"]["fields"] == {"project": "maker-123", "location": "us-east5"}
    assert json.loads(conns["vertex.service_account"]["secrets"]["credentials_json"]) == SA_KEY
    assert conns["azure.openai"]["fields"]["deployments"] == "gpt-5,gpt-5-mini" and conns["azure.openai"]["secrets"] == {"api_key": "azkey"}
    raw = (tmp_path / "d" / "settings.json").read_text()
    assert "AKIAENV" not in raw and "PRIVATE KEY" not in raw and "azkey" not in raw  # encrypted at rest


def test_design_request_through_bedrock(env, monkeypatch):
    import litellm

    from app import agent, openscad

    importlib.reload(agent)
    c = env.connections.create("bedrock.api_key", "AWS", {"region": "us-east-1", "api_key": "ABSK"})
    seen = []

    async def acompletion(**kw):
        seen.append(kw)
        raise RuntimeError("stop")

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    events = []

    async def go():
        async for ev in agent.run("a 20 mm cube", connection=c["id"], model="us.anthropic.claude-sonnet-x"):
            events.append(ev)

    asyncio.run(go())
    assert events[0]["billing"] == "cloud" and events[0]["provider"] == "bedrock"
    assert seen[0]["model"] == "bedrock/us.anthropic.claude-sonnet-x" and seen[0]["api_key"] == "ABSK" and seen[0]["aws_region_name"] == "us-east-1"
