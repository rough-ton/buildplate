"""McMaster-Carr as a parts catalog source, through McMaster-Carr's Product Information API.

The API is for approved McMaster-Carr customers. McMaster-Carr issues each one a
client certificate (a PKCS#12 .pfx file with its own password), and the account's
username and password log in to get a token that lasts 24 hours. There is no search
endpoint: products are looked up by part number, and an account must "subscribe" to a
product before it can read it (subscriptions are limited per day). So Buildplate
looks a part up only when the user asks for it, and keeps what it found in
db.catalog_cache. Searching McMaster-Carr in Buildplate searches those parts; for
everything else the user searches mcmaster.com and pastes the part number or link.

API reference: https://www.mcmaster.com/help/api/
"""

from __future__ import annotations

import base64
import os
import re
import ssl
import tempfile
import time
from typing import Any

import httpx

from . import config

API = "https://api.mcmaster.com/v1"
SOURCE = "mcmaster"
CACHE_DAYS = 7  # re-read a cached product after this long, to pick up changes
TIMEOUT = 15.0

# McMaster-Carr part numbers: digits, one letter, digits (91290A115, 94180A331, 5972K91).
# The letter is never x, so sizes such as "10x3" are not taken for part numbers.
PART_RE = re.compile(r"^\d{3,6}[A-WYZa-wyz]\d{1,5}$")
_URL_RE = re.compile(r"mcmaster\.com/(?:#)?(?:[a-z-]+/)*?(\d{2,6}[A-Za-z]\d{1,5})(?:[/?#]|$)", re.I)

# Tests swap in an httpx.MockTransport.
_transport: httpx.AsyncBaseTransport | None = None
_tokens: dict[str, tuple[str, float]] = {}  # username -> (token, expires at)


class McMasterError(Exception):
    """Shown to the user as is."""


def parse_ref(text: str) -> str | None:
    """The McMaster-Carr part number in a part number or mcmaster.com link, or None."""
    t = (text or "").strip()
    if PART_RE.match(t):
        return t.upper()
    m = _URL_RE.search(t)
    return m.group(1).upper() if m else None


def product_url(part_number: str) -> str:
    return f"https://www.mcmaster.com/{part_number}/"


def search_url(query: str) -> str:
    from urllib.parse import quote

    return f"https://www.mcmaster.com/products/{quote(query.strip())}/"


# ---- client certificate ------------------------------------------------------------
def load_certificate(pfx_b64: str, password: str) -> tuple[bytes, bytes]:
    """The certificate chain and private key (PEM) from a base64 PKCS#12 file."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    try:
        raw = base64.b64decode(pfx_b64, validate=True)
        key, cert, extra = pkcs12.load_key_and_certificates(raw, (password or "").encode() or None)
    except Exception:
        raise McMasterError("Couldn't open the client certificate. Check that it is the .pfx file McMaster-Carr sent "
                            "and that the certificate password is right.") from None
    if key is None or cert is None:
        raise McMasterError("The certificate file has no private key. Use the .pfx file McMaster-Carr sent.")
    chain = cert.public_bytes(serialization.Encoding.PEM) + b"".join(c.public_bytes(serialization.Encoding.PEM) for c in extra or [])
    pem_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return chain, pem_key


def _ssl_context(creds: dict[str, Any]) -> ssl.SSLContext:
    chain, key = load_certificate(creds["certificate"], creds.get("certificate_password", ""))
    ctx = ssl.create_default_context()
    # load_cert_chain only reads files: write a private temp file and delete it at once.
    config.ensure_dirs()
    fd, path = tempfile.mkstemp(prefix="mcm-", suffix=".pem", dir=config.CACHE_DIR)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(chain + key)
        ctx.load_cert_chain(path)
    finally:
        os.unlink(path)
    return ctx


def _client(creds: dict[str, Any]) -> httpx.AsyncClient:
    if _transport is not None:
        return httpx.AsyncClient(base_url=API, timeout=TIMEOUT, transport=_transport)
    return httpx.AsyncClient(base_url=API, timeout=TIMEOUT, verify=_ssl_context(creds))


def _reply_error(r: httpx.Response) -> str:
    try:
        body = r.json()
        msg = body.get("ErrorDescription") or body.get("ErrorMessage") or body.get("message") or ""
    except Exception:
        msg = ""
    return str(msg)[:200]


# ---- API calls -----------------------------------------------------------------------
async def _login(client: httpx.AsyncClient, creds: dict[str, Any]) -> str:
    try:
        r = await client.post("/login", json={"UserName": creds["username"], "Password": creds["password"]})
    except httpx.HTTPError as exc:
        raise McMasterError(f"Couldn't reach McMaster-Carr's API ({type(exc).__name__}). Check the certificate and "
                            "that this server can reach api.mcmaster.com.") from None
    if r.status_code in (401, 403):
        raise McMasterError(f"McMaster-Carr didn't accept that username and password. {_reply_error(r)}".strip())
    if r.status_code >= 400:
        raise McMasterError(f"McMaster-Carr's API answered {r.status_code}. {_reply_error(r)}".strip())
    token = (r.json() or {}).get("AuthToken")
    if not token:
        raise McMasterError("McMaster-Carr's API didn't return a login token.")
    return token


async def _token(client: httpx.AsyncClient, creds: dict[str, Any], fresh: bool = False) -> str:
    hit = _tokens.get(creds["username"])
    if hit and not fresh and hit[1] > time.time():
        return hit[0]
    token = await _login(client, creds)
    _tokens[creds["username"]] = (token, time.time() + 23 * 3600)  # tokens last 24 h
    return token


def forget_login(username: str = "") -> None:
    if username:
        _tokens.pop(username, None)
    else:
        _tokens.clear()


async def test(creds: dict[str, Any]) -> None:
    """Check a login (certificate, username, password). Raises McMasterError."""
    for k, label in (("username", "username"), ("password", "password"), ("certificate", "client certificate")):
        if not creds.get(k):
            raise McMasterError(f"Enter the McMaster-Carr API {label}.")
    forget_login(creds["username"])
    async with _client(creds) as client:
        await _token(client, creds, fresh=True)


async def fetch_product(creds: dict[str, Any], part_number: str) -> dict[str, Any]:
    """The API's product record for a part number, subscribing to it if needed."""
    async with _client(creds) as client:
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {await _token(client, creds, fresh=attempt > 0)}"}
            try:
                r = await client.get(f"/products/{part_number}", headers=headers)
                if r.status_code == 403:
                    # not subscribed yet: subscribing returns the product too
                    r = await client.put("/products", headers=headers, json={"URL": f"https://mcmaster.com/{part_number}"})
            except httpx.HTTPError as exc:
                raise McMasterError(f"Couldn't reach McMaster-Carr's API ({type(exc).__name__}).") from None
            if r.status_code == 401 and attempt == 0:
                continue  # token expired early: log in again once
            if r.status_code == 404:
                raise McMasterError(f"McMaster-Carr has no part {part_number}.")
            if r.status_code == 429:
                raise McMasterError("McMaster-Carr's API limit was reached (it limits how many new parts can be looked up "
                                    "each day). Try again tomorrow, or add the part as a custom part.")
            if r.status_code >= 400:
                raise McMasterError(f"McMaster-Carr couldn't look up {part_number} ({r.status_code}). {_reply_error(r)}".strip())
            data = r.json()
            if not isinstance(data, dict) or not data.get("PartNumber"):
                raise McMasterError(f"McMaster-Carr returned no details for {part_number}.")
            return data
    raise McMasterError("McMaster-Carr didn't accept the login.")


