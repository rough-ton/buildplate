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
    from app import connections

    importlib.reload(connections)
    store = sf["settings"].store
    c = connections.create("openai.api_key", "OpenAI", {"api_key": "sk-test-abcdefgh1234"})
    raw = sf["config"].SETTINGS_FILE.read_text()
    assert "sk-test-abcdefgh1234" not in raw
    assert connections.get(c["id"])["secrets"]["api_key"] == "sk-test-abcdefgh1234"
    pub = store.public()
    assert "sk-test-abcdefgh1234" not in json.dumps(pub)
    assert pub["connections"][0]["secrets"]["api_key"] == {"set": True, "hint": "****1234"}
    assert pub["providers"]["openai"]["api_key_hint"] == "****1234"


def test_settings_apply_without_restart(sf):
    store = sf["settings"].store
    store.update({"printer": {"bed_x": 180}})
    assert store.get()["printer"]["bed_x"] == 180.0
    data = json.loads(sf["config"].SETTINGS_FILE.read_text())
    data["printers"][0]["bed_x"] = 300
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


def test_printer_profiles(sf):
    settings = sf["settings"]
    store = settings.store
    s = store.get()
    assert len(s["printers"]) == 1 and s["printer"]["id"] == s["active_printer"]
    printers = s["printers"] + [{"id": "mini", "name": "A1 mini", "bed_x": 180, "bed_y": 180, "bed_z": 180, "nozzle": 0.4}]
    store.update({"printers": printers, "active_printer": "mini"})
    assert store.get()["printer"]["bed_x"] == 180.0
    # legacy single-printer patch edits the active profile
    store.update({"printer": {"bed_z": 170}})
    assert store.get()["printer"]["bed_z"] == 170.0 and len(store.get()["printers"]) == 2
    with pytest.raises(ValueError):
        store.update({"printers": []})
    # removing the active profile falls back to the first one
    store.update({"printers": [p for p in store.get()["printers"] if p["id"] != "mini"]})
    assert store.get()["printer"]["id"] != "mini"


def test_legacy_settings_migrate(sf):
    cfg = sf["config"]
    cfg.SETTINGS_FILE.write_text(json.dumps({"printer": {"name": "Old MK4", "bed_x": 250, "bed_y": 210, "bed_z": 220}}))
    import os, time

    os.utime(cfg.SETTINGS_FILE, (time.time() + 5, time.time() + 5))
    s = sf["settings"].store.get()
    assert s["printer"]["name"] == "Old MK4" and s["printer"]["bed_y"] == 210
    assert "printer" not in json.loads(cfg.SETTINGS_FILE.read_text())


@pytest.mark.skipif(__import__("shutil").which("openscad") is None, reason="OpenSCAD not installed")
def test_checks_follow_active_printer(sf):
    store, osc = sf["settings"].store, sf["openscad"]
    store.update({"printers": [{"id": "big", "name": "Big", "bed_x": 300, "bed_y": 300, "bed_z": 300}, {"id": "small", "name": "Small", "bed_x": 180, "bed_y": 180, "bed_z": 180}], "active_printer": "big"})
    _, big = asyncio.run(osc.validate("cube([200, 50, 10]);"))
    store.update({"active_printer": "small"})
    _, small = asyncio.run(osc.validate("cube([200, 50, 10]);"))
    assert not big["issues"] and big["printer"] == "Big"
    assert any("build volume" in i for i in small["issues"]) and small["printer"] == "Small"


def test_printer_catalog():
    from app import printers

    cat = printers.catalog()
    assert len(cat["printers"]) > 200 and len(cat["vendors"]) > 30
    names = {p["display"] for p in cat["printers"]}
    assert {"Bambu Lab P1S", "Prusa MK4S"} <= names
    p1s = next(p for p in cat["printers"] if p["display"] == "Bambu Lab P1S")
    prof = printers.profile_from_catalog(p1s["key"], 0.6)
    assert prof["name"] == "Bambu Lab P1S 0.6 nozzle" and prof["nozzle"] == 0.6 and prof["bed_x"] == 256
    assert prof["type"] == "corexy_enclosed"
    types = {p["display"]: p["type"] for p in cat["printers"]}
    assert types["Bambu Lab A1 mini"] == "cantilever" and types["Prusa MK4S"] == "bedslinger"
    assert all(p["type"] == "delta" for p in cat["printers"] if p["shape"] == "round")
    assert any(p["shape"] == "round" for p in cat["printers"])


@pytest.mark.skipif(__import__("shutil").which("openscad") is None, reason="OpenSCAD not installed")
def test_round_bed_check(sf):
    store, osc = sf["settings"].store, sf["openscad"]
    store.update({"printers": [{"id": "delta", "name": "Delta", "shape": "round", "bed_x": 200, "bed_y": 200, "bed_z": 300}], "active_printer": "delta"})
    _, ok = asyncio.run(osc.validate("translate([-60, -60, 0]) cube([120, 120, 10]);"))
    _, bad = asyncio.run(osc.validate("translate([-80, -80, 0]) cube([160, 160, 10]);"))
    assert not ok["issues"]
    assert any("round bed" in i for i in bad["issues"])


def test_openscad_env_uses_writable_home(sf, monkeypatch):
    monkeypatch.setenv("HOME", "/root")
    env = sf["openscad"]._env()
    assert env["HOME"].startswith(str(sf["config"].DATA_DIR))
    assert env["XDG_CONFIG_HOME"].startswith(env["HOME"])


def test_3mf_splits_bodies_into_objects(sf):
    import zipfile

    from lxml import etree

    osc = sf["openscad"]
    code = (
        "cube([20, 20, 5]);\n"
        "translate([40, 0, 0]) cube([10, 10, 5]);\n"
        # hollow cube: the internal void must stay part of its body
        "translate([0, 40, 0]) difference() { cube(20); translate([5, 5, 5]) cube(10); }\n"
    )
    p = asyncio.run(osc.export_3mf(code))
    with zipfile.ZipFile(p) as z:
        model = next(n for n in z.namelist() if n.endswith(".model"))
        root = etree.fromstring(z.read(model))
    ns = {"m": root.nsmap.get(None)}
    meshes = root.findall(".//m:object/m:mesh", ns)
    items = root.findall(".//m:build/m:item", ns)
    assert len(meshes) == 3
    assert len(items) == 3


def test_3mf_single_body_is_one_object(sf):
    import zipfile

    osc = sf["openscad"]
    p = asyncio.run(osc.export_3mf("cube(10);"))
    with zipfile.ZipFile(p) as z:
        model = next(n for n in z.namelist() if n.endswith(".model"))
        assert z.read(model).count(b"<mesh") == 1
