"""Machine style for each catalog printer, used to pick its illustration.

Types: bedslinger, cantilever, corexy_open, corexy_enclosed, delta, generic.

Order of precedence:
1. Round bed -> delta.
2. The first matching rule in RULES (vendor regex, model regex).
3. OrcaSlicer's printer_structure field (i3 -> bedslinger, corexy/hbot -> corexy_open,
   delta -> delta).
4. Model-name hints for common bed slingers.
5. generic.
"""

from __future__ import annotations

import re

TYPES = ("bedslinger", "cantilever", "corexy_open", "corexy_enclosed", "delta", "generic")

RULES: list[tuple[str, str, str]] = [
    # Bambu Lab
    ("Bambu Lab", r"\bA1 mini\b", "cantilever"),
    ("Bambu Lab", r"\bA1\b", "bedslinger"),
    ("Bambu Lab", r"\bP1P\b", "corexy_open"),
    ("Bambu Lab", r"\b(P1S|P2S|X1|X1 Carbon|X1E|X2D|H2D|H2D Pro|H2S|H2C)\b", "corexy_enclosed"),
    # Prusa
    ("Prusa", r"\bMINI", "cantilever"),
    ("Prusa", r"\bCORE One\b", "corexy_enclosed"),
    ("Prusa", r"\bXL\b", "corexy_open"),
    ("Prusa", r"\bMK\d", "bedslinger"),
    # Creality
    ("Creality", r"\bK(1|1C|1 Max|1 SE|2|2 Plus|2 Pro)(\b|_)", "corexy_enclosed"),
    ("Creality", r"\bSermoon\b", "corexy_enclosed"),
    ("Creality", r"\b(Hi|SPARKX)\b", "bedslinger"),
    # Enclosed by design across most of a range
    ("Qidi", r".", "corexy_enclosed"),
    ("Raise3D", r".", "corexy_enclosed"),
    ("UltiMaker", r".", "corexy_enclosed"),
    ("Dremel", r".", "corexy_enclosed"),
    ("Elegoo", r"\bCentauri", "corexy_enclosed"),
    ("Anycubic", r"\bKobra S1\b", "corexy_enclosed"),
    ("Flashforge", r"\b(Adventurer 5M Pro|Adventurer 3|Adventurer 4|Creator|Guider)\b", "corexy_enclosed"),
    ("Flashforge", r"\b(Adventurer 5M|AD5X)\b", "corexy_open"),
    # Voron and other CoreXY kits
    ("Voron", r"\bSwitchwire\b", "bedslinger"),
    ("Voron", r".", "corexy_enclosed"),
    ("Ratrig", r"\bV-Core\b", "corexy_open"),
    ("Sovol", r"\bSV08\b", "corexy_open"),
    ("Vzbot", r".", "corexy_open"),
    ("Ratrig", r"\bV-(Minion|Cast)\b", "corexy_open"),
    ("Tronxy", r"\bX5SA\b", "corexy_open"),
    ("TwoTrees", r"\bSP-5\b", "corexy_open"),
    ("Kingroon", r"\bKLP1\b", "corexy_open"),
    ("Vivedino", r"\bTroodon\b", "corexy_enclosed"),
    ("Tiertime", r"\bUP\d", "corexy_enclosed"),
    ("Anycubic", r"\b4Max\b", "corexy_enclosed"),
    ("Flashforge", r"\bGuider", "corexy_enclosed"),
    ("Flashforge", r"\bAdventurer A5\b", "bedslinger"),
    # Bed slingers
    ("Anycubic", r"\bChiron\b", "bedslinger"),
    ("BIQU", r"\b(B1|BX|Hurakan)\b", "bedslinger"),
    ("Comgrow", r"\bT\d00\b", "bedslinger"),
    ("Eryone", r"\bER20\b", "bedslinger"),
    ("Geeetech", r"\b(A\d0|Mizar)", "bedslinger"),
    ("Kingroon", r"\bKP3S\b", "bedslinger"),
    ("Longer", r"\bLK\d", "bedslinger"),
    ("Lulzbot", r".", "bedslinger"),
    ("Snapmaker", r"\b(A250|A350|Artisan)\b", "bedslinger"),
    ("Wanhao", r"\bD(9|12)\b", "bedslinger"),
    ("Wanhao France", r"\bD12\b", "bedslinger"),
    ("DeltaMaker", r".", "delta"),
]

BEDSLINGER_HINTS = re.compile(r"\b(Ender|Neptune|Kobra|Sidewinder|Genius|Hornet|Aquila|SV0[1-7]|i3|Mega|Vyper|CR-10|CR-6)\b", re.I)


def classify(vendor: str, model: str, shape: str, structure: str | None) -> str:
    if shape == "round" or re.search(r"\bdelta", f"{vendor} {model}", re.I):
        return "delta"
    for v, pattern, kind in RULES:
        if v.lower() == vendor.lower() and re.search(pattern, model, re.I):
            return kind
    s = (structure or "").lower()
    if s == "delta":
        return "delta"
    if s == "i3":
        return "bedslinger"
    if s in ("corexy", "hbot"):
        return "corexy_open"
    if BEDSLINGER_HINTS.search(model):
        return "bedslinger"
    return "generic"
