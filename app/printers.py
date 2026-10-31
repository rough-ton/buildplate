"""Printer presets used to pre-fill a new profile.

Build volumes are the manufacturers' published figures in millimetres. They are a
starting point: users can edit every value, and should check their own machine.
"""

from __future__ import annotations

import re

PRESETS: list[dict] = [
    {"brand": "Anycubic", "model": "Kobra 3", "bed_x": 250, "bed_y": 250, "bed_z": 260},
    {"brand": "Bambu Lab", "model": "A1", "bed_x": 256, "bed_y": 256, "bed_z": 256},
    {"brand": "Bambu Lab", "model": "A1 mini", "bed_x": 180, "bed_y": 180, "bed_z": 180},
    {"brand": "Bambu Lab", "model": "P1P / P1S", "bed_x": 256, "bed_y": 256, "bed_z": 256},
    {"brand": "Bambu Lab", "model": "X1 Carbon / X1E", "bed_x": 256, "bed_y": 256, "bed_z": 256},
    {"brand": "Creality", "model": "Ender 3 V2", "bed_x": 220, "bed_y": 220, "bed_z": 250},
    {"brand": "Creality", "model": "Ender 3 V3 SE", "bed_x": 220, "bed_y": 220, "bed_z": 250},
    {"brand": "Creality", "model": "K1 / K1C", "bed_x": 220, "bed_y": 220, "bed_z": 250},
    {"brand": "Creality", "model": "K1 Max", "bed_x": 300, "bed_y": 300, "bed_z": 300},
    {"brand": "Elegoo", "model": "Centauri Carbon", "bed_x": 256, "bed_y": 256, "bed_z": 256},
    {"brand": "Elegoo", "model": "Neptune 4 Pro", "bed_x": 225, "bed_y": 225, "bed_z": 265},
    {"brand": "Prusa", "model": "CORE One", "bed_x": 250, "bed_y": 220, "bed_z": 270},
    {"brand": "Prusa", "model": "MINI+", "bed_x": 180, "bed_y": 180, "bed_z": 180},
    {"brand": "Prusa", "model": "MK3S+", "bed_x": 250, "bed_y": 210, "bed_z": 210},
    {"brand": "Prusa", "model": "MK4 / MK4S", "bed_x": 250, "bed_y": 210, "bed_z": 220},
    {"brand": "Prusa", "model": "XL", "bed_x": 360, "bed_y": 360, "bed_z": 360},
    {"brand": "Sovol", "model": "SV06", "bed_x": 220, "bed_y": 220, "bed_z": 250},
]


def presets() -> list[dict]:
    out = []
    for p in PRESETS:
        out.append(
            {
                "key": re.sub(r"[^a-z0-9]+", "-", f"{p['brand']} {p['model']}".lower()).strip("-"),
                "name": f"{p['brand']} {p['model']}",
                "brand": p["brand"],
                "bed_x": p["bed_x"],
                "bed_y": p["bed_y"],
                "bed_z": p["bed_z"],
                "nozzle": p.get("nozzle", 0.4),
                "layer_height": 0.2,
                "clearance": 0.25,
                "max_overhang": 45,
            }
        )
    return out
