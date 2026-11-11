"""Parts inventory: the hardware a user has on hand, the catalog to pick it from, and
bills of materials (BOMs) for designs.

Parts come from catalog sources, which the user turns on and off in Settings:

  bambu     Bambu's Maker Supply: the bundled catalog (data/parts_catalog.json, built
            by tools/build_parts_catalog.py) of common 3D printing hardware with the
            hole and pocket sizes needed to model it, bought from the Bambu Lab Store.
            On by default.
  mcmaster  McMaster-Carr, looked up live by part number through its API (see
            mcmaster.py). Off until the user saves an API login. Its keys are
            "mcm:<part number>".

The inventory is the user's own stock. Each item records its source (bambu, mcmaster or
custom) and its part number or product link there, and buy links follow the source.
Custom parts can always be added, whatever sources are on.

A design version can carry a BOM: the parts it uses, as {part, quantity, use}. part is
an inventory id (p_...), a catalog key, or a free-text name for something not in the
catalog. Status (have / short / buy) and buy links are worked out when the BOM is read,
so they follow the current inventory.

Hardware modes for a design request:
  ask        ask the user when the request implies hardware, otherwise best effort
  inventory  use only parts on hand, within the quantities on hand
  order      pick the best standard parts and list what to buy
  none       design without hardware (printed alternatives) unless unavoidable
"""

from __future__ import annotations

import csv
import io
import json
import re
import secrets
import time
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from . import db, mcmaster
from .settings import store

CATALOG_FILE = Path(__file__).parent / "data" / "parts_catalog.json"
MODES = ("ask", "inventory", "order", "none")
MODE_LABELS = {
    "ask": "Ask me",
    "inventory": "Use parts I have",
    "order": "Buy parts as needed",
    "none": "No hardware",
}
STORE_REGIONS = {
    "us": "us.store.bambulab.com",
    "ca": "ca.store.bambulab.com",
    "eu": "eu.store.bambulab.com",
    "uk": "uk.store.bambulab.com",
    "au": "au.store.bambulab.com",
    "asia": "asia.store.bambulab.com",
}
AMAZON_DOMAINS = ("amazon.com", "amazon.ca", "amazon.co.uk", "amazon.de", "amazon.fr", "amazon.it", "amazon.es",
                  "amazon.nl", "amazon.se", "amazon.pl", "amazon.com.au", "amazon.co.jp", "amazon.in", "amazon.com.mx", "amazon.com.br")
FIELDS = ("category", "name", "size", "type", "material", "quantity", "low_stock", "bambu_url", "amazon_url", "other_url", "notes")
CSV_COLUMNS = ("id", "catalog_key", "category", "name", "size", "type", "material", "quantity", "low_stock",
               "bambu_url", "amazon_url", "other_url", "notes", "dims", "holes", "source", "source_ref")
SOURCES = {"bambu": "Bambu's Maker Supply", "mcmaster": "McMaster-Carr", "custom": "Custom"}
CATALOG_SOURCES = ("bambu", "mcmaster")
_BAMBU_URL = re.compile(r"^https?://(?:[a-z]+\.)?store\.bambulab\.com/(?:[a-z-]+/)*products/([a-z0-9][a-z0-9-]*)", re.I)


class PartsError(ValueError):
    """Shown to the user as is."""


# ---- catalog ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def catalog() -> dict[str, Any]:
    """The bundled catalog: Bambu's Maker Supply."""
    return json.loads(CATALOG_FILE.read_text())


def enabled_sources() -> list[str]:
    on = (store.get().get("parts") or {}).get("sources") or {}
    return [s_ for s_ in CATALOG_SOURCES if on.get(s_)]


def _source_of(p: dict[str, Any]) -> str:
    """bambu, mcmaster or custom, also for parts saved before sources were recorded."""
    if p.get("source") in CATALOG_SOURCES:
        return p["source"]
    key = str(p.get("catalog_key") or p.get("key") or "")
    return "mcmaster" if key.startswith("mcm:") else "bambu" if key else "custom"


