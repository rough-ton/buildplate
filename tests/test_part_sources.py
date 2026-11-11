"""Parts catalog sources (issue #30): Bambu's Maker Supply (the bundled catalog, on by
default) and McMaster-Carr (live lookups by part number through its API, off until a
login is saved). McMaster-Carr's API is faked with httpx.MockTransport."""

import asyncio
import base64
import datetime
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest

PN = "91290A115"
PRODUCT = {
    "PartNumber": PN,
    "ProductStatus": "Active",
    "FamilyDescription": "Black-Oxide Alloy Steel Socket Head Screw",
    "DetailDescription": "M3 x 0.5 mm Thread, 8 mm Long",
    "Specifications": [
        {"Attribute": "Thread Size", "Values": ["M3 x 0.5 mm"]},
        {"Attribute": "Length", "Values": ["8 mm"]},
        {"Attribute": "Head Type", "Values": ["Socket"]},
        {"Attribute": "Material", "Values": ["Black-Oxide Alloy Steel"]},
    ],
    "Links": [],
}


def _pfx(password: str = "certpass") -> str:
    """A self-signed client certificate as base64 PKCS#12, like McMaster-Carr's .pfx."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "buildplate-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    raw = pkcs12.serialize_key_and_certificates(b"mcm", key, cert, None, serialization.BestAvailableEncryption(password.encode()))
    return base64.b64encode(raw).decode()


class FakeMcMaster:
    """McMaster-Carr's API: login, then products by part number, after subscribing."""

    def __init__(self):
        self.calls = []
        self.subscribed = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/v1/login":
            body = json.loads(request.content)
            if (body["UserName"], body["Password"]) != ("maker", "s3cret"):
                return httpx.Response(401, json={"ErrorDescription": "Invalid credentials"})
            return httpx.Response(200, json={"AuthToken": "tok-1", "ExpirationTS": "2099-01-01"})
        if request.headers.get("authorization") != "Bearer tok-1":
            return httpx.Response(401)
        if request.method == "PUT" and path == "/v1/products":
            pn = json.loads(request.content)["URL"].rsplit("/", 1)[-1]
            if pn != PN:
                return httpx.Response(404, json={"ErrorDescription": "Not found"})
            self.subscribed.add(pn)
            return httpx.Response(201, json=PRODUCT)
        if request.method == "GET" and path.startswith("/v1/products/"):
            pn = path.rsplit("/", 1)[-1]
            if pn not in self.subscribed:
                return httpx.Response(403)
            return httpx.Response(200, json=PRODUCT)
        return httpx.Response(404)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDPLATE_DATA", str(tmp_path))
    from app import config

    importlib.reload(config)
    from app import settings

    importlib.reload(settings)
    from app import auth_methods, connections, db, mcmaster, parts, providers

    for m in (auth_methods, connections, providers, db, mcmaster, parts):
        importlib.reload(m)
    db.init()
    fake = FakeMcMaster()
    monkeypatch.setattr(mcmaster, "_transport", httpx.MockTransport(fake))
    return SimpleNamespace(store=settings.store, settings=settings, db=db, parts=parts, mcmaster=mcmaster, fake=fake, config=config)


def _connect(env):
    creds = {"username": "maker", "password": "s3cret", "certificate": _pfx(), "certificate_password": "certpass"}
    asyncio.run(env.mcmaster.test(creds))
    env.store.set_mcmaster("maker", {k: v for k, v in creds.items() if k != "username"}, enabled=True)


def test_defaults_bambu_on_mcmaster_off(env):
    P = env.parts
    assert P.enabled_sources() == ["bambu"]
    view = P.catalog_view()
    assert {s["id"]: s["enabled"] for s in view["sources"]} == {"bambu": True, "mcmaster": False}
    assert len(view["parts"]) > 100 and {p["source"] for p in view["parts"]} == {"bambu"}
    # McMaster-Carr cannot be switched on without a login
    env.store.update({"parts": {"sources": {"mcmaster": True}}})
    assert P.enabled_sources() == ["bambu"]
    pub = env.store.public()["parts"]
    assert pub["sources"] == {"bambu": True, "mcmaster": False}
    assert pub["mcmaster"] == {"username": "", "configured": False, "checked_at": 0}


