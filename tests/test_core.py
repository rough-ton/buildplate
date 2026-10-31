import asyncio
import importlib
import json

import pytest


@pytest.fixture()
def sf(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDPLATE_DATA", str(tmp_path))
    from app import config

    importlib.reload(config)
    from app import settings

    importlib.reload(settings)
    from app import db, openscad, providers

    for m in (db, openscad, providers):
        importlib.reload(m)
    db.init()
    return {"config": config, "settings": settings, "db": db, "openscad": openscad}


def test_secrets_are_encrypted_and_masked(sf):
    store = sf["settings"].store
    store.update({"providers": {"openai": {"api_key": "sk-test-abcdefgh1234", "enabled": True}}})
    raw = sf["config"].SETTINGS_FILE.read_text()
    assert "sk-test-abcdefgh1234" not in raw
    assert store.get()["providers"]["openai"]["api_key"] == "sk-test-abcdefgh1234"
    pub = store.public()["providers"]["openai"]
    assert "api_key" not in pub and pub["api_key_hint"] == "****1234"


def test_settings_apply_without_restart(sf):
    store = sf["settings"].store
    store.update({"printer": {"bed_x": 180}})
    assert store.get()["printer"]["bed_x"] == 180.0
    data = json.loads(sf["config"].SETTINGS_FILE.read_text())
    data["printer"]["bed_x"] = 300
    sf["config"].SETTINGS_FILE.write_text(json.dumps(data))
    import os, time

    os.utime(sf["config"].SETTINGS_FILE, (time.time() + 5, time.time() + 5))
    assert store.get()["printer"]["bed_x"] == 300


def test_password(sf):
    store = sf["settings"].store
    assert not store.password_enabled()
    store.set_password("correct horse")
    assert store.check_password("correct horse") and not store.check_password("nope")


def test_parameter_parsing(sf):
    code = """/* [Size] */
// Box width
width = 40; // [10:1:120]
lid = true;
style = "round"; // [round:Rounded, square:Square]
/* [Hidden] */
$fn = 48;
cube(width);
"""
    params = {p["name"]: p for p in sf["openscad"].parse_parameters(code)}
    assert params["width"]["control"] == "slider" and params["width"]["max"] == 120
    assert params["width"]["label"] == "Box width" and params["width"]["section"] == "Size"
    assert params["lid"]["control"] == "checkbox"
    assert params["style"]["options"][1] == {"value": "square", "label": "Square"}
    assert "$fn" not in params


@pytest.mark.skipif(__import__("shutil").which("openscad") is None, reason="OpenSCAD not installed")
def test_render_and_validate(sf):
    osc = sf["openscad"]
    res, stats = asyncio.run(osc.validate("w = 20;\ncube([w, 10, 5]);", {"w": 30}))
    assert stats["size_mm"] == [30.0, 10.0, 5.0]
    assert stats["watertight"] and not stats["issues"]
    _, floating = asyncio.run(osc.validate("translate([0,0,3]) cube(5);"))
    assert any("floating" in i for i in floating["issues"])
    with pytest.raises(osc.RenderError):
        asyncio.run(osc.render("cube(", "stl"))


def test_usage_accounting(sf):
    db = sf["db"]
    rid = db.start_run(None, "a hook", "anthropic", "m", "studio")
    u = {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 5, "cache_write_tokens": 0, "cost_usd": 0.01, "cost_known": True}
    db.add_step(rid, 1, u, 900, ["check_design"])
    db.add_step(rid, 2, u, 800, ["submit_design"])
    db.finish_run(rid, "ok")
    run = db.get_run(rid)
    assert run["input_tokens"] == 200 and run["output_tokens"] == 40 and run["iterations"] == 2
    assert abs(run["cost_usd"] - 0.02) < 1e-9
    assert db.usage_summary(1)["totals"]["runs"] == 1
