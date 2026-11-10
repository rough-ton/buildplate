"""Reference files a Studio request can carry: a picture, a mesh (STL or 3MF), OpenSCAD
source, or G-code. Each is analysed once on upload into what the design agent needs:
a text summary for every model, the source for SCAD, and a picture for vision models
(the image itself, or a rendered preview of a mesh, SCAD file or G-code toolpath).

The agent treats a reference as something to match, never as the design to edit."""

from __future__ import annotations

import base64
import io
import json
import re
import secrets
from pathlib import Path
from typing import Any

from . import config, openscad

KINDS = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
    ".stl": "mesh", ".3mf": "mesh",
    ".scad": "scad",
    ".gcode": "gcode", ".gco": "gcode", ".g": "gcode",
}
KIND_LABELS = {"image": "Picture", "mesh": "3D model", "scad": "OpenSCAD source", "gcode": "G-code"}
MAX_BYTES = {"image": 15 * 1024**2, "mesh": 50 * 1024**2, "scad": 512 * 1024, "gcode": 50 * 1024**2}
MAX_IMAGE_SIDE = 1568  # larger pictures cost more tokens without helping the model
MAX_SOURCE_CHARS = 40_000
_ID = re.compile(r"^ref_[A-Za-z0-9_-]{16}$")


class ReferenceError(ValueError):
    """A file that cannot be used as a reference; the message is for the user."""


def _dir() -> Path:
    d = config.DATA_DIR / "references"
    d.mkdir(parents=True, exist_ok=True)
    return d


def kind_of(name: str) -> str | None:
    return KINDS.get(Path(name).suffix.lower())


async def save(name: str, data: bytes) -> dict[str, Any]:
    """Store and analyse an uploaded file. Returns its public description."""
    name = Path(name or "").name[:120] or "reference"
    kind = kind_of(name)
    if kind is None:
        raise ReferenceError("Attach a picture (PNG, JPG, WebP), an STL, 3MF, SCAD or G-code file.")
    if not data:
        raise ReferenceError("The file is empty.")
    if len(data) > MAX_BYTES[kind]:
        raise ReferenceError(f"{KIND_LABELS[kind]} files can be up to {MAX_BYTES[kind] / 1024**2:g} MB.")
    ref_id = "ref_" + secrets.token_urlsafe(12)[:16]
    folder = _dir() / ref_id
    folder.mkdir()
    src = folder / ("source" + Path(name).suffix.lower())
    src.write_bytes(data)
    try:
        meta = await _ANALYSE[kind](src, folder)
    except ReferenceError:
        _remove(folder)
        raise
    except Exception as exc:
        _remove(folder)
        raise ReferenceError(f"Could not read {name}: {str(exc)[:200]}") from exc
    meta.update({"id": ref_id, "name": name, "kind": kind, "bytes": len(data)})
    (folder / "meta.json").write_text(json.dumps(meta))
    return public(meta)


def public(meta: dict[str, Any]) -> dict[str, Any]:
    """What Studio shows and what a message stores."""
    out = {k: meta[k] for k in ("id", "name", "kind", "summary") if k in meta}
    out["kind_label"] = KIND_LABELS.get(meta.get("kind"), "File")
    out["image_url"] = f"/api/references/{meta['id']}/image" if meta.get("image") else None
    return out


def load(ref_id: str | None) -> dict[str, Any] | None:
    """Everything the agent needs: meta, plus image_b64 and source when there are any."""
    if not ref_id or not _ID.match(ref_id):
        return None
    folder = _dir() / ref_id
    try:
        meta = json.loads((folder / "meta.json").read_text())
    except (OSError, ValueError):
        return None
    if meta.get("image"):
        meta["image_b64"] = base64.b64encode((folder / meta["image"]).read_bytes()).decode()
    if meta["kind"] == "scad":
        meta["source"] = next(folder.glob("source.*")).read_text(errors="replace")[:MAX_SOURCE_CHARS]
    return meta


def image_path(ref_id: str) -> Path | None:
    ref = load(ref_id)
    return (_dir() / ref_id / ref["image"]) if ref and ref.get("image") else None


def _remove(folder: Path) -> None:
    for f in folder.glob("*"):
        f.unlink(missing_ok=True)
    folder.rmdir()


# ---- what the model sees -------------------------------------------------------------
INTRO = (
    "The user attached a reference file. Use it as ground truth for what they want (shape, proportions, "
    "features, size), but write your own parametric OpenSCAD design; it is not the code to edit."
)


def prompt_text(ref: dict[str, Any], with_image: bool) -> str:
    """The reference as text, for the request message."""
    lines = [INTRO, f"Reference: {ref['name']} ({KIND_LABELS[ref['kind']].lower()}). {ref['summary']}"]
    if ref.get("image") and with_image:
        lines.append(ref.get("image_note") or "The attached image shows it.")
    if ref.get("source"):
        lines += ["Reference OpenSCAD source (read-only; do not treat it as the current design):",
                  "```openscad-reference", ref["source"].strip(), "```"]
    return "\n".join(lines)