def test_mcmaster_login_is_encrypted_masked_and_checked(env):
    with pytest.raises(env.mcmaster.McMasterError, match="didn't accept"):
        asyncio.run(env.mcmaster.test({"username": "maker", "password": "wrong", "certificate": "x"}))
    with pytest.raises(env.mcmaster.McMasterError, match="client certificate"):
        asyncio.run(env.mcmaster.test({"username": "maker", "password": "s3cret"}))
    with pytest.raises(env.mcmaster.McMasterError, match="certificate"):
        env.mcmaster.load_certificate(_pfx(), "wrong-password")
    chain, key = env.mcmaster.load_certificate(_pfx(), "certpass")
    assert chain.startswith(b"-----BEGIN CERTIFICATE") and b"PRIVATE KEY" in key

    _connect(env)
    assert env.parts.enabled_sources() == ["bambu", "mcmaster"]
    disk = env.config.SETTINGS_FILE.read_text()
    assert "s3cret" not in disk and "certpass" not in disk
    pub = env.store.public()
    assert pub["parts"]["mcmaster"]["configured"] is True and pub["parts"]["mcmaster"]["username"] == "maker"
    assert "s3cret" not in json.dumps(pub) and "secrets" not in pub["parts"]["mcmaster"]
    # a whole-settings save from the browser (with the masked view) keeps the login
    env.store.update({"parts": pub["parts"]})
    assert env.store.get()["parts"]["mcmaster"]["secrets"]["password"] == "s3cret"


def test_mcmaster_lookup_add_and_disable(env):
    P = env.parts
    with pytest.raises(P.PartsError, match="Turn on McMaster-Carr"):
        asyncio.run(P.lookup(PN))
    _connect(env)
    for query in (PN, f"https://www.mcmaster.com/{PN}/", f"https://www.mcmaster.com/{PN.lower()}"):
        assert P.mcmaster.parse_ref(query) == PN
    for not_a_part in ("10x3", "m3x8", "608zz", "insert-m3x5.7", "https://example.com/91290A115"):
        assert P.mcmaster.parse_ref(not_a_part) is None, not_a_part
    found = asyncio.run(P.lookup(f"https://www.mcmaster.com/{PN}/"))["parts"]
    part = found[0]
    assert (part["key"], part["source"], part["source_ref"], part["category"], part["size"]) == (f"mcm:{PN}", "mcmaster", PN, "screw", "M3 x 8")
    # the agent gets hole sizes from the matching standard part
    assert part["holes"]["clearance_d"] == 3.4 and part["holes"]["counterbore_d"] == 6.1
    assert ("PUT", "/v1/products") in env.fake.calls
    # cached: the next lookup and searches do not call the API
    n = len(env.fake.calls)
    assert asyncio.run(P.lookup(PN))["parts"][0]["key"] == f"mcm:{PN}" and len(env.fake.calls) == n
    assert [p["key"] for p in P.search_catalog("black-oxide socket") if p["source"] == "mcmaster"] == [f"mcm:{PN}"]
    assert any(p["key"] == f"mcm:{PN}" for p in P.catalog_view()["parts"])
    assert f"mcm:{PN}" in P.search_for_agent("black-oxide")
    with pytest.raises(P.PartsError, match="no part"):
        asyncio.run(P.lookup("12345A999"))
    # a name that matches nothing explains how to look McMaster-Carr parts up
    res = asyncio.run(P.lookup("unobtainium widget"))
    assert res["parts"] == [] and "part number" in res["message"] and res["search_url"].startswith("https://www.mcmaster.com/")

    item = P.create({"catalog_key": f"mcm:{PN}", "quantity": 25})
    assert (item["source"], item["source_ref"], item["source_label"]) == ("mcmaster", PN, "McMaster-Carr")
    assert item["links"]["mcmaster"] == f"https://www.mcmaster.com/{PN}/" and "bambu" not in item["links"]
    bom = P.resolve_bom([{"part": f"mcm:{PN}", "quantity": 4}])[0]
    assert (bom["source"], bom["catalog_source"], bom["source_ref"]) == ("inventory", "mcmaster", PN)

    # turned off: no McMaster-Carr parts in search or add-from-catalog; stock keeps its links
    env.store.update({"parts": {"sources": {"mcmaster": False}}})
    assert P.enabled_sources() == ["bambu"]
    assert not [p for p in P.catalog_view()["parts"] if p["source"] == "mcmaster"]
    agent_view = P.search_for_agent("black-oxide")
    assert item["id"] in agent_view and f"mcm:{PN}" not in agent_view  # stock yes, catalog no
    with pytest.raises(P.PartsError):
        asyncio.run(P.lookup(PN))
    P.delete(item["id"])
    with pytest.raises(P.PartsError, match="turned off"):
        P.create({"catalog_key": f"mcm:{PN}", "quantity": 1})


