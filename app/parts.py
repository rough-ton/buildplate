"""Parts inventory: the hardware a user has on hand, the catalog to pick it from, and
bills of materials (BOMs) for designs.

The catalog (data/parts_catalog.json, built by tools/build_parts_catalog.py) lists
common 3D printing hardware with the hole and pocket sizes needed to model it. The
inventory is the user's own stock, each item optionally linked to a catalog entry.

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

from . import db
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
               "bambu_url", "amazon_url", "other_url", "notes", "dims", "holes")


class PartsError(ValueError):
    """Shown to the user as is."""


# ---- catalog ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def catalog() -> dict[str, Any]:
    return json.loads(CATALOG_FILE.read_text())


def catalog_part(key: str) -> dict[str, Any] | None:
    return next((p for p in catalog()["parts"] if p["key"] == key), None)


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
    """Buy links for a part: the user's own URLs where set, otherwise searches on the
    Bambu Lab Store for their region and on their Amazon site (with their associate
    tag, if they set one)."""
    s = store.get().get("parts") or {}
    host = STORE_REGIONS.get(s.get("store_region") or "us", STORE_REGIONS["us"])
    domain = s.get("amazon_domain") if s.get("amazon_domain") in AMAZON_DOMAINS else "amazon.com"
    query = p.get("search") or " ".join(x for x in (p.get("size"), p.get("type"), p.get("name")) if x)
    amazon = f"https://www.{domain}/s?k={quote_plus(query)}"
    tag = (s.get("amazon_tag") or "").strip()
    if tag:
        amazon += f"&tag={quote_plus(tag)}"
    out = {
        "bambu": p.get("bambu_url") or f"https://{host}/search?q={quote_plus(_store_query(p))}",
        "amazon": p.get("amazon_url") or amazon,
    }
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
    out["links"] = links(out)
    return out


def list_inventory(query: str = "", category: str = "") -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM parts", []
    where = []
    if category:
        where.append("category = ?")
        args.append(category)
    for word in query.lower().split():
        where.append("lower(name || ' ' || size || ' ' || type || ' ' || category || ' ' || notes) LIKE ?")
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
    """Add a part: from the catalog ({catalog_key, quantity, ...}) or custom fields.
    Adding a catalog part that is already in the inventory adds to its quantity."""
    key = (data.get("catalog_key") or "").strip()
    base: dict[str, Any] = {}
    dims: dict[str, Any] = {}
    holes: dict[str, Any] = {}
    if key:
        cat = catalog_part(key)
        if cat is None:
            raise PartsError("That part is not in the catalog.")
        existing = next((p for p in list_inventory() if p.get("catalog_key") == key), None)
        if existing:
            return adjust(existing["id"], int(data.get("quantity") or 0))
        base = {k: cat[k] for k in ("category", "name", "size", "type", "material")}
        dims, holes = cat["dims"], cat["holes"]
    base.update(_clean({**{"quantity": 0}, **data}, partial=bool(key)))
    if isinstance(data.get("dims"), dict):
        dims = data["dims"]
    if isinstance(data.get("holes"), dict):
        holes = data["holes"]
    pid = "p_" + secrets.token_hex(4)
    now = time.time()
    with db.tx() as c:
        c.execute(
            "INSERT INTO parts (id, catalog_key, category, name, size, type, material, dims, holes, quantity, low_stock, "
            "bambu_url, amazon_url, other_url, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, key or None, base.get("category") or "other", base["name"], base.get("size", ""), base.get("type", ""),
             base.get("material", ""), json.dumps(dims), json.dumps(holes), base.get("quantity", 0), base.get("low_stock"),
             base.get("bambu_url", ""), base.get("amazon_url", ""), base.get("other_url", ""), base.get("notes", ""), now, now),
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
    """Inventory first, then catalog parts not in the inventory, as compact lines."""
    inv = list_inventory(query, category)
    have_keys = {p.get("catalog_key") for p in inv}
    words = query.lower().split()
    cat = [p for p in catalog()["parts"]
           if p["key"] not in have_keys and (not category or p["category"] == category)
           and all(w in f"{p['name']} {p['size']} {p['type']} {p['category']}".lower() for w in words)]
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
            "part": ref, "quantity": need, "use": ln["use"], "source": source,
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