def _with_holes(part: dict[str, Any]) -> dict[str, Any]:
    """A McMaster-Carr part has no modelling data of its own: borrow the hole and pocket
    sizes of the matching standard part in the bundled catalog (same category and size,
    and head type for screws), so the design agent can still model it."""
    if part.get("holes") or part.get("category") in (None, "", "other"):
        return part
    size = str(part.get("size") or "")
    kind = str(part.get("type") or "").lower()
    same = [c for c in catalog()["parts"] if c["category"] == part["category"]]
    head = next((h for h in ("socket", "button", "countersunk", "flat") if h in kind or h in part["name"].lower()), "")
    head = "countersunk" if head == "flat" else head
    match = (next((c for c in same if c["size"] == size and head and head in c["type"]), None)
             or next((c for c in same if c["size"] == size), None)
             or next((c for c in same if size and c["size"].split(" x ")[0] == size.split(" x ")[0]), None))
    if match:
        part = {**part, "holes": {**match["holes"], "note": f"Sizes of the standard {match['name']}; check McMaster-Carr's drawing."},
                "dims": part.get("dims") or match.get("dims") or {}}
    return part


def _mcmaster_cached(part_number: str) -> dict[str, Any] | None:
    hit = db.cache_get(mcmaster.SOURCE, part_number)
    return _with_holes(hit[0]) if hit else None


def catalog_part(key: str) -> dict[str, Any] | None:
    """A catalog part by key: a Bambu's Maker Supply key, or "mcm:<part number>" for a
    McMaster-Carr part that has been looked up."""
    key = str(key or "")
    if key.startswith("mcm:"):
        return _mcmaster_cached(key[4:])
    p = next((p for p in catalog()["parts"] if p["key"] == key), None)
    return {**p, "source": "bambu"} if p else None


def _matches(p: dict[str, Any], words: list[str]) -> bool:
    text = f"{p['name']} {p.get('size', '')} {p.get('type', '')} {p['category']} {p.get('source_ref', '')}".lower()
    return all(w in text for w in words)


def search_catalog(query: str = "", category: str = "", sources: list[str] | None = None) -> list[dict[str, Any]]:
    """Catalog parts from the enabled sources (or the given ones), each tagged with its
    source. McMaster-Carr has no search API, so its results are the parts already
    looked up."""
    srcs = enabled_sources() if sources is None else sources
    words = query.lower().split()
    out: list[dict[str, Any]] = []
    if "bambu" in srcs:
        out += [{**p, "source": "bambu"} for p in catalog()["parts"]
                if (not category or p["category"] == category) and _matches(p, words)]
    if "mcmaster" in srcs:
        out += [_with_holes(p) for p in db.cache_list(mcmaster.SOURCE)
                if (not category or p["category"] == category) and _matches(p, words)]
    return out


def catalog_view() -> dict[str, Any]:
    """The catalog for "Add from catalog": parts from the enabled sources, and which
    sources there are."""
    srcs = enabled_sources()
    found = search_catalog(sources=srcs)
    cats = sorted({p["category"] for p in found} | (set(catalog()["categories"]) if "bambu" in srcs else set()))
    return {
        "version": catalog()["version"],
        "categories": cats or list(catalog()["categories"]),
        "parts": found,
        "sources": [{"id": k, "label": SOURCES[k], "enabled": k in srcs} for k in CATALOG_SOURCES],
    }


async def mcmaster_part(part_number: str) -> dict[str, Any]:
    """A McMaster-Carr part, from the cache when it is recent, else from the API."""
    hit = db.cache_get(mcmaster.SOURCE, part_number)
    if hit and time.time() - hit[1] < mcmaster.CACHE_DAYS * 86400:
        return _with_holes(hit[0])
    mc = store.get()["parts"]["mcmaster"]
    creds = {"username": mc.get("username", ""), **(mc.get("secrets") or {})}
    try:
        part = mcmaster.to_part(await mcmaster.fetch_product(creds, part_number))
    except mcmaster.McMasterError as exc:
        if hit:
            return _with_holes(hit[0])  # stale is better than nothing
        raise PartsError(str(exc)) from None
    db.cache_put(mcmaster.SOURCE, part_number, part)
    return _with_holes(part)


def _bambu_link_part(url: str, handle: str) -> dict[str, Any]:
    """A part for a Bambu Lab Store product link that is not in the bundled catalog."""
    name = " ".join(w.upper() if re.fullmatch(r"m\d+(\.\d+)?", w) else w for w in handle.split("-") if w).strip()
    name = name[:1].upper() + name[1:]
    text = name.lower().replace("-", " ")
    category = next((c for c in catalog()["categories"] if c.lower().replace("-", " ") in text), "other")
    return {"key": "", "source": "bambu", "source_ref": url, "category": category, "name": name or "Bambu Lab Store part",
            "size": "", "type": "", "material": "", "dims": {}, "holes": {}, "url": url, "link_only": True}


