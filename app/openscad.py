"""OpenSCAD rendering, customizer parameter parsing and mesh validation."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import config
from .settings import store

VIEWS = {
    "iso": "0,0,0,55,0,25,0",
    "front": "0,0,0,90,0,0,0",
    "right": "0,0,0,90,0,90,0",
    "top": "0,0,0,0,0,0,0",
    "back_iso": "0,0,0,55,0,205,0",
}

_sem: asyncio.Semaphore | None = None
_sem_size = 0


class RenderError(Exception):
    def __init__(self, message: str, log: str = ""):
        super().__init__(message)
        self.log = log


@dataclass
class RenderResult:
    path: Path
    log: str
    warnings: list[str] = field(default_factory=list)
    cached: bool = False


# ---- environment detection ---------------------------------------------------
@lru_cache(maxsize=1)
def detect() -> dict[str, Any]:
    exe = shutil.which("openscad")
    info: dict[str, Any] = {"available": bool(exe), "path": exe, "version": "", "manifold": None, "binstl": False}
    if not exe:
        return info
    try:
        ver = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20, env=_env())
        out = (ver.stdout + ver.stderr).strip()
        m = re.search(r"OpenSCAD version (\S+)", out)
        if ver.returncode != 0 or not m:
            info["available"] = False
            info["error"] = (out.splitlines() or ["OpenSCAD did not start"])[-1][:300]
            return info
        info["version"] = m.group(1)
        helptext = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=20, env=_env())
        h = helptext.stdout + helptext.stderr
    except Exception as exc:  # pragma: no cover
        info["available"] = False
        info["error"] = str(exc)
        return info
    if "--backend" in h:
        info["manifold"] = "--backend=manifold"
    elif re.search(r"\bmanifold\b", h):
        info["manifold"] = "--enable=manifold"
    info["binstl"] = "binstl" in h
    info["xvfb"] = bool(shutil.which("xvfb-run"))
    return info


def library_dirs() -> list[Path]:
    dirs = [config.USER_LIB_DIR, config.BUNDLED_LIB_DIR, Path("/usr/share/openscad/libraries")]
    return [d for d in dirs if d.exists()]


def list_libraries() -> list[dict[str, str]]:
    seen: dict[str, dict[str, str]] = {}
    for d in library_dirs():
        for child in sorted(d.iterdir()):
            if child.is_dir() and not child.name.startswith(".") and child.name not in seen:
                seen[child.name] = {"name": child.name, "path": str(child)}
    return list(seen.values())


def _env() -> dict[str, str]:
    """Environment for OpenSCAD. HOME and XDG_CONFIG_HOME always point at a writable
    folder in the data directory: the container runs as an unprivileged user, and
    OpenSCAD aborts at startup if it cannot read or create its config folder."""
    home = config.DATA_DIR / ".home"
    (home / ".config").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    env["OPENSCADPATH"] = os.pathsep.join(str(d) for d in library_dirs())
    return env


def _semaphore() -> asyncio.Semaphore:
    global _sem, _sem_size
    n = store.get()["render"]["max_parallel"]
    if _sem is None or n != _sem_size:
        _sem, _sem_size = asyncio.Semaphore(n), n
    return _sem


# ---- parameters ---------------------------------------------------------------
_ASSIGN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*;\s*(?://\s*(.*))?$")
_SECTION = re.compile(r"^\s*/\*\s*\[(.+?)\]\s*\*/\s*$")
_STOP = re.compile(r"^\s*(module|function)\b|^\s*\{")


def _py_value(raw: str) -> tuple[Any, str] | None:
    raw = raw.strip()
    if raw in ("true", "false"):
        return raw == "true", "bool"
    if re.fullmatch(r"-?\d+(\.\d+)?([eE]-?\d+)?", raw):
        return (float(raw) if any(c in raw for c in ".eE") else int(raw)), "number"
    m = re.fullmatch(r'"((?:[^"\\]|\\.)*)"', raw)
    if m:
        return m.group(1), "string"
    if re.fullmatch(r"\[\s*-?[\d.]+(\s*,\s*-?[\d.]+)*\s*\]", raw):
        return [float(x) for x in raw.strip("[] ").split(",")], "vector"
    return None


def parse_parameters(code: str) -> list[dict[str, Any]]:
    """Extract OpenSCAD Customizer-style parameters from the top of a file."""
    params: list[dict[str, Any]] = []
    section = "Parameters"
    prev_comment = ""
    for line in code.splitlines():
        if _STOP.search(line):
            break
        sec = _SECTION.match(line)
        if sec:
            if sec.group(1).strip().lower() == "hidden":
                break
            section = sec.group(1).strip()
            continue
        stripped = line.strip()
        if stripped.startswith("//"):
            prev_comment = stripped.lstrip("/ ").strip()
            continue
        m = _ASSIGN.match(line)
        if not m:
            if stripped:
                prev_comment = ""
            continue
        name, raw, hint = m.group(1), m.group(2), (m.group(3) or "").strip()
        if name.startswith("$"):
            continue
        parsed = _py_value(raw)
        if parsed is None:
            prev_comment = ""
            continue
        value, kind = parsed
        p: dict[str, Any] = {"name": name, "value": value, "type": kind, "section": section, "label": prev_comment}
        rng = re.search(r"\[([^\]]*)\]\s*$", hint)
        if rng:
            body = rng.group(1)
            nums = body.split(":")
            if kind == "number" and len(nums) in (2, 3) and all(re.fullmatch(r"\s*-?[\d.]+\s*", n) for n in nums):
                f = [float(n) for n in nums]
                p["min"], p["max"] = f[0], f[-1]
                p["step"] = f[1] if len(f) == 3 else (1 if isinstance(value, int) else 0.1)
                p["control"] = "slider"
            elif "," in body or kind == "string":
                opts = []
                for o in body.split(","):
                    o = o.strip()
                    if ":" in o:
                        v, lbl = o.split(":", 1)
                    else:
                        v, lbl = o, o
                    v = v.strip().strip('"')
                    opts.append({"value": v, "label": lbl.strip().strip('"')})
                if opts:
                    p["options"] = opts
                    p["control"] = "select"
            hint = hint[: rng.start()].strip()
        if hint and not p["label"]:
            p["label"] = hint
        p.setdefault("control", {"bool": "checkbox", "string": "text", "vector": "vector"}.get(kind, "number"))
        params.append(p)
        prev_comment = ""
    return params


def _define_args(params: dict[str, Any] | None) -> list[str]:
    args: list[str] = []
    for k, v in (params or {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            raise RenderError(f"Invalid parameter name: {k}")
        if isinstance(v, bool):
            val = "true" if v else "false"
        elif isinstance(v, (int, float)):
            val = repr(v)
        elif isinstance(v, list):
            val = "[" + ",".join(repr(float(x)) for x in v) + "]"
        else:
            sval = str(v)
            if re.fullmatch(r"-?\d+(\.\d+)?", sval):
                val = sval
            else:
                val = json.dumps(sval)
        args += ["-D", f"{k}={val}"]
    return args


# ---- rendering ----------------------------------------------------------------
def _parse_log(log: str) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    for line in log.splitlines():
        if line.startswith("ERROR") or "Parser error" in line:
            errors.append(line.strip())
        elif line.startswith("WARNING") or "top level object is empty" in line:
            warnings.append(line.strip())
    return errors, warnings


def _backend_args() -> list[str]:
    choice = store.get()["render"]["backend"]
    flag = detect().get("manifold")
    if choice in ("auto", "manifold") and flag:
        return [flag]
    return []


async def render(
    code: str,
    fmt: str = "stl",
    params: dict[str, Any] | None = None,
    view: str = "iso",
    size: int | None = None,
) -> RenderResult:
    if not detect()["available"]:
        raise RenderError("OpenSCAD is not installed in this container.")
    s = store.get()
    fmt = fmt.lower()
    if fmt not in ("stl", "3mf", "png", "off"):
        raise RenderError(f"Unsupported format: {fmt}")
    size = size or s["agent"]["preview_size"]
    extra: list[str] = []
    if fmt == "png":
        extra = [
            "--imgsize",
            f"{size},{size}",
            "--autocenter",
            "--viewall",
            "--projection=p",
            f"--colorscheme={s['render']['colorscheme']}",
            f"--camera={VIEWS.get(view, VIEWS['iso'])}",
        ]
    elif fmt == "stl" and detect().get("binstl"):
        extra = ["--export-format=binstl"]
    defines = _define_args(params)
    backend = _backend_args()
    key_src = json.dumps([code, fmt, defines, extra, backend, [str(d) for d in library_dirs()]])
    key = hashlib.sha256(key_src.encode()).hexdigest()[:24]
    out = config.CACHE_DIR / f"{key}.{fmt}"
    if out.exists() and out.stat().st_size > 0:
        return RenderResult(path=out, log="", cached=True)

    async with _semaphore():
        with tempfile.TemporaryDirectory(dir=config.CACHE_DIR) as td:
            src = Path(td) / "model.scad"
            src.write_text(code)
            tmp_out = Path(td) / f"out.{fmt}"
            cmd = ["openscad", *backend, "-o", str(tmp_out), *extra, *defines, str(src)]
            if fmt == "png" and detect().get("xvfb"):
                cmd = ["xvfb-run", "-a", "-s", "-screen 0 1024x768x24", *cmd]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_env(), cwd=td
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=s["render"]["timeout"])
            except asyncio.TimeoutError:
                proc.kill()
                raise RenderError(f"Render timed out after {s['render']['timeout']}s. Simplify the model or raise the timeout in Settings.")
            log = stdout.decode(errors="replace").replace(str(src), "model.scad")
            errors, warnings = _parse_log(log)
            if proc.returncode != 0 or not tmp_out.exists() or tmp_out.stat().st_size == 0:
                msg = errors[0] if errors else "OpenSCAD produced no output (is the top level object empty?)"
                raise RenderError(msg, log[-6000:])
            shutil.move(str(tmp_out), out)
    return RenderResult(path=out, log=log[-6000:], warnings=warnings)


async def preview_sheet(code: str, params: dict[str, Any] | None = None, views: list[str] | None = None) -> tuple[bytes, str]:
    """Render several views and tile them into one PNG (fewer images means fewer tokens)."""
    from PIL import Image, ImageDraw

    views = views or ["iso", "front", "right", "top"]
    size = store.get()["agent"]["preview_size"]
    tile = max(256, size // 2) if len(views) > 1 else size
    results = await asyncio.gather(*(render(code, "png", params, v, tile) for v in views))
    if len(views) == 1:
        return results[0].path.read_bytes(), results[0].log
    cols = 2
    rows = (len(views) + 1) // 2
    sheet = Image.new("RGB", (cols * tile, rows * tile), "white")
    draw = ImageDraw.Draw(sheet)
    for i, (v, r) in enumerate(zip(views, results)):
        img = Image.open(r.path).convert("RGB").resize((tile, tile))
        x, y = (i % cols) * tile, (i // cols) * tile
        sheet.paste(img, (x, y))
        draw.rectangle([x, y, x + 70, y + 18], fill="white")
        draw.text((x + 4, y + 3), v.upper(), fill="black")
    buf = io.BytesIO()
    sheet.save(buf, "PNG", optimize=True)
    return buf.getvalue(), results[0].log


# ---- mesh validation ------------------------------------------------------------
def mesh_stats(stl_path: Path) -> dict[str, Any]:
    import trimesh

    s = store.get()["printer"]
    mesh = trimesh.load(str(stl_path), force="mesh")
    ext = [float(x) for x in mesh.extents]
    lo, hi = mesh.bounds
    try:
        bodies = len(mesh.split(only_watertight=False)) if len(mesh.faces) < 400000 else None
    except Exception:
        bodies = None
    stats: dict[str, Any] = {
        "triangles": int(len(mesh.faces)),
        "watertight": bool(mesh.is_watertight),
        "volume_mm3": round(float(mesh.volume), 2) if mesh.is_watertight else None,
        "size_mm": [round(e, 2) for e in ext],
        "min_mm": [round(float(x), 2) for x in lo],
        "max_mm": [round(float(x), 2) for x in hi],
        "bodies": bodies,
    }
    issues: list[str] = []
    if not mesh.is_watertight:
        issues.append("Mesh is not watertight (non-manifold); slicers may misbehave.")
    if s.get("shape") == "round":
        import numpy as np

        reach = float(np.max(np.hypot(mesh.vertices[:, 0], mesh.vertices[:, 1]))) if len(mesh.vertices) else 0.0
        if reach > s["bed_x"] / 2 or ext[2] > s["bed_z"]:
            issues.append(
                f"Part reaches {reach:.1f} mm from the centre and is {ext[2]:.1f} mm tall; the round bed allows "
                f"{s['bed_x'] / 2:.0f} mm radius and {s['bed_z']:.0f} mm height. Centre the part on the origin or make it smaller."
            )
    elif ext[0] > s["bed_x"] or ext[1] > s["bed_y"] or ext[2] > s["bed_z"]:
        issues.append(
            f"Part ({ext[0]:.1f} x {ext[1]:.1f} x {ext[2]:.1f} mm) exceeds the build volume "
            f"({s['bed_x']:.0f} x {s['bed_y']:.0f} x {s['bed_z']:.0f} mm)."
        )
    if lo[2] < -0.01:
        issues.append(f"Geometry extends below the bed (min Z = {lo[2]:.2f} mm). Translate it up to Z=0.")
    elif lo[2] > 0.01:
        issues.append(f"Part is floating (min Z = {lo[2]:.2f} mm). Its lowest face should sit at Z=0.")
    if bodies and bodies > 1:
        issues.append(f"Model contains {bodies} separate bodies. Make sure that is intended.")
    min_dim = min(ext) if ext else 0
    if 0 < min_dim < s["nozzle"] * 2:
        issues.append(f"Thinnest overall dimension ({min_dim:.2f} mm) is under two nozzle widths.")
    stats["issues"] = issues
    stats["printer"] = s["name"]
    if stats["volume_mm3"]:
        stats["est_filament_g_pla"] = round(stats["volume_mm3"] / 1000 * 1.24, 1)
    return stats


async def validate(code: str, params: dict[str, Any] | None = None) -> tuple[RenderResult, dict[str, Any]]:
    res = await render(code, "stl", params)
    stats = await asyncio.to_thread(mesh_stats, res.path)
    if res.warnings:
        stats["openscad_warnings"] = res.warnings[:10]
    return res, stats


def split_bodies(mesh):
    """Split a mesh into separate printable bodies.

    Connected components are split apart, but the inward-facing shell of an
    internal void stays with the body that encloses it, so a hollow part is
    still one object.
    """
    import trimesh

    parts = mesh.split(only_watertight=False)
    if len(parts) <= 1:
        return [mesh]
    solids, voids = [], []
    for p in parts:
        (voids if p.is_watertight and p.volume < 0 else solids).append(p)
    if not solids:
        return [mesh]
    groups = [[s] for s in solids]
    for v in voids:
        vmin, vmax = v.bounds
        home = None
        for g in groups:
            smin, smax = g[0].bounds
            if (smin <= vmin).all() and (smax >= vmax).all():
                if home is None or g[0].volume < home[0].volume:
                    home = g
        (home or groups[0]).append(v)
    bodies = [trimesh.util.concatenate(g) if len(g) > 1 else g[0] for g in groups]
    # Largest first, so the main part is "part-1" in the slicer.
    bodies.sort(key=lambda b: abs(b.volume), reverse=True)
    return bodies


async def export_3mf(code: str, params: dict[str, Any] | None = None) -> Path:
    """Export a 3MF with one object per separate body.

    OpenSCAD writes every top-level shape as a single object, so slicers such as
    Bambu Studio import a multi-part design as one object. Splitting the mesh
    here gives each part its own object, ready to arrange or color separately.
    """
    import trimesh

    stl = (await render(code, "stl", params)).path
    # Own cache key, so 3MF files cached by older versions (one object) are not reused.
    key = hashlib.sha256(f"{stl.stem}:split-bodies-v1".encode()).hexdigest()[:24]
    out = stl.with_name(f"{key}.3mf")
    if out.exists() and out.stat().st_size > 0:
        return out
    mesh = trimesh.load(str(stl), force="mesh")
    scene = trimesh.Scene()
    for i, body in enumerate(split_bodies(mesh), start=1):
        scene.add_geometry(body, node_name=f"part-{i}", geom_name=f"part-{i}")
    tmp = out.with_name(out.stem + ".tmp.3mf")
    scene.export(str(tmp), file_type="3mf")
    tmp.replace(out)
    return out
