import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDPLATE_DATA", str(tmp_path))
    from app import config

    importlib.reload(config)
    from app import settings

    importlib.reload(settings)
    from app import auth_methods, connections, db, parts, providers

    for m in (auth_methods, connections, providers, db, parts):
        importlib.reload(m)
    db.init()
    from app import agent

    importlib.reload(agent)
    return SimpleNamespace(store=settings.store, db=db, parts=parts, agent=agent, connections=connections)


def test_catalog_has_modelling_data(env):
    cat = env.parts.catalog()
    keys = [p["key"] for p in cat["parts"]]
    assert len(keys) == len(set(keys)) > 100
    assert {"screw", "heat-set insert", "nut", "magnet", "bearing"} <= set(cat["categories"])
    m3 = env.parts.catalog_part("screw-shcs-m3x8")
    assert m3["holes"]["clearance_d"] == 3.4 and m3["holes"]["counterbore_d"] == 6.1
    assert env.parts.catalog_part("insert-m3x5.7")["holes"] == {**env.parts.catalog_part("insert-m3x5.7")["holes"], "insert_hole_d": 4.0}
    assert env.parts.catalog_part("bearing-608zz")["dims"] == {"bore": 8, "outer_d": 22, "width": 7}
    assert all(p["holes"] for p in cat["parts"])


def test_inventory_crud_and_links(env):
    P = env.parts
    a = P.create({"catalog_key": "screw-shcs-m3x8", "quantity": 40, "low_stock": 10})
    assert a["name"] == "M3 x 8 socket head cap screw" and a["quantity"] == 40 and not a["low"]
    # adding the same catalog part again tops it up rather than duplicating it
    assert P.create({"catalog_key": "screw-shcs-m3x8", "quantity": 5})["quantity"] == 45 and len(P.list_inventory()) == 1
    assert P.adjust(a["id"], -40)["low"] is True
    assert P.adjust(a["id"], -999)["quantity"] == 0
    custom = P.create({"name": "Brass hinge pin 3 x 40", "category": "pin", "quantity": 4, "notes": "from the hardware store"})
    assert custom["category"] == "pin" and custom["catalog_key"] is None
    assert [p["name"] for p in P.list_inventory("hinge")] == ["Brass hinge pin 3 x 40"]
    for bad in ({"name": ""}, {"name": "x", "quantity": -1}, {"name": "x", "amazon_url": "javascript:alert(1)"}, {"catalog_key": "nope"}):
        with pytest.raises(P.PartsError):
            P.create(bad)

    links = P.get(a["id"])["links"]
    assert links["bambu"] == "https://us.store.bambulab.com/search?q=M3+socket+head+screw"
    assert links["amazon"].startswith("https://www.amazon.com/s?k=M3x8+socket+head+cap+screw")
    env.store.update({"parts": {"store_region": "uk", "amazon_domain": "amazon.co.uk", "amazon_tag": "buildplate-21"}})
    links = P.get(a["id"])["links"]
    assert links["bambu"].startswith("https://uk.store.bambulab.com/search?q=") and links["amazon"].startswith("https://www.amazon.co.uk/")
    assert links["amazon"].endswith("&tag=buildplate-21")
    P.update(a["id"], {"amazon_url": "https://example.com/my-screws", "other_url": "https://shop.example/m3"})
    assert P.get(a["id"])["links"] == {**P.get(a["id"])["links"], "amazon": "https://example.com/my-screws", "other": "https://shop.example/m3"}
    with pytest.raises(ValueError):
        env.store.update({"parts": {"amazon_tag": "bad tag!"}})
    P.delete(custom["id"])
    with pytest.raises(KeyError):
        P.get(custom["id"])