def history_note(r: dict[str, Any]) -> str:
    """How a reference attached to an earlier turn is recalled later: name and summary only."""
    return f"(Attached reference: {r.get('name')}, {r.get('summary', '')})"


# ---- analysis ------------------------------------------------------------------------
async def _image(src: Path, folder: Path) -> dict[str, Any]:
    from PIL import Image, UnidentifiedImageError

    try:
        img = Image.open(src)
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ReferenceError("That file is not a picture Buildplate can read.") from exc
    w, h = img.size
    img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    img.save(folder / "image.png", "PNG", optimize=True)
    return {"summary": f"A picture, {w} x {h} px.", "image": "image.png",
            "image_note": "The attached picture is the reference."}


async def _mesh(src: Path, folder: Path) -> dict[str, Any]:
    import trimesh

    mesh = trimesh.load(str(src), force="mesh")
    if mesh is None or not len(getattr(mesh, "faces", [])):
        raise ReferenceError("That file has no 3D geometry in it.")
    ext = [round(float(x), 1) for x in mesh.extents]
    try:
        bodies = mesh.body_count if len(mesh.faces) < 400_000 else None
    except Exception:
        bodies = None
    parts = [f"Size {ext[0]:g} x {ext[1]:g} x {ext[2]:g} mm (X x Y x Z)", f"{len(mesh.faces):,} triangles"]
    if mesh.is_watertight:
        parts.append(f"volume {float(mesh.volume) / 1000:.1f} cm3")
    if bodies and bodies > 1:
        parts.append(f"{bodies} separate bodies")
    parts.append(_shape_hint(ext))
    meta: dict[str, Any] = {"summary": ", ".join(parts) + ".", "size_mm": ext}
    # render the 4-view sheet the agent already uses for its own previews
    stl = folder / "mesh.stl"
    mesh.export(str(stl))
    try:
        png, _ = await openscad.preview_sheet(f'import("{stl.as_posix()}", convexity=10);')
        (folder / "image.png").write_bytes(png)
        meta["image"] = "image.png"
        meta["image_note"] = "The attached image is a render of the reference model (ISO, FRONT, RIGHT, TOP views)."
    except openscad.RenderError:
        pass
    return meta


def _shape_hint(ext: list[float]) -> str:
    lo, mid, hi = sorted(ext)
    if hi and lo / hi > 0.6:
        return "roughly cube-like proportions"
    if hi and mid / hi > 0.4 and lo / hi < 0.15:
        return "a flat, plate-like shape"
    if hi and mid / hi < 0.25:
        return "a long, slender shape"
    return "a block-like shape"


async def _scad(src: Path, folder: Path) -> dict[str, Any]:
    code = src.read_bytes().decode("utf-8", errors="replace")
    params = openscad.parse_parameters(code)
    modules = re.findall(r"^\s*module\s+(\w+)", code, re.M)
    bits = [f"{len(code.splitlines())} lines of OpenSCAD"]
    if params:
        shown = ", ".join(f"{p['name']} = {p['value']}" for p in params[:12])
        bits.append(f"{len(params)} parameters ({shown}{', ...' if len(params) > 12 else ''})")
    if modules:
        bits.append("modules: " + ", ".join(modules[:12]) + (", ..." if len(modules) > 12 else ""))
    if len(code) > MAX_SOURCE_CHARS:
        bits.append(f"source truncated to the first {MAX_SOURCE_CHARS:,} characters")
    meta: dict[str, Any] = {"summary": "; ".join(bits) + "."}
    try:
        png, _ = await openscad.preview_sheet(code)
        (folder / "image.png").write_bytes(png)
        meta["image"] = "image.png"
        meta["image_note"] = "The attached image is a render of the reference source (ISO, FRONT, RIGHT, TOP views)."
    except openscad.RenderError:
        pass
    return meta


_MOVE = re.compile(r"^G[01]\b", re.I)
_AXIS = re.compile(r"([XYZEF])(-?\d*\.?\d+)", re.I)