async def lookup(query: str) -> dict[str, Any]:
    """Direct entry: find parts by part number, catalog key, product link or name in the
    enabled sources. Returns {parts, message}."""
    q = str(query or "").strip()[:300]
    srcs = enabled_sources()
    if not q:
        raise PartsError("Enter a part number, a product link or a name.")
    if not srcs:
        raise PartsError("No parts catalog is turned on. Turn one on in Settings > Parts and shopping, or add a custom part.")
    pn = mcmaster.parse_ref(q)
    if pn and (mcmaster.PART_RE.match(q) or "mcmaster" in q.lower()):
        if "mcmaster" not in srcs:
            raise PartsError("That is a McMaster-Carr part. Turn on McMaster-Carr in Settings > Parts and shopping to look it up.")
        return {"parts": [await mcmaster_part(pn)], "message": ""}
    m = _BAMBU_URL.match(q)
    if m:
        if "bambu" not in srcs:
            raise PartsError("That is a Bambu Lab Store link. Turn on Bambu's Maker Supply in Settings > Parts and shopping to use it.")
        handle = m.group(1).lower()
        words = [w for w in handle.split("-") if w and w not in ("pcs", "pack", "kit", "and", "for", "with", "the")]
        sizes = [w for w in words if re.fullmatch(r"m\d+(\.\d+)?(x\d+(\.\d+)?)?|\d+x\d+|\d+zz", w)]

        def score(c: dict[str, Any]) -> int:
            text = f"{c['name']} {c.get('search', '')} {c['size']}".lower().replace("-", " ")
            tokens = set(re.split(r"[\s,]+", text.replace(" x ", "x")))
            if any(sz not in tokens for sz in sizes):  # an M3 link never suggests M2 parts
                return 0
            return sum(w in text for w in words)

        scored = sorted(((score(c), c) for c in catalog()["parts"]), key=lambda x: -x[0])
        best = [{**c, "source": "bambu"} for n, c in scored[:5] if n and n >= max(2, len(words) - 1)]
        link = _bambu_link_part(q.split("?")[0], handle)
        return {"parts": best + [link], "message": "Pick the matching catalog part to get its hole sizes, or add the linked product as it is."}
    if "bambu" in srcs:
        exact = catalog_part(q)
        if exact and exact["source"] == "bambu":
            return {"parts": [exact], "message": ""}
    found = search_catalog(q, sources=srcs)[:50]
    msg = "" if found else "No catalog parts match."
    if "mcmaster" in srcs and not pn:
        msg = (msg + " McMaster-Carr can only be looked up by part number: search mcmaster.com and paste the part number or link.").strip()
    return {"parts": found, "message": msg, "search_url": mcmaster.search_url(q) if "mcmaster" in srcs else ""}


# ---- links ------------------------------------------------------------------------------
def _store_query(p: dict[str, Any]) -> str:
    size = str(p.get("size") or "").split(" x ")[0]
    cat = p.get("category", "")
    if cat == "screw":
        return f"{size} {p.get('type', '')} screw".strip()
    if cat == "heat-set insert":
        return f"{size} heat-set insert"
    if cat in ("magnet", "bearing", "nut"):
        return p.get("name") or cat
    return p.get("name") or cat