def test_csv_round_trip(env):
    P = env.parts
    P.create({"catalog_key": "magnet-disc-6x3", "quantity": 20})
    P.create({"name": "Hinge pin", "category": "pin", "quantity": 4, "dims": {"diameter": 3}})
    text = P.export_csv()
    assert text.splitlines()[0].startswith("id,catalog_key,category,name")
    # edit quantities, add a catalog part by key only, and one bad row
    text = text.replace(",20,", ",25,") + ",bearing-608zz,,,,,,8,,,,,,,\n,,,,,,,lots,,,,,,,\n"
    result = P.import_csv(text)
    assert result["updated"] == 2 and result["added"] == 1 and len(result["errors"]) == 1
    inv = {p["name"]: p for p in P.list_inventory()}
    assert inv["6 x 3 mm disc magnet"]["quantity"] == 25 and inv["608ZZ ball bearing"]["quantity"] == 8
    assert inv["Hinge pin"]["dims"] == {"diameter": 3}
    with pytest.raises(P.PartsError):
        P.import_csv("just,some,columns\n1,2,3\n")


def test_needs_hardware(env):
    yes = ["A wall mount for my router", "box with a magnetic lid", "bracket with two M3 screws", "spool holder with a 608 bearing",
           "hinged lid using heat-set inserts"]
    no = ["a 20 mm calibration cube", "a vase", "phone stand, snap fit, no screws", "a box with a print-in-place hinge"]
    assert all(env.parts.needs_hardware(p) for p in yes), [p for p in yes if not env.parts.needs_hardware(p)]
    assert not any(env.parts.needs_hardware(p) for p in no), [p for p in no if env.parts.needs_hardware(p)]


def test_bom_status_and_mode_rules(env):
    P = env.parts
    screw = P.create({"catalog_key": "screw-shcs-m3x8", "quantity": 3})
    bom = [{"part": screw["id"], "quantity": 4, "use": "lid"}, {"part": "insert-m3x5.7", "quantity": 4},
           {"part": "magnet-disc-6x3", "quantity": 2}, {"part": "Brass hinge pin", "quantity": 1}, {"part": "", "quantity": 1}]
    res = P.resolve_bom(bom)
    assert [(r["name"], r["status"], r["on_hand"], r["to_buy"], r["source"]) for r in res] == [
        ("M3 x 8 socket head cap screw", "short", 3, 1, "inventory"),
        ("M3 x 5.7 heat-set insert", "buy", 0, 4, "catalog"),
        ("6 x 3 mm disc magnet", "buy", 0, 2, "catalog"),
        ("Brass hinge pin", "buy", 0, 1, "custom"),
    ]
    assert res[1]["holes"]["insert_hole_d"] == 4.0 and res[3]["links"]["amazon"].endswith("k=Brass+hinge+pin")
    assert P.bom_summary(bom) == {"parts": 11, "lines": 4, "to_buy": 4}
    # inventory mode: only stocked parts, within stock
    assert "M3 x 8 socket head cap screw: needs 4, 3 on hand" in P.check_submission("inventory", bom[:1], "")
    assert P.check_submission("inventory", [{"part": "screw-shcs-m3x8", "quantity": 3}], "") is None
    assert "only parts on hand" in P.check_submission("inventory", [{"part": "magnet-disc-6x3", "quantity": 1}], "")
    # no-hardware mode: hardware only with a reason
    assert "no hardware" in P.check_submission("none", bom[:1], "")
    assert P.check_submission("none", bom[:1], "A printed hinge would not hold the weight.") is None
    assert P.check_submission("none", [], "") is None
    assert P.check_submission("order", bom, "") is None


