"""Printer catalog used to create profiles.

The catalog in data/printer_db.json is generated from OrcaSlicer's printer profiles by
tools/build_printer_db.py. It holds factual machine data only: manufacturer, model,
printable area, printable height, bed shape and nozzle sizes. Printable volumes are the
slicer's usable values, which can be slightly smaller than marketing figures.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

DB_FILE = Path(__file__).parent / "data" / "printer_db.json"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@lru_cache(maxsize=1)
def catalog() -> dict[str, Any]:
    raw = json.loads(DB_FILE.read_text(encoding="utf-8"))
    printers = []
    for p in raw["printers"]:
        printers.append({**p, "key": _slug(f"{p['vendor']} {p['model']}")})
    vendors: dict[str, int] = {}
    for p in printers:
        vendors[p["vendor"]] = vendors.get(p["vendor"], 0) + 1
    return {
        "source": raw.get("source", ""),
        "generated": raw.get("generated", ""),
        "vendors": [{"name": v, "count": n} for v, n in sorted(vendors.items(), key=lambda kv: kv[0].lower())],
        "printers": printers,
    }


def find(key: str) -> dict[str, Any] | None:
    for p in catalog()["printers"]:
        if p["key"] == key:
            return p
    return None


def profile_from_catalog(key: str, nozzle: float) -> dict[str, Any]:
    p = find(key)
    if p is None:
        raise KeyError(key)
    nz = f"{nozzle:g}"
    return {
        "id": f"{key}-{nz.replace('.', '')}",
        "name": f"{p['display']} {nz} nozzle",
        "preset": key,
        "shape": p.get("shape", "rect"),
        "type": p.get("type", "generic"),
        "bed_x": p["bed_x"],
        "bed_y": p["bed_y"],
        "bed_z": p["bed_z"],
        "nozzle": nozzle,
        "layer_height": round(min(max(nozzle * 0.5, 0.08), 0.4), 2),
        "clearance": 0.2 if nozzle <= 0.25 else 0.25 if nozzle <= 0.5 else 0.35,
        "max_overhang": 45,
    }