def links(p: dict[str, Any]) -> dict[str, str]:
    """Buy links for a part, following where it came from: its McMaster-Carr product
    page, or the Bambu Lab Store (the product link, or a search in the user's region),
    plus a search on their Amazon site (with their associate tag, if set) and the
    user's own link. A custom part gets the Bambu Lab Store search only while Bambu's
    Maker Supply is turned on."""
    s = store.get().get("parts") or {}
    source = _source_of(p)
    host = STORE_REGIONS.get(s.get("store_region") or "us", STORE_REGIONS["us"])
    domain = s.get("amazon_domain") if s.get("amazon_domain") in AMAZON_DOMAINS else "amazon.com"
    query = p.get("search") or " ".join(x for x in (p.get("size"), p.get("type"), p.get("name")) if x)
    amazon = f"https://www.{domain}/s?k={quote_plus(query)}"
    tag = (s.get("amazon_tag") or "").strip()
    if tag:
        amazon += f"&tag={quote_plus(tag)}"
    out: dict[str, str] = {}
    if source == "mcmaster":
        ref = p.get("source_ref") or str(p.get("catalog_key") or p.get("key") or "")[4:]
        if ref:
            out["mcmaster"] = mcmaster.product_url(ref)
    if source == "bambu" or p.get("bambu_url") or (source == "custom" and (s.get("sources") or {}).get("bambu", True)):
        out["bambu"] = p.get("bambu_url") or f"https://{host}/search?q={quote_plus(_store_query(p))}"
    out["amazon"] = p.get("amazon_url") or amazon
    if p.get("other_url"):
        out["other"] = p["other_url"]
    return out


