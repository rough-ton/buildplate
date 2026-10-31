#!/usr/bin/env python3
"""Build app/data/printer_db.json from OrcaSlicer's printer profiles.

Only factual machine data is extracted: manufacturer, model, build volume, bed shape
and available nozzle sizes. Run it when you want a newer printer list:

    git clone --depth 1 --filter=blob:none --sparse https://github.com/SoftFever/OrcaSlicer /tmp/orca
    git -C /tmp/orca sparse-checkout set resources/profiles
    python3 tools/build_printer_db.py /tmp/orca
"""

from __future__ import annotations

import datetime as dt
import json
import math
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from printer_types import classify  # noqa: E402

SKIP_VENDORS = {"Custom", "OrcaArena", "OrcaFilamentLibrary"}
VENDOR_NAMES = {
    "BBL": "Bambu Lab",
    "LH": "LH Stinger",
    "iQ": "iQ",
    "re3D": "re3D",
    "CONSTRUCT3D": "Construct3D",
    "LONGER": "Longer",
    "WEMAKE3D": "WeMake3D",
    "UltiMaker": "UltiMaker",
}


def vendor_name(folder: str, index: dict) -> str:
    if folder in VENDOR_NAMES:
        return VENDOR_NAMES[folder]
    return folder


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [x for x in str(v).replace(";", ",").split(",") if x.strip()]


def to_float(v) -> float | None:
    import re

    m = re.match(r"\s*(\d+(?:\.\d+)?)", str(v))
    return float(m.group(1)) if m else None


def parse_area(points) -> tuple[float, float, str] | None:
    pts = []
    for p in as_list(points):
        try:
            x, y = p.split("x")
            pts.append((float(x), float(y)))
        except ValueError:
            continue
    if len(pts) < 3:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    shape = "rect"
    if len(pts) > 8:
        cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        r = [math.hypot(x - cx, y - cy) for x, y in pts]
        if max(r) - min(r) < 0.05 * max(r):
            shape = "round"
    return round(w, 1), round(h, 1), shape


def build(profiles_dir: Path) -> list[dict]:
    printers: dict[tuple[str, str], dict] = {}
    for index_file in sorted(profiles_dir.glob("*.json")):
        folder = index_file.stem
        vdir = profiles_dir / folder
        if folder in SKIP_VENDORS or not vdir.is_dir():
            continue
        index = load(index_file)
        if "machine_list" not in index:
            continue
        vendor = vendor_name(folder, index)
        by_name = {m["name"]: vdir / m["sub_path"] for m in index.get("machine_list", []) + index.get("machine_model_list", [])}
        cache: dict[str, dict] = {}

        def resolve(name: str, depth: int = 0) -> dict:
            if name in cache:
                return cache[name]
            path = by_name.get(name) or vdir / "machine" / f"{name}.json"
            data = load(path)
            if data.get("inherits") and depth < 12:
                merged = dict(resolve(data["inherits"], depth + 1))
                merged.update(data)
                data = merged
            cache[name] = data
            return data

        models = {}
        for m in index.get("machine_model_list", []):
            md = load(vdir / m["sub_path"])
            if md.get("machine_tech", "FFF") not in ("FFF", "FDM"):
                continue
            nozzles = sorted({f for f in (to_float(n) for n in as_list(md.get("nozzle_diameter"))) if f})
            models[md.get("name", m["name"])] = nozzles

        for m in index.get("machine_list", []):
            data = resolve(m["name"])
            if str(data.get("instantiation", "")).lower() != "true" or data.get("type") != "machine":
                continue
            model = data.get("printer_model") or ""
            if not model or (models and model not in models):
                continue
            area = parse_area(data.get("printable_area"))
            height = to_float((as_list(data.get("printable_height")) or [""])[0])
            if height is None:
                continue
            if not area or area[0] < 20 or area[1] < 20 or height < 20:
                continue
            nozzle = to_float((as_list(data.get("nozzle_diameter")) or ["0.4"])[0]) or 0.4
            key = (vendor, model)
            entry = printers.get(key)
            if entry is None or (abs(nozzle - 0.4) < 1e-6 and abs(entry["_n"] - 0.4) > 1e-6):
                printers[key] = {
                    "vendor": vendor,
                    "model": model,
                    "bed_x": area[0],
                    "bed_y": area[1],
                    "bed_z": round(height, 1),
                    "shape": area[2],
                    "nozzles": models.get(model) or [nozzle],
                    "_n": nozzle,
                    "_structure": data.get("printer_structure"),
                }
            elif nozzle not in printers[key]["nozzles"]:
                printers[key]["nozzles"] = sorted(set(printers[key]["nozzles"]) | {nozzle})

    out = []
    for p in printers.values():
        p.pop("_n")
        p["type"] = classify(p["vendor"], p["model"], p["shape"], p.pop("_structure", None))
        if not p["nozzles"]:
            p["nozzles"] = [0.4]
        p["display"] = p["model"] if p["model"].lower().startswith(p["vendor"].lower()) else f"{p['vendor']} {p['model']}"
        out.append(p)
    out.sort(key=lambda p: (p["vendor"].lower(), p["model"].lower()))
    return out


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    repo = Path(sys.argv[1])
    profiles = repo / "resources" / "profiles"
    printers = build(profiles)
    try:
        commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    except Exception:
        commit = ""
    dest = Path(__file__).resolve().parent.parent / "app" / "data" / "printer_db.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "OrcaSlicer printer profiles (https://github.com/SoftFever/OrcaSlicer)",
        "source_commit": commit,
        "generated": dt.date.today().isoformat(),
        "count": len(printers),
        "printers": printers,
    }
    dest.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    vendors = sorted({p["vendor"] for p in printers})
    print(f"Wrote {len(printers)} printers from {len(vendors)} manufacturers to {dest}")


if __name__ == "__main__":
    main()