def _fake_model(monkeypatch, script):
    """A scripted LiteLLM: each call returns the next list of (tool, args) calls."""
    import litellm

    calls = iter(script)
    seen = []

    async def acompletion(**kw):
        seen.append(kw)
        tool_calls = [SimpleNamespace(id=f"t{n}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
                      for n, (name, args) in enumerate(next(calls))]
        msg = SimpleNamespace(content="", tool_calls=tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    return seen


@pytest.fixture()
def model(env, monkeypatch):
    from app import openscad

    async def validate(code, params=None):
        return None, {"size_mm": [30.0, 20.0, 10.0], "min_mm": [0, 0, 0], "max_mm": [30, 20, 10], "triangles": 12,
                      "watertight": True, "bodies": 1, "issues": [], "printer": "Test"}

    async def preview(code, params=None, views=None):
        raise openscad.RenderError("no preview in tests", "")

    monkeypatch.setattr(openscad, "validate", validate)
    monkeypatch.setattr(openscad, "preview_sheet", preview)
    c = env.connections.create("ollama.local", "Box", {"base_url": "http://box:11434"})
    env.store.update({"agent": {"connection": c["id"], "model": "qwen3"}})
    return c


def _run(env, prompt, **kw):
    events = []

    async def go():
        async for ev in env.agent.run(prompt, **kw):
            events.append(ev)

    asyncio.run(go())
    return events


def test_question_comes_before_any_model_call(env, model, monkeypatch):
    seen = _fake_model(monkeypatch, [])
    events = _run(env, "A wall bracket held on with two M4 screws")
    assert [e["type"] for e in events] == ["question"]
    q = events[0]
    assert [o["value"] for o in q["options"]] == ["inventory", "order", "none"]
    assert q["options"][0]["disabled"] is True  # empty inventory
    assert seen == [] and env.db.list_runs()[1] == 0  # no tokens, no run recorded
    env.parts.create({"catalog_key": "screw-shcs-m4x16", "quantity": 10})
    assert _run(env, "A wall bracket held on with two M4 screws")[0]["options"][0]["disabled"] is False
    # a request that does not imply hardware goes straight to the model
    _fake_model(monkeypatch, [[("submit_design", {"code": "cube(10);", "title": "Cube", "summary": "A cube."})]])
    assert _run(env, "a 10 mm cube")[-1]["type"] == "done"


def test_inventory_mode_end_to_end(env, model, monkeypatch):
    P = env.parts
    screw = P.create({"catalog_key": "screw-shcs-m4x16", "quantity": 2})
    code = "difference() { cube([30, 20, 10]); }"
    seen = _fake_model(monkeypatch, [
        [("list_parts", {"query": "M4"})],
        # asks for more screws than are on hand: refused
        [("submit_design", {"code": code, "title": "Bracket", "summary": "x", "parts": [{"part": screw["id"], "quantity": 4}]})],
        [("submit_design", {"code": code, "title": "Bracket", "summary": "Two M4 screws.",
                            "parts": [{"part": screw["id"], "quantity": 2, "use": "wall screws"}]})],
    ])
    events = _run(env, "A wall bracket held on with M4 screws", hardware_mode="inventory")
    done = events[-1]
    assert done["type"] == "done" and done["hardware_mode"] == "inventory"
    # the system prompt carried the inventory with hole sizes, and the tools included list_parts
    system = seen[0]["messages"][0]["content"]
    assert "use ONLY parts from the user's inventory" in system and f"{screw['id']}: M4 x 16 socket head cap screw" in system
    assert "clearance_d=4.5" in system and any(t["function"]["name"] == "list_parts" for t in seen[0]["tools"])
    tool_results = [m["content"] for m in seen[2]["messages"] if m.get("role") == "tool"]
    assert any("M4 x 16 socket head cap screw" in t for t in tool_results)
    assert any("Not saved: this request must use only parts on hand" in t for t in tool_results)
    assert [(b["name"], b["quantity"], b["status"]) for b in done["bom"]] == [("M4 x 16 socket head cap screw", 2, "have")]

    # saved with the version; a follow-up keeps the mode without asking again
    d = env.db.get_design(done["design_id"])
    v = d["versions"][-1]
    assert json.loads(v["bom"]) == [{"part": screw["id"], "quantity": 2, "use": "wall screws"}] and v["hardware_mode"] == "inventory"
    _fake_model(monkeypatch, [[("submit_design", {"code": code, "title": "Bracket", "summary": "Taller.",
                                                  "parts": [{"part": screw["id"], "quantity": 2}]})]])
    follow = _run(env, "make it taller, keep the M4 screws", design_id=done["design_id"])
    assert follow[-1]["type"] == "done" and follow[-1]["hardware_mode"] == "inventory"

    # mark as built takes the parts out of stock; undo puts them back
    built = P.mark_built(done["design_id"], 1)
    assert built["taken"] == [{"part_id": screw["id"], "name": "M4 x 16 socket head cap screw", "quantity": 2}]
    assert P.get(screw["id"])["quantity"] == 0
    with pytest.raises(P.PartsError):
        P.mark_built(done["design_id"], 1)
    P.unmark_built(done["design_id"], 1)
    assert P.get(screw["id"])["quantity"] == 2


def test_no_hardware_mode_needs_a_reason(env, model, monkeypatch):
    seen = _fake_model(monkeypatch, [
        [("submit_design", {"code": "cube(5);", "title": "Lid", "summary": "x", "parts": [{"part": "magnet-disc-6x3", "quantity": 2}]})],
        [("submit_design", {"code": "cube(5);", "title": "Lid", "summary": "Snap lid.", "parts": []})],
    ])
    done = _run(env, "box with a magnetic lid", hardware_mode="none")[-1]
    assert done["type"] == "done" and done["bom"] == [] and "WITHOUT hardware" in seen[0]["messages"][0]["content"]


def test_api_and_mcp(env, model, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main, mcp_server

    importlib.reload(mcp_server)
    monkeypatch.setattr(main, "store", env.store)
    client = TestClient(main.app)
    assert len(client.get("/api/parts/catalog").json()["parts"]) > 100
    p = client.post("/api/parts", json={"catalog_key": "magnet-disc-10x3", "quantity": 12}).json()
    assert client.post(f"/api/parts/{p['id']}/adjust", json={"delta": -2}).json()["quantity"] == 10
    assert client.patch(f"/api/parts/{p['id']}", json={"low_stock": 10}).json()["low"] is True
    assert client.post("/api/parts", json={"name": ""}).status_code == 400
    assert client.patch("/api/parts/p_missing", json={"quantity": 1}).status_code == 404
    csv_text = client.get("/api/parts/export.csv").text
    assert "10 x 3 mm disc magnet" in csv_text
    assert client.post("/api/parts/import", json={"csv": csv_text}).json() == {"added": 0, "updated": 1, "errors": []}

    _fake_model(monkeypatch, [[("submit_design", {"code": "cube(5);", "title": "Fridge note holder", "summary": "x",
                                                  "parts": [{"part": p["id"], "quantity": 2}, {"part": "screw-shcs-m3x8", "quantity": 1}]})]])
    lines = [json.loads(x) for x in client.post("/api/generate", json={"prompt": "fridge magnet note holder", "hardware_mode": "order"}).text.splitlines()]
    did = lines[-1]["design_id"]
    bom = client.get(f"/api/designs/{did}/bom").json()
    assert [(b["name"], b["status"]) for b in bom["parts"]] == [("10 x 3 mm disc magnet", "have"), ("M3 x 8 socket head cap screw", "buy")]
    listed = next(d for d in client.get("/api/designs").json()["designs"] if d["id"] == did)
    assert listed["hardware"] == {"parts": 3, "lines": 2, "to_buy": 1}
    assert client.post(f"/api/designs/{did}/versions/1/built").json()["built"]["taken"][0]["quantity"] == 2
    assert client.post(f"/api/designs/{did}/versions/1/built").status_code == 400
    assert client.delete(f"/api/designs/{did}/versions/1/built").json() == {"ok": True}
    assert client.get("/api/designs/x/bom").status_code == 404

    q = asyncio.run(mcp_server.generate_design("a shelf bracket with two M4 screws"))
    assert q["question"].startswith("This design looks like it needs hardware") and len(q["options"]) == 3
    added = asyncio.run(mcp_server.add_part(quantity=6, catalog_key="bearing-608zz"))
    assert added["quantity"] == 6
    assert asyncio.run(mcp_server.update_part_quantity(added["id"], delta=-1))["quantity"] == 5
    listing = asyncio.run(mcp_server.list_parts(query="608", include_catalog=True))
    assert listing["inventory"][0]["holes"]["seat_d"] == 22.15
    assert asyncio.run(mcp_server.get_design(did))["bill_of_materials"][0]["name"] == "10 x 3 mm disc magnet"
