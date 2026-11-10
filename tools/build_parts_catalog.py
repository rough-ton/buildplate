#!/usr/bin/env python3
"""Build app/data/parts_catalog.json: common 3D printing hardware with the hole and
pocket sizes the design agent needs to model each part correctly.

The range follows what printing hardware stores (such as the Bambu Lab Store's
Hardware Parts) and hobby suppliers stock: metric screws, heat-set inserts, nuts,
washers, magnets, bearings, standoffs and a few motion parts. Dimensions come from the
standards noted below; FDM-specific allowances (press-fit and pocket clearances) are
typical values that work on most printers, and the agent adds the active printer's
clearance where it matters.

Run it after editing the tables below:

    python3 tools/build_parts_catalog.py
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "data" / "parts_catalog.json"

# ISO 273 medium clearance holes; thread-forming pilot holes for printed plastic.
METRIC = {
    #      clearance, pilot in plastic, SHCS head d/h (ISO 4762), button head d/h (ISO 7380), countersunk head d (ISO 10642)
    "M2":   (2.4, 1.8, 3.8, 2.0, 3.5, 1.3, 4.4),
    "M2.5": (2.9, 2.2, 4.5, 2.5, 4.7, 1.5, 5.5),
    "M3":   (3.4, 2.7, 5.5, 3.0, 5.7, 1.65, 6.72),
    "M4":   (4.5, 3.6, 7.0, 4.0, 7.6, 2.2, 8.96),
    "M5":   (5.5, 4.5, 8.5, 5.0, 9.5, 2.75, 11.2),
}
LENGTHS = {
    "M2": [4, 6, 8, 10],
    "M2.5": [6, 8, 10, 12],
    "M3": [6, 8, 10, 12, 16, 20, 25, 30],
    "M4": [8, 10, 12, 16, 20, 25],
    "M5": [10, 16, 20, 25],
}
# ISO 4032 hex nuts / ISO 10511 nyloc: width across flats, thickness, nyloc thickness
NUTS = {"M2": (4.0, 1.6, None), "M2.5": (5.0, 2.0, None), "M3": (5.5, 2.4, 4.0), "M4": (7.0, 3.2, 5.0), "M5": (8.0, 4.0, 5.0)}
# ISO 7089 washers: outer diameter, thickness
WASHERS = {"M3": (7.0, 0.5), "M4": (9.0, 0.8), "M5": (10.0, 1.0)}
# Heat-set inserts (common "short" brass inserts): length, recommended hole diameter
INSERTS = {"M2": (3.0, 3.2), "M2.5": (4.0, 3.6), "M3": (5.7, 4.0), "M4": (8.1, 5.6), "M5": (9.5, 6.4)}
# Neodymium disc magnets: diameter x thickness (mm)
MAGNETS = [(6, 2), (6, 3), (8, 3), (10, 2), (10, 3), (12, 3), (15, 3), (20, 3)]
# Ball bearings: designation, bore, outer diameter, width
BEARINGS = [("623ZZ", 3, 10, 4), ("625ZZ", 5, 16, 5), ("MR105ZZ", 5, 10, 4), ("688ZZ", 8, 16, 5),
            ("608ZZ", 8, 22, 7), ("6800ZZ", 10, 19, 5), ("6001ZZ", 12, 28, 8)]
STANDOFFS = {"M2.5": [6, 10, 15], "M3": [5, 10, 15, 20]}

SCREW_TYPES = {
    "socket head": ("SHCS", "socket head cap screw"),
    "button head": ("BHCS", "button head socket screw"),
    "countersunk": ("FHCS", "countersunk flat head screw"),
}


def r(x: float) -> float:
    return round(x, 2)


def screws() -> list[dict]:
    out = []
    for size, (clear, pilot, shd, shh, bhd, bhh, csd) in METRIC.items():
        d = float(size[1:])
        for head, (code, words) in SCREW_TYPES.items():
            for length in LENGTHS[size]:
                if head == "button head" and size == "M2" or head == "countersunk" and size == "M2" and length < 6:
                    continue
                holes = {"clearance_d": clear, "pilot_in_plastic_d": pilot, "min_thread_engagement": r(d * 1.5)}
                if head == "socket head":
                    dims = {"thread_d": d, "length": length, "head_d": shd, "head_h": shh}
                    holes |= {"counterbore_d": r(shd + 0.6), "counterbore_depth": r(shh + 0.2)}
                elif head == "button head":
                    dims = {"thread_d": d, "length": length, "head_d": bhd, "head_h": bhh}
                    holes |= {"counterbore_d": r(bhd + 0.6), "counterbore_depth": r(bhh + 0.2)}
                else:
                    dims = {"thread_d": d, "length": length, "head_d": csd, "head_angle": 90}
                    holes |= {"countersink_d": r(csd + 0.4), "countersink_angle": 90}
                out.append({
                    "key": f"screw-{code.lower()}-{size.lower()}x{length}",
                    "category": "screw", "name": f"{size} x {length} {words}", "size": f"{size} x {length}",
                    "type": head, "material": "stainless steel", "dims": dims, "holes": holes,
                    "search": f"{size}x{length} {words} stainless",
                })
    return out


def inserts() -> list[dict]:
    return [{
        "key": f"insert-{s.lower()}x{length}",
        "category": "heat-set insert", "name": f"{s} x {length} heat-set insert", "size": f"{s} x {length}",
        "type": "knurled brass", "material": "brass",
        "dims": {"thread_d": float(s[1:]), "length": length},
        "holes": {"insert_hole_d": hole, "insert_hole_depth": r(length + 1.0), "note": "Melt in with a soldering iron; leave 1 mm below for displaced plastic."},
        "search": f"{s} heat set insert brass {length}mm 3D printing",
    } for s, (length, hole) in INSERTS.items()]


def nuts() -> list[dict]:
    out = []
    for s, (af, t, nyloc_t) in NUTS.items():
        out.append({
            "key": f"nut-hex-{s.lower()}", "category": "nut", "name": f"{s} hex nut", "size": s, "type": "hex nut",
            "material": "stainless steel", "dims": {"thread_d": float(s[1:]), "across_flats": af, "thickness": t},
            "holes": {"nut_trap_across_flats": r(af + 0.3), "nut_trap_depth": r(t + 0.2), "clearance_d": METRIC[s][0]},
            "search": f"{s} hex nut stainless",
        })
        if nyloc_t:
            out.append({
                "key": f"nut-nyloc-{s.lower()}", "category": "nut", "name": f"{s} nyloc nut", "size": s, "type": "nyloc nut",
                "material": "stainless steel", "dims": {"thread_d": float(s[1:]), "across_flats": af, "thickness": nyloc_t},
                "holes": {"nut_trap_across_flats": r(af + 0.3), "nut_trap_depth": r(nyloc_t + 0.2), "clearance_d": METRIC[s][0]},
                "search": f"{s} nylon insert lock nut stainless",
            })
    return out


def washers() -> list[dict]:
    return [{
        "key": f"washer-{s.lower()}", "category": "washer", "name": f"{s} flat washer", "size": s, "type": "flat washer",
        "material": "stainless steel", "dims": {"inner_d": r(METRIC[s][0] - 0.2), "outer_d": od, "thickness": t},
        "holes": {"recess_d": r(od + 0.4), "recess_depth": r(t + 0.1)},
        "search": f"{s} flat washer stainless",
    } for s, (od, t) in WASHERS.items()]


def magnets() -> list[dict]:
    return [{
        "key": f"magnet-disc-{d}x{t}", "category": "magnet", "name": f"{d} x {t} mm disc magnet", "size": f"{d} x {t} mm",
        "type": "neodymium disc (N52)", "material": "neodymium",
        "dims": {"diameter": d, "thickness": t},
        "holes": {"press_fit_pocket_d": r(d + 0.1), "glue_pocket_d": r(d + 0.3), "pocket_depth": r(t + 0.2),
                  "note": "Mark polarity before inserting; magnets in a pair must face opposite poles."},
        "search": f"{d}x{t}mm neodymium disc magnet N52",
    } for d, t in MAGNETS]


def bearings() -> list[dict]:
    return [{
        "key": f"bearing-{name.lower()}", "category": "bearing", "name": f"{name} ball bearing", "size": f"{bore} x {od} x {w} mm",
        "type": "deep groove, shielded", "material": "chrome steel",
        "dims": {"bore": bore, "outer_d": od, "width": w},
        "holes": {"seat_d": r(od + 0.15), "seat_depth": w, "shaft_d": bore, "note": "Seat diameter is a light press fit; add the printer's clearance if the fit is tight."},
        "search": f"{name} ball bearing {bore}x{od}x{w}",
    } for name, bore, od, w in BEARINGS]


def standoffs() -> list[dict]:
    return [{
        "key": f"standoff-{s.lower()}x{length}", "category": "standoff", "name": f"{s} x {length} brass standoff", "size": f"{s} x {length}",
        "type": "female-female hex", "material": "brass", "dims": {"thread_d": float(s[1:]), "length": length},
        "holes": {"clearance_d": METRIC[s][0]},
        "search": f"{s} x {length}mm brass standoff female",
    } for s, lengths in STANDOFFS.items() for length in lengths]


def motion() -> list[dict]:
    return [
        {"key": "rod-smooth-8mm", "category": "rod", "name": "8 mm smooth rod", "size": "8 mm", "type": "hardened linear rod",
         "material": "chrome steel", "dims": {"diameter": 8}, "holes": {"rod_hole_d": 8.2, "press_fit_d": 8.05},
         "search": "8mm smooth linear rod hardened"},
        {"key": "belt-gt2-6mm", "category": "belt", "name": "GT2 belt, 6 mm wide", "size": "6 mm", "type": "GT2 timing belt",
         "material": "rubber with glass fibre", "dims": {"width": 6, "pitch": 2}, "holes": {"belt_slot_w": 6.6, "belt_slot_t": 1.6},
         "search": "GT2 timing belt 6mm"},
        {"key": "ptfe-4x2", "category": "PTFE tube", "name": "PTFE tube 4 x 2 mm", "size": "4 x 2 mm", "type": "PTFE tube",
         "material": "PTFE", "dims": {"outer_d": 4, "inner_d": 2}, "holes": {"tube_hole_d": 4.1, "push_fit_d": 4.0},
         "search": "PTFE tube 4mm OD 2mm ID"},
        {"key": "spring-compression-6x20", "category": "spring", "name": "Compression spring 6 x 20 mm", "size": "6 x 20 mm",
         "type": "compression spring", "material": "spring steel", "dims": {"outer_d": 6, "free_length": 20, "wire_d": 0.6},
         "holes": {"spring_pocket_d": 6.6}, "search": "compression spring 6mm OD 20mm long"},
        {"key": "rubber-foot-10x3", "category": "rubber foot", "name": "Adhesive rubber foot 10 x 3 mm", "size": "10 x 3 mm",
         "type": "self-adhesive bumper", "material": "silicone rubber", "dims": {"diameter": 10, "height": 3},
         "holes": {"recess_d": 10.4, "recess_depth": 1.0}, "search": "self adhesive rubber feet 10mm"},
    ]


def main() -> None:
    parts = screws() + inserts() + nuts() + washers() + magnets() + bearings() + standoffs() + motion()
    keys = [p["key"] for p in parts]
    assert len(keys) == len(set(keys)), "duplicate keys"
    categories = sorted({p["category"] for p in parts})
    OUT.write_text(json.dumps({"version": 1, "categories": categories, "parts": parts}, indent=1) + "\n")
    print(f"wrote {len(parts)} parts in {len(categories)} categories to {OUT}")


if __name__ == "__main__":
    main()
