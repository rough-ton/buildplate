"""Tuned parameter values are kept with a design version (issue #17)."""
import importlib
from types import SimpleNamespace

import pytest

CODE = """/* [Size] */
// Inside width
width = 60; // [20:1:200]
// Lid style
style = "snap"; // [snap:Snap fit, slide:Sliding]
rounded = true;
cube([width, 10, 10]);
"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDPLATE_DATA", str(tmp_path))
    from app import config

    importlib.reload(config)
    from app import settings

    importlib.reload(settings)
    from app import db

    importlib.reload(db)
    db.init()
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "store", settings.store)
    did = db.create_design("Box", "manual")
    db.add_version(did, CODE, "v1", {}, None)
    db.add_version(did, CODE.replace("60", "70"), "v2", {}, None)
    return SimpleNamespace(http=TestClient(main.app), main=main, did=did)


def test_values_are_saved_per_version(client):
    http, did = client.http, client.did
    assert http.get(f"/api/designs/{did}").json()["param_values"] == {}

    r = http.put(f"/api/designs/{did}/versions/2/params", json={"values": {"width": 95, "style": "slide", "rounded": False}})
    assert r.status_code == 200 and r.json()["param_values"] == {"width": 95, "style": "slide", "rounded": False}

    d = http.get(f"/api/designs/{did}").json()
    assert d["param_values"] == {"width": 95, "style": "slide", "rounded": False}
    assert [v["param_values"] for v in d["versions"]] == [{}, {"width": 95, "style": "slide", "rounded": False}]
    assert http.get(f"/api/designs/{did}/versions/2").json()["param_values"]["width"] == 95
    assert http.get(f"/api/designs/{did}/versions/1").json()["param_values"] == {}

    # Reset clears them.
    assert http.put(f"/api/designs/{did}/versions/2/params", json={"values": {}}).json()["param_values"] == {}
    assert http.get(f"/api/designs/{did}").json()["param_values"] == {}


def test_values_are_checked(client):
    http, did = client.http, client.did
    # Names the code doesn't declare are dropped, not stored.
    r = http.put(f"/api/designs/{did}/versions/2/params", json={"values": {"width": 80, "nope": 1}})
    assert r.json()["param_values"] == {"width": 80}
    assert http.put(f"/api/designs/{did}/versions/2/params", json={"values": {"width": [1, 2]}}).status_code == 400
    assert http.put(f"/api/designs/{did}/versions/2/params", json={"values": {"style": "x" * 501}}).status_code == 400
    assert http.put(f"/api/designs/{did}/versions/9/params", json={"values": {}}).status_code == 404
    assert http.put("/api/designs/missing/versions/1/params", json={"values": {}}).status_code == 404


def test_download_uses_saved_values(client, monkeypatch, tmp_path):
    http, did, main = client.http, client.did, client.main
    seen = []
    out = tmp_path / "part.stl"
    out.write_text("solid x\nendsolid x\n")

    async def fake_render(code, fmt, overrides=None, view="iso"):
        seen.append(overrides)
        return SimpleNamespace(path=out, warnings=[])

    monkeypatch.setattr(main.openscad, "render", fake_render)
    http.put(f"/api/designs/{did}/versions/2/params", json={"values": {"width": 95}})
    assert http.get(f"/api/designs/{did}/download?format=stl").status_code == 200
    assert seen[-1] == {"width": 95}
    # Explicit values (what Studio sends) still win, and other versions use their own.
    http.get(f"/api/designs/{did}/download?format=stl&params=%7B%22width%22%3A30%7D")
    assert seen[-1] == {"width": 30}
    http.get(f"/api/designs/{did}/download?format=stl&version=1")
    assert seen[-1] is None
    # The .scad download is the source as written.
    assert "width = 70;" in http.get(f"/api/designs/{did}/download?format=scad").text