# ---- product -> Buildplate part --------------------------------------------------------
_CATEGORIES = (
    ("heat-set insert", ("heat-set", "heat set", "insert")),
    ("screw", ("screw", "bolt")),
    ("nut", ("nut",)),
    ("washer", ("washer",)),
    ("magnet", ("magnet",)),
    ("bearing", ("bearing",)),
    ("standoff", ("standoff", "spacer")),
    ("spring", ("spring",)),
    ("rod", ("rod", "shaft")),
    ("rubber foot", ("bumper", "foot")),
)


def _specs(data: dict[str, Any]) -> dict[str, str]:
    out = {}
    for sp in data.get("Specifications") or []:
        name = str(sp.get("Attribute") or "").strip()
        vals = sp.get("Values") or []
        if name and vals:
            out[name] = ", ".join(str(v) for v in vals)[:120]
    return out


def to_part(data: dict[str, Any]) -> dict[str, Any]:
    """A catalog part (same shape as the bundled catalog's) from an API product record."""
    pn = str(data["PartNumber"]).upper()
    family = str(data.get("FamilyDescription") or "").strip()
    detail = str(data.get("DetailDescription") or "").strip()
    specs = _specs(data)
    text = f"{family} {detail}".lower()
    category = next((c for c, words in _CATEGORIES if any(w in text for w in words)), "other")
    thread = specs.get("Thread Size", "")
    m = re.match(r"(M\d+(?:\.\d+)?)", thread)
    size = m.group(1) if m else thread.split(",")[0]
    length = specs.get("Length", "")
    lm = re.match(r"([\d.]+)\s*mm", length)
    if size and lm and category == "screw":
        size = f"{size} x {lm.group(1).rstrip('0').rstrip('.')}"
    name = (f"{family}, {detail}" if detail else family) or f"McMaster-Carr {pn}"
    return {
        "key": f"mcm:{pn}",
        "source": SOURCE,
        "source_ref": pn,
        "category": category,
        "name": name[:200],
        "size": size[:60],
        "type": specs.get("Head Type", "") or specs.get("Type", ""),
        "material": specs.get("Material", ""),
        "dims": {},
        "holes": {},
        "specs": dict(list(specs.items())[:20]),
        "url": product_url(pn),
        "status": str(data.get("ProductStatus") or ""),
        "search": f"{size} {family}".strip(),
    }