def test_bambu_off_custom_parts_still_work(env):
    P = env.parts
    env.store.update({"parts": {"sources": {"bambu": False}}})
    assert P.enabled_sources() == [] and P.catalog_view()["parts"] == []
    assert "insert-m3x5.7" not in P.search_for_agent("insert")
    with pytest.raises(P.PartsError, match="No parts catalog"):
        asyncio.run(P.lookup("m3 insert"))
    with pytest.raises(P.PartsError, match="turned off"):
        P.create({"catalog_key": "insert-m3x5.7", "quantity": 10})
    custom = P.create({"name": "Brass hinge pin 3 x 40", "category": "pin", "quantity": 4})
    assert custom["source"] == "custom" and set(custom["links"]) == {"amazon"}
    env.store.update({"parts": {"sources": {"bambu": True}}})
    assert set(P.get(custom["id"])["links"]) == {"bambu", "amazon"}


def test_bambu_direct_entry(env):
    P = env.parts
    assert asyncio.run(P.lookup("insert-m3x5.7"))["parts"][0]["key"] == "insert-m3x5.7"
    url = "https://us.store.bambulab.com/products/m3-heat-set-insert?variant=123"
    res = asyncio.run(P.lookup(url))
    keys = [p["key"] for p in res["parts"]]
    assert "insert-m3x5.7" in keys and res["parts"][-1]["link_only"] is True
    link = res["parts"][-1]
    assert link["name"] == "M3 heat set insert" and link["category"] == "heat-set insert"
    assert link["source_ref"] == "https://us.store.bambulab.com/products/m3-heat-set-insert"
    item = P.create({"source": "bambu", "source_ref": link["source_ref"], "name": link["name"], "category": link["category"], "quantity": 50})
    assert (item["source"], item["catalog_key"], item["links"]["bambu"]) == ("bambu", None, link["source_ref"])
    with pytest.raises(P.PartsError, match="not a Bambu Lab Store"):
        P.create({"source": "bambu", "source_ref": "https://example.com/products/x", "name": "x"})
    # name search lists catalog parts, tagged with their source
    named = asyncio.run(P.lookup("608zz"))["parts"]
    assert [(p["key"], p["source"]) for p in named] == [("bearing-608zz", "bambu")]


def test_existing_catalog_parts_become_bambu(env):
    P = env.parts
    a = P.create({"catalog_key": "screw-shcs-m3x8", "quantity": 5})
    assert a["source"] == "bambu"
    with env.db.tx() as c:  # as saved before sources were recorded
        c.execute("UPDATE parts SET source='custom' WHERE id=?", (a["id"],))
    assert P.get(a["id"])["source"] == "bambu"  # inferred from the catalog key
    env.db.init()  # migration backfills the column
    with env.db.tx() as c:
        assert c.execute("SELECT source FROM parts WHERE id=?", (a["id"],)).fetchone()[0] == "bambu"


def test_api(env, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    importlib.reload(main)
    monkeypatch.setattr(main, "store", env.store)
    monkeypatch.setattr(main.mcmaster, "_transport", env.mcmaster._transport)
    body = {"username": "maker", "password": "wrong", "certificate": _pfx(), "certificate_password": "certpass"}
    # like AI connections, only a password session or the server itself can change it
    assert TestClient(main.app).put("/api/parts/sources/mcmaster", json=body).status_code == 403
    assert TestClient(main.app).delete("/api/parts/sources/mcmaster").status_code == 403
    client = TestClient(main.app, client=("127.0.0.1", 5000))
    r = client.put("/api/parts/sources/mcmaster", json=body)
    assert r.status_code == 400 and "didn't accept" in r.json()["detail"]
    r = client.put("/api/parts/sources/mcmaster", json={**body, "password": "s3cret"})
    assert r.status_code == 200, r.text
    assert r.json()["sources"]["mcmaster"] is True and r.json()["mcmaster"]["configured"] is True
    assert "s3cret" not in r.text
    # saving again without re-entering secrets keeps them; enabled=false turns it off
    r = client.put("/api/parts/sources/mcmaster", json={"username": "maker", "enabled": False})
    assert r.status_code == 200 and r.json()["sources"]["mcmaster"] is False
    client.put("/api/parts/sources/mcmaster", json={"username": "maker"})
    found = client.post("/api/parts/lookup", json={"query": PN}).json()["parts"]
    assert found[0]["key"] == f"mcm:{PN}"
    assert client.post("/api/parts/lookup", json={"query": ""}).status_code == 400
    added = client.post("/api/parts", json={"catalog_key": f"mcm:{PN}", "quantity": 3}).json()
    assert added["source"] == "mcmaster"
    cat = client.get("/api/parts/catalog").json()
    assert {s["id"] for s in cat["sources"] if s["enabled"]} == {"bambu", "mcmaster"}
    r = client.delete("/api/parts/sources/mcmaster")
    assert r.json()["mcmaster"]["configured"] is False and r.json()["sources"]["mcmaster"] is False
    assert client.get("/api/parts").json()["parts"][0]["links"]["mcmaster"].endswith(f"/{PN}/")