def parse_gcode(text: str) -> dict[str, Any]:
    """Recover the printed shape from a toolpath: the extruding moves only (travel,
    retraction and purge moves outside the print are what slicers add around it)."""
    x = y = z = e = 0.0
    absolute, abs_e = True, True
    segs: list[tuple[float, float, float, float, float]] = []  # x0 y0 x1 y1 z
    layers: set[float] = set()
    meta: dict[str, str] = {}
    for raw in text.splitlines():
        line, _, comment = raw.partition(";")
        if comment and not line.strip():
            # "; generated by PrusaSlicer 2.8.1 on ..." / ";Generated with Cura_SteamEngine 5.7"
            m = re.match(r"\s*generated (?:by|with)\s+(.+?)(?:\s+on\s+\d.*)?$", comment, re.I)
            if m and "slicer" not in meta:
                meta["slicer"] = m.group(1).strip()[:80]
            continue
        line = line.strip().upper()
        if not line:
            continue
        cmd = line.split()[0]
        if cmd == "G90":
            absolute = abs_e = True
        elif cmd == "G91":
            absolute = abs_e = False
        elif cmd == "M82":
            abs_e = True
        elif cmd == "M83":
            abs_e = False
        elif cmd == "G92":
            for k, v in _AXIS.findall(line):
                if k.upper() == "E":
                    e = float(v)
        elif _MOVE.match(cmd):
            vals = {k.upper(): float(v) for k, v in _AXIS.findall(line)}
            nx, ny, nz = ((vals[k] if absolute else cur + vals[k]) if k in vals else cur for k, cur in (("X", x), ("Y", y), ("Z", z)))
            extruding = False
            if "E" in vals:
                ne = vals["E"] if abs_e else e + vals["E"]
                extruding = ne > e + 1e-6
                e = ne
            if extruding and (nx != x or ny != y):
                segs.append((x, y, nx, ny, nz))
                layers.add(round(nz, 3))
            x, y, z = nx, ny, nz
    if not segs:
        raise ReferenceError("No printed moves found in that G-code.")
    xs = [s[0] for s in segs] + [s[2] for s in segs]
    ys = [s[1] for s in segs] + [s[3] for s in segs]
    zs = sorted(layers)
    heights = [round(b - a, 3) for a, b in zip(zs, zs[1:]) if b > a]
    layer_h = max(set(heights), key=heights.count) if heights else None
    return {"segments": segs, "min": [min(xs), min(ys), zs[0]], "max": [max(xs), max(ys), zs[-1]],
            "layers": len(zs), "layer_height": layer_h, "slicer": meta}


async def _gcode(src: Path, folder: Path) -> dict[str, Any]:
    g = parse_gcode(src.read_bytes().decode("utf-8", errors="replace"))
    lo, hi = g["min"], g["max"]
    size = [round(hi[i] - lo[i], 1) for i in range(2)] + [round(hi[2], 1)]
    bits = [f"Printed area {size[0]:g} x {size[1]:g} mm, {size[2]:g} mm tall (from the toolpath, so approximate)",
            f"{g['layers']} layers" + (f" of {g['layer_height']:g} mm" if g["layer_height"] else "")]
    slicer = g["slicer"].get("slicer")
    if slicer:
        bits.append(f"sliced with {slicer}")
    bits.append(_shape_hint(size))
    _draw_toolpath(g, folder / "image.png")
    return {"summary": ", ".join(bits) + ".", "size_mm": size, "image": "image.png",
            "image_note": "The attached image is the reference toolpath drawn from the G-code (TOP and FRONT views)."}


def _draw_toolpath(g: dict[str, Any], out: Path, tile: int = 512) -> None:
    from PIL import Image, ImageDraw

    lo, hi = g["min"], g["max"]
    segs = g["segments"]
    step = max(1, len(segs) // 200_000)  # enough lines to show the shape, bounded time
    sheet = Image.new("RGB", (tile * 2, tile), "white")
    draw = ImageDraw.Draw(sheet)

    def fit(ax_lo, ax_hi, ay_lo, ay_hi):
        span = max(ax_hi - ax_lo, ay_hi - ay_lo, 1e-6)
        k = (tile - 40) / span
        ox = 20 + (tile - 40 - (ax_hi - ax_lo) * k) / 2
        oy = 20 + (tile - 40 - (ay_hi - ay_lo) * k) / 2
        return k, ox, oy

    k, ox, oy = fit(lo[0], hi[0], lo[1], hi[1])
    for x0, y0, x1, y1, _ in segs[::step]:  # top view, Y up
        draw.line([(ox + (x0 - lo[0]) * k, tile - oy - (y0 - lo[1]) * k), (ox + (x1 - lo[0]) * k, tile - oy - (y1 - lo[1]) * k)], fill=(40, 90, 200))
    k, ox, oy = fit(lo[0], hi[0], 0, hi[2])
    for x0, _, x1, _, z in segs[::step]:  # front view: X across, Z up
        yy = tile - oy - z * k
        draw.line([(tile + ox + (x0 - lo[0]) * k, yy), (tile + ox + (x1 - lo[0]) * k, yy)], fill=(200, 90, 40))
    for i, label in enumerate(("TOP", "FRONT")):
        draw.rectangle([i * tile, 0, i * tile + 60, 18], fill="white")
        draw.text((i * tile + 4, 3), label, fill="black")
    buf = io.BytesIO()
    sheet.save(buf, "PNG", optimize=True)
    out.write_bytes(buf.getvalue())


_ANALYSE = {"image": _image, "mesh": _mesh, "scad": _scad, "gcode": _gcode}