# ---- inventory ----------------------------------------------------------------------------
def _row(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    out["dims"] = json.loads(out.get("dims") or "{}")
    out["holes"] = json.loads(out.get("holes") or "{}")
    out["low"] = out["low_stock"] is not None and out["quantity"] <= out["low_stock"]
    out["search"] = (catalog_part(out["catalog_key"]) or {}).get("search") if out.get("catalog_key") else None
    out["source"] = _source_of(out)
    out["source_label"] = SOURCES[out["source"]]
    out["links"] = links(out)
    return out


def list_inventory(query: str = "", category: str = "") -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM parts", []
    where = []
    if category:
        where.append("category = ?")
        args.append(category)
    for word in query.lower().split():
        where.append("lower(name || ' ' || size || ' ' || type || ' ' || category || ' ' || notes || ' ' || source_ref) LIKE ?")
        args.append(f"%{word}%")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY category, name"
    with db.tx() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
    return [_row(r) for r in rows]


def get(part_id: str) -> dict[str, Any]:
    with db.tx() as c:
        r = c.execute("SELECT * FROM parts WHERE id=?", (part_id,)).fetchone()
    if r is None:
        raise KeyError(part_id)
    return _row(dict(r))


def _clean(data: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in FIELDS:
        if f not in data:
            continue
        v = data[f]
        if f in ("quantity", "low_stock"):
            if v in (None, "") and f == "low_stock":
                out[f] = None
                continue
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise PartsError(f"{f.replace('_', ' ').capitalize()} must be a whole number.") from None
            if v < 0 or v > 1_000_000:
                raise PartsError(f"{f.replace('_', ' ').capitalize()} must be between 0 and 1,000,000.")
        elif f.endswith("_url"):
            v = str(v or "").strip()
            if v and not re.match(r"^https?://", v):
                raise PartsError("Links must start with http:// or https://.")
            v = v[:500]
        else:
            v = str(v or "").strip()[:200 if f != "notes" else 1000]
        out[f] = v
    if not partial and not out.get("name"):
        raise PartsError("Give the part a name.")
    return out


def create(data: dict[str, Any]) -> dict[str, Any]:
    """Add a part: from a catalog source ({catalog_key, quantity, ...}), from a Bambu
    Lab Store product link ({source: "bambu", source_ref: <link>, name, ...}) or
    custom fields. Adding a catalog part that is already in the inventory adds to its
    quantity."""
    key = (data.get("catalog_key") or "").strip()
    base: dict[str, Any] = {}
    dims: dict[str, Any] = {}
    holes: dict[str, Any] = {}
    source, source_ref = "custom", ""
    if key:
        cat = catalog_part(key)
        if cat is None:
            raise PartsError("That part is not in the catalog." if not key.startswith("mcm:")
                             else "Look that McMaster-Carr part up in Add from catalog first.")
        existing = next((p for p in list_inventory() if p.get("catalog_key") == key), None)
        if existing:
            return adjust(existing["id"], int(data.get("quantity") or 0))
        if cat["source"] not in enabled_sources():
            raise PartsError(f"{SOURCES[cat['source']]} is turned off in Settings > Parts and shopping.")
        base = {k: cat.get(k, "") for k in ("category", "name", "size", "type", "material")}
        dims, holes = cat.get("dims") or {}, cat.get("holes") or {}
        source, source_ref = cat["source"], cat.get("source_ref", "")
    elif data.get("source") == "bambu" and data.get("source_ref"):
        ref = str(data["source_ref"]).strip()
        if not _BAMBU_URL.match(ref):
            raise PartsError("That is not a Bambu Lab Store product link.")
        if "bambu" not in enabled_sources():
            raise PartsError(f"{SOURCES['bambu']} is turned off in Settings > Parts and shopping.")
        source, source_ref = "bambu", ref[:500]
        data = {**data, "bambu_url": data.get("bambu_url") or source_ref}
    base.update(_clean({**{"quantity": 0}, **data}, partial=bool(key)))
    if isinstance(data.get("dims"), dict):
        dims = data["dims"]
    if isinstance(data.get("holes"), dict):
        holes = data["holes"]
    pid = "p_" + secrets.token_hex(4)
    now = time.time()
    with db.tx() as c:
        c.execute(
            "INSERT INTO parts (id, catalog_key, source, source_ref, category, name, size, type, material, dims, holes, quantity, "
            "low_stock, bambu_url, amazon_url, other_url, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, key or None, source, source_ref, base.get("category") or "other", base["name"], base.get("size", ""),
             base.get("type", ""), base.get("material", ""), json.dumps(dims), json.dumps(holes), base.get("quantity", 0),
             base.get("low_stock"), base.get("bambu_url", ""), base.get("amazon_url", ""), base.get("other_url", ""),
             base.get("notes", ""), now, now),
        )
    return get(pid)


def update(part_id: str, data: dict[str, Any]) -> dict[str, Any]:
    get(part_id)
    vals = _clean(data, partial=True)
    if "name" in vals and not vals["name"]:
        raise PartsError("Give the part a name.")
    sets = [f"{k}=?" for k in vals]
    args = list(vals.values())
    for jf in ("dims", "holes"):
        if isinstance(data.get(jf), dict):
            sets.append(f"{jf}=?")
            args.append(json.dumps(data[jf]))
    if sets:
        with db.tx() as c:
            c.execute(f"UPDATE parts SET {', '.join(sets)}, updated_at=? WHERE id=?", (*args, time.time(), part_id))
    return get(part_id)


def adjust(part_id: str, delta: int) -> dict[str, Any]:
    """Change the quantity on hand by delta (never below zero)."""
    get(part_id)
    with db.tx() as c:
        c.execute("UPDATE parts SET quantity=MAX(0, quantity + ?), updated_at=? WHERE id=?", (int(delta), time.time(), part_id))
    return get(part_id)


def delete(part_id: str) -> None:
    get(part_id)
    with db.tx() as c:
        c.execute("DELETE FROM parts WHERE id=?", (part_id,))


# ---- CSV ---------------------------------------------------------------------------------
def export_csv() -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for p in list_inventory():
        w.writerow({**p, "dims": json.dumps(p["dims"]), "holes": json.dumps(p["holes"]),
                    "low_stock": "" if p["low_stock"] is None else p["low_stock"]})
    return buf.getvalue()


def import_csv(text: str) -> dict[str, Any]:
    """Merge a CSV into the inventory. Rows match an existing part by id, then by
    catalog_key, then by name; matches are updated, everything else is added. Only
    the columns present are changed."""
    added = updated = 0
    errors: list[str] = []
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or "name" not in reader.fieldnames and "catalog_key" not in reader.fieldnames:
        raise PartsError("The CSV needs a header row with at least a name or catalog_key column.")
    current = list_inventory()
    for n, row in enumerate(reader, start=2):
        row = {k: v for k, v in row.items() if k and v is not None}
        try:
            for jf in ("dims", "holes"):
                if row.get(jf):
                    row[jf] = json.loads(row[jf])
                else:
                    row.pop(jf, None)
            match = (next((p for p in current if row.get("id") and p["id"] == row["id"]), None)
                     or next((p for p in current if row.get("catalog_key") and p.get("catalog_key") == row["catalog_key"]), None)
                     or next((p for p in current if row.get("name") and p["name"].lower() == row["name"].strip().lower()), None))
            data = {k: v for k, v in row.items() if k in FIELDS or k in ("dims", "holes")}
            if match:
                update(match["id"], data)
                updated += 1
            else:
                if row.get("catalog_key") and not row.get("name") and catalog_part(row["catalog_key"]):
                    create({"catalog_key": row["catalog_key"], "quantity": row.get("quantity") or 0})
                else:
                    create({**data, "catalog_key": row.get("catalog_key") if catalog_part(row.get("catalog_key") or "") else ""})
                added += 1
                current = list_inventory()
        except (PartsError, json.JSONDecodeError) as exc:
            errors.append(f"Row {n}: {exc}")
    return {"added": added, "updated": updated, "errors": errors}


# ---- for the design agent ---------------------------------------------------------------------
_HARDWARE = re.compile(
    r"\b(screws?|bolts?|nuts?|washers?|inserts?|heat[- ]?set|magnet(s|ic)?|bearings?|hinges?|fasten(ers?|ing)?|"
    r"threaded|standoffs?|springs?|axles?|rods?|wall[- ]?mount(ed)?|mounting holes?|m[2-8](x\d+)?|608|625)\b",
    re.I,
)
_NO_HARDWARE = re.compile(r"\b(no|without)\s+(hardware|screws?|magnets?|fasteners?)\b|\bsnap[- ]?fit\b|\bprint[- ]in[- ]place\b", re.I)


def needs_hardware(prompt: str) -> bool:
    """Does the request clearly imply hardware (and not rule it out)?"""
    return bool(_HARDWARE.search(prompt)) and not _NO_HARDWARE.search(prompt)


def _holes_text(holes: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in holes.items() if k != "note")


def describe(p: dict[str, Any], on_hand: int | None = None) -> str:
    ref = p.get("id") or p.get("key")
    stock = f", {on_hand} on hand" if on_hand is not None else ""
    return f"- {ref}: {p['name']} ({p.get('type', '')}{stock}); holes: {_holes_text(p.get('holes') or {})}"


def inventory_summary(limit: int = 40) -> str:
    inv = [p for p in list_inventory() if p["quantity"] > 0]
    lines = [describe(p, p["quantity"]) for p in inv[:limit]]
    if len(inv) > limit:
        lines.append(f"- ...and {len(inv) - limit} more; call list_parts to search.")
    return "\n".join(lines) if lines else "(The user's inventory is empty.)"


def search_for_agent(query: str = "", category: str = "", limit: int = 25) -> str:
    """Inventory first, then parts from the enabled catalog sources that are not in the
    inventory, as compact lines."""
    inv = list_inventory(query, category)
    have_keys = {p.get("catalog_key") for p in inv}
    cat = [p for p in search_catalog(query, category) if p["key"] not in have_keys]
    lines = [describe(p, p["quantity"]) for p in inv[:limit]]
    lines += [describe(p, 0) for p in cat[: max(0, limit - len(lines))]]
    return "\n".join(lines) or "No matching parts. For something not listed, give its name in submit_design."


def mode_instructions(mode: str) -> str:
    inv = inventory_summary()
    common = ("When the design uses hardware, model every hole, pocket, nut trap and insert bore with the sizes listed "
              "for the part (they already allow for printing), and list each part in submit_design's parts with its "
              "quantity, using the part's id or key.")
    if mode == "inventory":
        return ("Hardware: use ONLY parts from the user's inventory, and no more than the quantity on hand. "
                f"{common}\nThe user's inventory:\n{inv}")
    if mode == "none":
        return ("Hardware: design WITHOUT hardware. Use printed alternatives: snap fits, printed threads, press-fit "
                "printed pins, living hinges, dovetails, clips or friction fits. If hardware is truly unavoidable, design "
                "it anyway, list the parts, and explain why in submit_design's hardware_note.")
    if mode == "order":
        return ("Hardware: choose the best standard parts for the job from the catalog (call list_parts to search); "
                f"prefer parts the user already has. {common}\nThe user's inventory:\n{inv}")
    return ("Hardware: if the design needs hardware, prefer parts from the user's inventory, otherwise standard catalog "
            f"parts (call list_parts). {common}\nThe user's inventory:\n{inv}")


# ---- bills of materials ------------------------------------------------------------------------
def normalize_bom(lines: Any) -> list[dict[str, Any]]:
    out = []
    for ln in lines or []:
        if not isinstance(ln, dict):
            continue
        part = str(ln.get("part") or ln.get("key") or ln.get("id") or ln.get("name") or "").strip()[:200]
        try:
            qty = int(ln.get("quantity") or ln.get("qty") or 1)
        except (TypeError, ValueError):
            qty = 1
        if part and qty > 0:
            out.append({"part": part, "quantity": min(qty, 10000), "use": str(ln.get("use") or "").strip()[:200]})
    return out


def resolve_bom(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each line with what it refers to, how many are on hand and buy links.
    status: have (enough on hand), short (some on hand), buy (none on hand)."""
    inv = list_inventory()
    by_id = {p["id"]: p for p in inv}
    by_key = {p["catalog_key"]: p for p in inv if p.get("catalog_key")}
    by_name = {p["name"].lower(): p for p in inv}
    out = []
    for ln in normalize_bom(lines):
        ref = ln["part"]
        item = by_id.get(ref) or by_key.get(ref) or by_name.get(ref.lower())
        source = "inventory"
        if item is None:
            item = catalog_part(ref)
            source = "catalog" if item else "custom"
        if item is None:
            item = {"name": ref, "category": "other", "holes": {}, "search": ref}
        on_hand = int(item.get("quantity") or 0) if source == "inventory" else 0
        need = ln["quantity"]
        status = "have" if on_hand >= need else ("short" if on_hand > 0 else "buy")
        out.append({
            "part": ref, "quantity": need, "use": ln["use"], "source": source, "catalog_source": _source_of(item),
            "source_ref": item.get("source_ref", ""),
            "part_id": item.get("id"), "catalog_key": item.get("catalog_key") or item.get("key"),
            "name": item["name"], "category": item.get("category", "other"), "size": item.get("size", ""),
            "holes": item.get("holes") or {}, "on_hand": on_hand, "to_buy": max(0, need - on_hand), "status": status,
            "links": links(item),
        })
    return out


def bom_summary(lines: list[dict[str, Any]] | None) -> dict[str, int] | None:
    if not lines:
        return None
    res = resolve_bom(lines)
    return {"parts": sum(r["quantity"] for r in res), "lines": len(res), "to_buy": sum(1 for r in res if r["to_buy"])}


def check_submission(mode: str, lines: list[dict[str, Any]], note: str) -> str | None:
    """Why a submitted design breaks its hardware mode, or None if it is fine."""
    res = resolve_bom(lines)
    if mode == "inventory":
        bad = [r for r in res if r["source"] != "inventory" or r["to_buy"]]
        if bad:
            detail = "; ".join(f"{r['name']}: needs {r['quantity']}, {r['on_hand']} on hand" for r in bad)
            return ("Not saved: this request must use only parts on hand, within the quantities on hand. "
                    f"Problems: {detail}. Change the design to use inventory parts (call list_parts).")
    if mode == "none" and res and not note.strip():
        return ("Not saved: this request asked for no hardware. Use a printed alternative, or if hardware is truly "
                "unavoidable, explain why in hardware_note.")
    return None


# ---- building a design ---------------------------------------------------------------------------
def mark_built(design_id: str, version: int) -> dict[str, Any]:
    """Take the version's parts out of the inventory (as many as are on hand) and
    record exactly what was taken, so it can be undone."""
    v = db.get_version(design_id, version)
    if v is None:
        raise KeyError(design_id)
    if v.get("built"):
        raise PartsError("This version is already marked as built.")
    taken = []
    for r in resolve_bom(json.loads(v.get("bom") or "[]")):
        if r["source"] == "inventory" and r["on_hand"]:
            n = min(r["quantity"], r["on_hand"])
            adjust(r["part_id"], -n)
            taken.append({"part_id": r["part_id"], "name": r["name"], "quantity": n})
    built = {"at": time.time(), "taken": taken}
    with db.tx() as c:
        c.execute("UPDATE versions SET built=? WHERE design_id=? AND version=?", (json.dumps(built), design_id, version))
    return built


def unmark_built(design_id: str, version: int) -> None:
    """Put back what mark_built took."""
    v = db.get_version(design_id, version)
    if v is None:
        raise KeyError(design_id)
    built = json.loads(v.get("built") or "null")
    if not built:
        raise PartsError("This version is not marked as built.")
    for t in built.get("taken", []):
        try:
            adjust(t["part_id"], t["quantity"])
        except KeyError:
            pass  # the part was deleted since
    with db.tx() as c:
        c.execute("UPDATE versions SET built=NULL WHERE design_id=? AND version=?", (design_id, version))
