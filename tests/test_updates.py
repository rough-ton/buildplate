"""Settings shows whether a newer release is on GitHub (issue #35). GitHub is faked."""
import asyncio
import importlib
from types import SimpleNamespace

import httpx
import pytest


@pytest.fixture()
def up(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDPLATE_DATA", str(tmp_path))
    monkeypatch.setenv("BUILDPLATE_VERSION", "0.6.0")
    from app import config

    importlib.reload(config)
    from app import settings

    importlib.reload(settings)
    from app import updates

    importlib.reload(updates)
    gh = SimpleNamespace(calls=0, reply=(200, {"tag_name": "v0.6.1", "html_url": "https://github.com/rough-ton/buildplate/releases/tag/v0.6.1"}), requests=[])

    def handler(request):
        gh.calls += 1
        gh.requests.append(request)
        status, body = gh.reply
        if isinstance(body, Exception):
            raise body
        return httpx.Response(status, json=body) if not isinstance(body, str) else httpx.Response(status, text=body)

    real = httpx.AsyncClient

    def client(**kw):
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(updates.httpx, "AsyncClient", client)
    return SimpleNamespace(updates=updates, config=config, settings=settings, gh=gh, monkeypatch=monkeypatch)


def status(up, enabled=True):
    return asyncio.run(up.updates.status(enabled))


def test_newer_release_links_to_it(up):
    s = status(up)
    assert s == {"current": "0.6.0", "checked": True, "latest": "0.6.1", "update_available": True,
                 "url": "https://github.com/rough-ton/buildplate/releases/tag/v0.6.1"}
    req = up.gh.requests[0]
    assert str(req.url) == "https://api.github.com/repos/rough-ton/buildplate/releases/latest"
    assert req.headers["user-agent"] == "Buildplate/0.6.0"  # nothing else identifies the install


@pytest.mark.parametrize("tag", ["v0.6.0", "0.6.0", "v0.5.9"])
def test_same_or_older_release_is_up_to_date(up, tag):
    """A local build newer than the latest release never reports an update."""
    up.gh.reply = (200, {"tag_name": tag, "html_url": f"https://github.com/rough-ton/buildplate/releases/tag/{tag}"})
    s = status(up)
    assert s["checked"] and s["latest"] == tag.lstrip("v") and not s["update_available"] and s["url"] is None


def test_compares_numbers_not_text(up):
    up.monkeypatch.setattr(up.config, "VERSION", "0.9.0")
    up.gh.reply = (200, {"tag_name": "v0.10.0", "html_url": "https://github.com/rough-ton/buildplate/releases/tag/v0.10.0"})
    assert status(up)["update_available"]


@pytest.mark.parametrize("reply", [
    (200, {"tag_name": "nightly"}),
    (200, {"tag_name": "v1.0.0-rc1"}),
    (200, ["not", "an", "object"]),
    (200, "<html>not json</html>"),
    (403, {"message": "API rate limit exceeded"}),
    (404, {"message": "Not Found"}),
    (0, httpx.ConnectError("offline")),
    (0, httpx.ReadTimeout("slow")),
])
def test_no_usable_answer_shows_nothing(up, reply):
    up.gh.reply = reply
    s = status(up)
    assert s["checked"] and s["latest"] is None and not s["update_available"] and s["url"] is None


def test_odd_release_link_falls_back_to_github(up):
    up.gh.reply = (200, {"tag_name": "v0.7.0", "html_url": "https://evil.example/x"})
    assert status(up)["url"] == "https://github.com/rough-ton/buildplate/releases/tag/v0.7.0"


@pytest.mark.parametrize("version", ["dev", "edge-0123456789abcdef", "", "0.6"])
def test_builds_without_a_release_version_skip_the_check(up, version):
    up.monkeypatch.setattr(up.config, "VERSION", version)
    s = status(up)
    assert not s["checked"] and s["latest"] is None and up.gh.calls == 0


def test_turned_off_skips_the_check(up):
    s = status(up, enabled=False)
    assert not s["checked"] and up.gh.calls == 0


def test_answers_are_cached(up):
    clock = SimpleNamespace(t=1000.0)
    up.monkeypatch.setattr(up.updates.time, "monotonic", lambda: clock.t)
    for _ in range(3):
        status(up)
    assert up.gh.calls == 1
    clock.t += up.updates.CACHE_SECONDS + 1
    status(up)
    assert up.gh.calls == 2
    # a failure is retried sooner than a success, but not on every page load
    up.updates.clear_cache()
    up.gh.reply = (0, httpx.ConnectError("offline"))
    status(up)
    status(up)
    assert up.gh.calls == 3
    clock.t += up.updates.RETRY_SECONDS + 1
    up.gh.reply = (200, {"tag_name": "v0.6.1", "html_url": "https://github.com/rough-ton/buildplate/releases/tag/v0.6.1"})
    assert status(up)["update_available"] and up.gh.calls == 4


def test_endpoint_and_setting(up):
    from fastapi.testclient import TestClient

    from app import main

    up.monkeypatch.setattr(main, "store", up.settings.store)
    client = TestClient(main.app, client=("127.0.0.1", 5000))
    assert up.settings.store.get()["updates"]["check"] is True
    r = client.get("/api/system/update").json()
    assert r["update_available"] and r["latest"] == "0.6.1"
    assert client.patch("/api/settings", json={"updates": {"check": False}}).json()["updates"]["check"] is False
    up.updates.clear_cache()
    calls = up.gh.calls
    r = client.get("/api/system/update").json()
    assert not r["checked"] and r["latest"] is None and up.gh.calls == calls
    # anything that isn't clearly "off" keeps it on
    assert client.patch("/api/settings", json={"updates": {"check": "yes"}}).json()["updates"]["check"] is True


def test_dev_is_the_default_version(monkeypatch):
    monkeypatch.delenv("BUILDPLATE_VERSION", raising=False)
    from app import config

    importlib.reload(config)
    assert config.VERSION == "dev"
    monkeypatch.setenv("BUILDPLATE_VERSION", "0.6.1")
    importlib.reload(config)
    assert config.VERSION == "0.6.1"
