"""MCP (Model Context Protocol) endpoint so Claude Desktop, Claude Code and other MCP
clients can use Buildplate's renderer, validator, library and design agent."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from . import agent, config, db, openscad, printers, search
from .settings import store

mcp = MCPServer(
    name="buildplate",
    title="Buildplate",
    version=config.VERSION,
    instructions=(
        "Buildplate renders and validates OpenSCAD models for 3D printing. Typical loop: write parametric "
        "OpenSCAD, call check_design to validate and see a preview, fix issues, then call save_design to store "
        "it and get STL/3MF download links. generate_design runs Buildplate's own design agent end to end "
        "using the model configured in its settings. Units are millimetres; parts should sit on Z=0."
    ),
)


def _url(path: str) -> str:
    base = store.get()["server"]["public_url"].rstrip("/")
    return f"{base}{path}" if base else path


def _generated_by(v: dict[str, Any]) -> dict[str, Any]:
    """Who produced a version: origin is agent, manual or mcp, plus provider, model and
    (for MCP saves) the self-reported client."""
    return {"origin": v.get("origin"), "provider": v.get("provider"), "model": v.get("model"), "client": v.get("client")}


def _links(design_id: str, version: int) -> dict[str, str]:
    return {
        fmt: _url(f"/api/designs/{design_id}/download?format={fmt}&version={version}")
        for fmt in ("stl", "3mf", "scad")
    } | {"studio": _url(f"/#/studio/{design_id}")}


@mcp.tool(
    description="Render OpenSCAD code, validate the mesh for printing, and return a report plus a 4-view preview image.",
    structured_output=False,
)
async def check_design(code: str, params: dict[str, Any] | None = None) -> list[Any]:
    try:
        res, stats = await openscad.validate(code, params)
    except openscad.RenderError as exc:
        return [f"Render FAILED: {exc}\n{exc.log[-3000:]}"]
    report = agent._report(stats, stats.get("openscad_warnings", []))
    try:
        png, _ = await openscad.preview_sheet(code, params)
        return [report, Image(data=png, format="png")]
    except openscad.RenderError:
        return [report]


@mcp.tool(description="Render a preview PNG. views: any of iso, front, right, top, back_iso.")
async def preview(code: str, views: list[str] | None = None) -> Image:
    png, _ = await openscad.preview_sheet(code, None, views or ["iso"])
    return Image(data=png, format="png")


@mcp.tool(
    description=(
        "Validate and save OpenSCAD code to the Buildplate library. Pass design_id to add a new version to an "
        "existing design, and optionally tags to label it. Identify yourself so the library can show who made "
        "the design: client is the app (e.g. Claude Desktop, Claude Code), model is the model that wrote the "
        "code (e.g. claude-sonnet-5-5) and provider its vendor (e.g. anthropic). Returns download links."
    )
)
async def save_design(
    code: str,
    title: str,
    summary: str = "",
    design_id: str | None = None,
    tags: list[str] | None = None,
    client: str = "",
    model: str = "",
    provider: str = "",
) -> dict[str, Any]:
    res, stats = await openscad.validate(code)
    if not design_id or not db.get_design(design_id):
        design_id = db.create_design(title, "mcp")
    version = db.add_version(design_id, code, summary, stats, None, "mcp", provider, model, client)
    out_tags = db.add_tags(design_id, tags) if tags else db.get_tags(design_id)
    return {"design_id": design_id, "version": version, "tags": out_tags, "stats": stats, "downloads": _links(design_id, version)}


@mcp.tool(
    description=(
        "Run Buildplate's built-in design agent on a natural-language request. Uses the design agent's default "
        "AI connection and model unless connection (an id from environment) and model are given. Token usage "
        "is tracked in Buildplate. Pass design_id to modify an existing design."
    )
)
async def generate_design(
    prompt: str, design_id: str | None = None, connection: str | None = None, model: str | None = None
) -> dict[str, Any]:
    done: dict[str, Any] | None = None
    async for ev in agent.run(prompt, design_id=design_id, source="mcp", connection=connection, model=model):
        if ev["type"] == "done":
            done = ev
        elif ev["type"] == "error":
            return {"error": ev["message"]}
    assert done is not None
    return {
        "design_id": done["design_id"],
        "version": done["version"],
        "title": done["title"],
        "summary": done["summary"],
        "tags": done["tags"],
        "stats": done["stats"],
        "tokens": done["totals"],
        "code": done["code"],
        "downloads": _links(done["design_id"], done["version"]),
    }


@mcp.tool(
    description=(
        "Search saved designs. query is free text matched against title, summary, tags, the prompt, "
        "provider and model, and may include qualifiers: tag:garage provider:ollama model:qwen source:mcp "
        "printer:x1c size:<100 w:<50 d:>20 h:20..40 after:2026-10-01 before:2026-11. The other arguments "
        "are the same filters: tags (comma-separated, all required), size in mm of the largest dimension, "
        "dates as YYYY-MM-DD. sort: relevance (default with free text), updated (default otherwise), "
        "created, title, largest or smallest."
    )
)
async def list_designs(
    query: str = "",
    tags: str = "",
    provider: str = "",
    model: str = "",
    source: str = "",
    printer: str = "",
    min_size: float | None = None,
    max_size: float | None = None,
    after: str = "",
    before: str = "",
    sort: str = "",
    limit: int = 25,
) -> list[dict[str, Any]] | dict[str, str]:
    try:
        f = search.filters(tags, provider, model, source, printer, min_size, max_size, after, before)
        rows = db.list_designs(limit, query, f, sort)
    except ValueError as exc:
        return {"error": str(exc)}
    return [
        {
            "design_id": d["id"],
            "title": d["title"],
            "version": d["current_version"],
            "summary": d["summary"],
            "tags": d["tags"],
            "source": d["source"],
            "generated_by": _generated_by(d),
            "models": d["models"],
            "size_mm": d["stats"].get("size_mm"),
            "printer": d["stats"].get("printer"),
            "created_at": d["created_at"],
            "updated_at": d["updated_at"],
        }
        for d in rows
    ]


@mcp.tool(description="Replace the tags on a saved design. Tags are lowercased; pass an empty list to clear them.")
async def set_design_tags(design_id: str, tags: list[str]) -> dict[str, Any]:
    try:
        return {"design_id": design_id, "tags": db.set_tags(design_id, tags)}
    except KeyError:
        return {"error": "Design not found"}
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool(description="Get the OpenSCAD source of a saved design (latest version unless version is given).")
async def get_design(design_id: str, version: int | None = None) -> dict[str, Any]:
    v = db.get_version(design_id, version)
    if not v:
        return {"error": "Design not found"}
    return {
        "design_id": design_id,
        "version": v["version"],
        "code": v["scad"],
        "tags": db.get_tags(design_id),
        "generated_by": _generated_by(v),
        "stats": v["stats"],
        "downloads": _links(design_id, v["version"]),
    }


@mcp.tool(description="Extract Customizer parameters (name, value, range, options) from OpenSCAD code.")
async def get_parameters(code: str) -> list[dict[str, Any]]:
    return openscad.parse_parameters(code)


@mcp.tool(description="Describe the active printer profile, all saved printer profiles, the AI connections and models generate_design can use, and available OpenSCAD libraries.")
async def environment() -> dict[str, Any]:
    s = store.get()
    return {
        "ai_connections": [
            {"id": c["id"], "label": c["label"], "method": c["method"], "enabled": c["enabled"], "models": c["models"][:50]}
            for c in s["connections"]
        ],
        "default_connection": s["agent"]["connection"],
        "default_model": s["agent"]["model"],
        "active_printer": s["printer"],
        "printers": [{"id": p["id"], "name": p["name"]} for p in s["printers"]],
        "libraries": openscad.list_libraries(),
        "openscad": openscad.detect(),
    }


@mcp.tool(description="Switch the active printer profile by id (see environment). Checks and new designs then use its build volume and tolerances.")
async def set_printer(printer_id: str) -> dict[str, Any]:
    if printer_id not in [p["id"] for p in store.get()["printers"]]:
        return {"error": "No printer profile with that id"}
    store.update({"active_printer": printer_id})
    return {"active_printer": store.get()["printer"]}


@mcp.tool(description="Search the printer catalog (hundreds of models from all major manufacturers) by name. Returns keys and nozzle sizes for add_printer.")
async def find_printers(query: str, limit: int = 15) -> list[dict[str, Any]]:
    q = query.lower()
    hits = [p for p in printers.catalog()["printers"] if q in f"{p['vendor']} {p['model']}".lower()]
    return [
        {"key": p["key"], "name": p["display"], "bed_mm": [p["bed_x"], p["bed_y"], p["bed_z"]], "shape": p["shape"], "type": p["type"], "nozzles": p["nozzles"]}
        for p in hits[:limit]
    ]


@mcp.tool(description="Add a printer profile from the catalog (see find_printers) and optionally make it the active printer.")
async def add_printer(key: str, nozzle: float = 0.4, activate: bool = True) -> dict[str, Any]:
    try:
        prof = printers.profile_from_catalog(key, nozzle)
    except KeyError:
        return {"error": "Unknown printer key. Use find_printers first."}
    s = store.get()
    profiles = list(s["printers"])
    if prof["id"] not in {p["id"] for p in profiles}:
        profiles.append(prof)
    patch: dict[str, Any] = {"printers": profiles}
    if activate:
        patch["active_printer"] = prof["id"]
    store.update(patch)
    return {"added": prof, "active_printer": store.get()["printer"]["name"]}
