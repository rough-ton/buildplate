"""The design agent: turns a natural-language request into a validated OpenSCAD model.

Loop: the model writes code, calls check_design (render + mesh validation + optional
multi-view preview image), fixes problems, then calls submit_design. Every model call's
token usage and cost is recorded against the run.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from typing import Any, AsyncIterator

from . import connections, db, openscad, parts, providers
from .auth_methods import MethodError
from .settings import store

PARTS_SCHEMA = {
    "type": "array",
    "description": "Every hardware part the design uses (screws, inserts, nuts, magnets, bearings...). Empty if none.",
    "items": {
        "type": "object",
        "properties": {
            "part": {"type": "string", "description": "The part's id (p_...) or catalog key from the inventory or list_parts, or a plain name if it is not listed."},
            "quantity": {"type": "integer", "minimum": 1},
            "use": {"type": "string", "description": "What it is for, e.g. 'wall mounting screws'."},
        },
        "required": ["part", "quantity"],
    },
}

LIST_PARTS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_parts",
        "description": (
            "Search the user's parts inventory and the hardware catalog (screws, heat-set inserts, nuts, washers, "
            "magnets, bearings, standoffs...). Returns each part's id or key, quantity on hand, and the hole, pocket "
            "and nut-trap sizes to model it with."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to match, e.g. 'M3 insert' or '608'."},
                "category": {"type": "string", "description": "Optional category, e.g. screw, heat-set insert, magnet, bearing."},
            },
        },
    },
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_design",
            "description": (
                "Render OpenSCAD code, validate the resulting mesh for 3D printing (size, watertightness, "
                "bed placement, build volume) and, when supported, return a preview image with iso, front, "
                "right and top views. Call this before submitting and after every fix."
            ),
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "Complete OpenSCAD source."}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_design",
            "description": "Save the final, checked design. Ends the task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Complete final OpenSCAD source."},
                    "title": {"type": "string", "description": "Short name for the part, 2 to 6 words."},
                    "summary": {
                        "type": "string",
                        "description": "One to three sentences for the user: what was built and the key parameters.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2 to 5 short lowercase library tags, e.g. what the part is for and its type: bracket, garage, cable management.",
                    },
                    "parts": PARTS_SCHEMA,
                    "hardware_note": {
                        "type": "string",
                        "description": "Only when hardware was asked to be avoided but is unavoidable: one sentence on why.",
                    },
                },
                "required": ["code", "title", "summary"],
            },
        },
    },
]


def system_prompt(vision: bool, hardware_mode: str | None = None) -> str:
    s = store.get()
    p = s["printer"]
    libs = ", ".join(lib["name"] for lib in openscad.list_libraries()) or "none"
    eng = openscad.detect()
    lines = [
        "You are a senior 3D CAD designer and a senior structural engineer who writes OpenSCAD for FDM 3D printing.",
        "Bring that judgment to every part, beyond whether it renders:",
        "- Structure: find the load paths and stress concentrations; size walls, ribs, gussets and fillets for the",
        "  real loads; avoid thin unsupported spans and sharp inside corners where the part will bend or crack.",
        "- FDM behaviour: layers are weakest in tension across them, so orient the part (or split it) so loads run",
        "  along the layers; mind bridging, elephant's foot, warping on long flat bases and shrinkage of holes.",
        "- Materials and fit: choose clearances, press fits, snap-fit strain and screw-boss sizes that work in",
        "  PLA/PETG, and model holes, threads and mating faces to function, not just to look right.",
        "- Printability and use: a flat, stable first layer, minimal supports, and a shape that is easy to print,",
        "  assemble and use.",
        "",
        "Rules for the code you write:",
        "- Units are millimetres. The part must rest on the bed: its lowest point at Z=0, centred near the origin in X/Y.",
        "- Make it parametric. Put user-tunable variables at the very top, before any module or function, using",
        "  OpenSCAD Customizer syntax: a comment line describing the parameter, then `name = value; // [min:step:max]`.",
        "  Group them with `/* [Section Name] */` headers. Put $fn and internal constants under `/* [Hidden] */`.",
        "- Use modules for logical sub-parts and keep the code readable, with brief comments.",
        (f"- Design for this printer: '{p['name']}', round bed {p['bed_x']:g} mm diameter, {p['bed_z']:g} mm tall build volume (delta),"
         if p.get("shape") == "round" else
         f"- Design for this printer: '{p['name']}', build volume {p['bed_x']:g} x {p['bed_y']:g} x {p['bed_z']:g} mm,"),
        f"  {p['nozzle']:g} mm nozzle, {p['layer_height']:g} mm layers. Use {p['clearance']:g} mm clearance for mating parts.",
        f"- Avoid overhangs steeper than {p['max_overhang']:g} degrees from vertical unless unavoidable; prefer chamfers",
        "  to fillets on downward-facing edges. Minimum wall thickness: at least 2 nozzle widths.",
        "- Produce a single watertight solid unless multiple parts are requested (then lay them out side by side).",
        f"- Available libraries: {libs}. Use `include <BOSL2/std.scad>` only when it clearly helps.",
        f"- OpenSCAD version {eng.get('version') or 'unknown'}.",
        "",
        "Workflow:",
        "1. Write the complete code and call check_design.",
        "2. Read the validation report" + (" and look carefully at the preview image" if vision else "") + ".",
        "3. Fix any errors, warnings or geometry that does not match the request, then check again.",
        "4. When it is correct, call submit_design with the final code, a short title, a brief summary and a few tags.",
        "Keep prose to a minimum; spend effort on getting the geometry right.",
    ]
    if hardware_mode:
        lines += ["", parts.mode_instructions(hardware_mode)]
    extra = s["agent"]["extra_instructions"].strip()
    if extra:
        lines += ["", "Additional instructions from the user:", extra]
    return "\n".join(lines)


def _history(design_id: str | None, current_code: str | None, prompt: str) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    if design_id:
        d = db.get_design(design_id)
        if d:
            turns = [m for m in d["messages"] if m["role"] in ("user", "assistant")][-12:]
            for m in turns:
                msgs.append({"role": m["role"], "content": m["content"]})
            if current_code is None:
                v = db.get_version(design_id)
                current_code = v["scad"] if v else None
    if current_code:
        content = (
            "Current design code:\n```openscad\n" + current_code.strip() + "\n```\n\n"
            f"Requested change: {prompt}\n\nModify the existing design rather than starting over unless asked."
        )
    else:
        content = f"Design request: {prompt}"
    msgs.append({"role": "user", "content": content})
    return msgs


def _report(stats: dict[str, Any], warnings: list[str]) -> str:
    lines = ["Render: OK"]
    lines.append(
        f"Size: {stats['size_mm'][0]} x {stats['size_mm'][1]} x {stats['size_mm'][2]} mm, "
        f"triangles: {stats['triangles']}, watertight: {stats['watertight']}, bodies: {stats.get('bodies')}"
    )
    lines.append(f"Bounds min: {stats['min_mm']} max: {stats['max_mm']}")
    if stats.get("volume_mm3"):
        lines.append(f"Volume: {stats['volume_mm3']} mm3 (about {stats.get('est_filament_g_pla')} g PLA)")
    issues = stats.get("issues") or []
    lines.append("Issues: " + ("; ".join(issues) if issues else "none"))
    if warnings:
        lines.append("OpenSCAD warnings: " + "; ".join(warnings[:8]))
    return "\n".join(lines)


def _prompt_text(design_id: str | None, current_code: str | None, prompt: str) -> str:
    """The conversation as one prompt, for runtimes that take a single message."""
    *earlier, current = _history(design_id, current_code, prompt)
    if not earlier:
        return current["content"]
    lines = [f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in earlier]
    return "Earlier conversation about this design:\n" + "\n\n".join(lines) + "\n\n" + current["content"]


async def run(
    prompt: str,
    design_id: str | None = None,
    current_code: str | None = None,
    connection: str | None = None,
    model: str | None = None,
    source: str = "studio",
    provider: str | None = None,
    hardware_mode: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run the design loop. connection and model default to the design agent's
    settings; provider (a provider family) is the pre-connections way to choose and
    picks the first enabled connection of that family. The connection's method picks
    the runtime: Buildplate's own LiteLLM loop, or Claude Code for a Claude plan."""
    try:
        target = connections.resolve(connection, model, provider)
    except MethodError as exc:
        yield {"type": "error", "message": str(exc)}
        return
    method, model = target.method, target.model
    provider = method.family

    a = store.get()["agent"]
    mode = hardware_mode if hardware_mode in parts.MODES else a.get("hardware_mode", "ask")
    if mode == "ask":
        # a follow-up keeps the choice made for this design; otherwise ask, before any
        # model call, when the request clearly needs hardware
        mode = db.latest_hardware_mode(design_id) or "ask"
        if mode == "ask" and parts.needs_hardware(prompt):
            yield hardware_question()
            return
    vision = providers.supports_vision(method, model)
    run_id = db.start_run(design_id, prompt, provider, model, source, target.id, method.id, method.billing)
    yield {
        "type": "run", "run_id": run_id, "provider": provider, "model": model, "vision": vision, "design_id": design_id,
        "connection": target.id, "connection_label": target.connection["label"], "billing": method.billing,
    }

    totals: dict[str, Any] = {k: 0 for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens")}
    totals["cost_usd"] = 0.0
    totals["cost_known"] = True
    totals["billing"] = method.billing
    state: dict[str, Any] = {"final": None, "last_good": None, "totals": totals}

    try:
        if method.runtime == "cli:claude":
            from . import claude_runtime

            loop = claude_runtime.design_loop(
                target.id, run_id, model, system_prompt(vision, mode), _prompt_text(design_id, current_code, prompt),
                vision, a["max_iterations"], state, mode,
            )
        elif method.runtime == "sdk:copilot":
            from . import copilot

            loop = copilot.design_loop(
                target.id, connections.values(target.connection)["token"], run_id, model, system_prompt(vision, mode),
                _prompt_text(design_id, current_code, prompt), vision, a["max_iterations"], state, mode,
            )
        else:
            loop = _litellm_loop(target, run_id, design_id, current_code, prompt, vision, state, mode)
        async for ev in loop:
            yield ev
        if method.billing == "subscription" and method.runtime != "cli:claude":
            # plan requests cost nothing extra; keep the API-price estimate on the run
            r = db.get_run(run_id)
            ests = [st.get("est_cost_usd") for st in r["steps"]]
            est = None if not ests or any(e is None for e in ests) else sum(ests)
            db.set_run_totals(run_id, {k: r[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")}, est, len(ests))
            totals["est_cost_usd"] = est

        final, last_good = state["final"], state["last_good"]
        if final is None and last_good:
            final = {"code": last_good["code"], "title": "", "summary": "Stopped before submitting; saved the last design that rendered."}
        if final is None:
            raise RuntimeError("The model did not produce a design that renders. Try rephrasing or a more capable model.")

        res, stats = await openscad.validate(final["code"])
        title = (final.get("title") or "").strip() or _title_from(prompt)
        if not design_id:
            design_id = db.create_design(title, source)
        elif final.get("title") and db.get_design(design_id) and db.get_design(design_id)["title"] == "Untitled":
            db.rename_design(design_id, title)
        version = db.add_version(design_id, final["code"], final.get("summary", ""), stats, run_id)
        bom = parts.normalize_bom(final.get("parts"))
        db.set_version_hardware(design_id, version, bom, mode, final.get("hardware_note") or "")
        tags = db.get_tags(design_id)
        suggested = final.get("tags")
        if not tags and isinstance(suggested, list):
            # only tag untagged designs, so the agent never overrides tags the user chose
            tags = db.set_tags(design_id, [str(t) for t in suggested[:5]])
        db.add_message(design_id, "user", prompt, run_id)
        db.add_message(design_id, "assistant", final.get("summary") or "Updated the design.", run_id)
        db.finish_run(run_id, "ok", design_id=design_id)
        yield {
            "type": "done",
            "run_id": run_id,
            "design_id": design_id,
            "version": version,
            "title": title,
            "code": final["code"],
            "summary": final.get("summary", ""),
            "tags": tags,
            "bom": parts.resolve_bom(bom),
            "hardware_mode": mode,
            "hardware_note": final.get("hardware_note") or "",
            "stats": stats,
            "params": openscad.parse_parameters(final["code"]),
            "totals": totals,
        }
    except asyncio.CancelledError:
        db.finish_run(run_id, "cancelled", design_id=design_id)
        raise
    except Exception as exc:
        db.finish_run(run_id, "error", error=str(exc)[:2000], design_id=design_id)
        conn_state = getattr(exc, "connection_state", None)
        if conn_state:
            connections.mark(target.id, conn_state, str(exc))
        message = str(exc) if method.runtime != "litellm" else _friendly(exc)
        yield {"type": "error", "message": message, "run_id": run_id, "totals": totals,
               "manage_url": getattr(exc, "manage_url", None)}


def hardware_question() -> dict[str, Any]:
    """The question Studio and MCP clients show when a request needs hardware and the
    user has not said how to source it."""
    stocked = sum(1 for p in parts.list_inventory() if p["quantity"] > 0)
    return {
        "type": "question",
        "kind": "hardware",
        "message": "This design looks like it needs hardware. How should I handle it?",
        "options": [
            {"value": "inventory", "label": parts.MODE_LABELS["inventory"],
             "detail": f"Design around the {stocked} part types in your inventory." if stocked else "Your parts inventory is empty.",
             "disabled": not stocked},
            {"value": "order", "label": parts.MODE_LABELS["order"],
             "detail": "Pick the best standard parts and give me a shopping list."},
            {"value": "none", "label": parts.MODE_LABELS["none"],
             "detail": "Use printed snap fits, threads or pins instead, where possible."},
        ],
    }


async def _litellm_loop(target, run_id: str, design_id, current_code, prompt: str, vision: bool, state: dict[str, Any],
                        mode: str = "ask") -> AsyncIterator[dict[str, Any]]:
    """Buildplate's own tool-calling loop through LiteLLM."""
    import litellm

    litellm.suppress_debug_info = True
    litellm.drop_params = True
    method, model, kwargs = target.method, target.model, target.kwargs
    complete = litellm.acompletion
    if method.runtime == "responses:chatgpt":
        from . import chatgpt

        # the ChatGPT plan goes through the Responses API, adapted to this loop's calls
        kwargs = {"model": model}
        complete = lambda **kw: chatgpt.acompletion(target.id, **kw)  # noqa: E731
    a = store.get()["agent"]
    totals = state["totals"]
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt(vision, mode)}]
    messages += _history(design_id, current_code, prompt)
    tool_mode = True
    tools = TOOLS + [LIST_PARTS_TOOL]

    cache = method.runtime == "litellm" and _wants_cache_control(method, model)
    pinned = len(messages)  # system prompt + earlier conversation: never compacted, so it stays cacheable

    for step in range(1, a["max_iterations"] + 1):
        yield {"type": "status", "message": f"Thinking (step {step} of {a['max_iterations']})"}
        t0 = time.time()
        _compact(messages, pinned)
        sent = _with_cache_control(messages, pinned) if cache else messages
        call = dict(kwargs, messages=sent, temperature=a["temperature"], max_tokens=a["max_output_tokens"])
        if tool_mode:
            call["tools"] = tools
            call["tool_choice"] = "auto"
        try:
            resp = await complete(**call)
        except Exception as exc:  # provider error
            msg = str(exc)
            if tool_mode and re.search(r"tool|function", msg, re.I) and step == 1:
                tool_mode = False
                messages[0]["content"] += TEXT_MODE_SUFFIX
                yield {"type": "status", "message": "Model has no tool support; switching to text mode"}
                continue
            raise
        usage = providers.usage_from_response(resp, method, model)
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens", "cost_usd"):
            totals[k] += usage[k]
        totals["cost_known"] = totals["cost_known"] and usage["cost_known"]
        msg = resp.choices[0].message
        calls = list(getattr(msg, "tool_calls", None) or [])
        db.add_step(run_id, step, usage, int((time.time() - t0) * 1000), [c.function.name for c in calls])
        yield {"type": "usage", "step": step, "usage": usage, "totals": dict(totals)}

        text = (msg.content or "").strip() if isinstance(msg.content, str) else ""
        if text:
            yield {"type": "assistant", "text": text}

        if not tool_mode:
            final, state["last_good"], feedback, events = await _text_mode_step(text, state["last_good"], vision)
            for e in events:
                yield e
            messages.append({"role": "assistant", "content": text or "(no response)"})
            if final:
                state["final"] = final
                return
            messages.append(feedback)
            continue

        if not calls:
            if state["last_good"]:
                state["final"] = {"code": state["last_good"]["code"], "title": "", "summary": text[:600]}
                return
            messages.append({"role": "assistant", "content": text or "(empty)"})
            messages.append({"role": "user", "content": "Please write the OpenSCAD code and call check_design."})
            continue

        messages.append(
            {
                "role": "assistant",
                "content": text or None,
                "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                    for c in calls
                ],
            }
        )
        images: list[str] = []
        for c in calls:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            code = args.get("code", "")
            name = c.function.name
            yield {"type": "tool", "name": name}
            if name == "list_parts":
                found = parts.search_for_agent(str(args.get("query") or ""), str(args.get("category") or ""))
                messages.append({"role": "tool", "tool_call_id": c.id, "content": found})
                continue
            if not code:
                messages.append({"role": "tool", "tool_call_id": c.id, "content": "Error: 'code' is required."})
                continue
            yield {"type": "code", "code": code}
            result_text, check, img = await _check(code, vision and name == "check_design")
            for e in check["events"]:
                yield e
            if check["ok"]:
                state["last_good"] = {"code": code, "stats": check["stats"]}
            problem = parts.check_submission(mode, args.get("parts") or [], str(args.get("hardware_note") or "")) if name == "submit_design" else None
            if name == "submit_design" and check["ok"] and not problem:
                state["final"] = {"code": code, "title": args.get("title", ""), "summary": args.get("summary", ""), "tags": args.get("tags") or [],
                                  "parts": args.get("parts") or [], "hardware_note": str(args.get("hardware_note") or "")}
                return
            if problem:
                messages.append({"role": "tool", "tool_call_id": c.id, "content": problem})
                continue
            if name == "submit_design":
                result_text = "Not saved, fix these problems first.\n" + result_text
            messages.append({"role": "tool", "tool_call_id": c.id, "content": result_text})
            if img:
                images.append(img)
        if images:
            content: list[dict[str, Any]] = [{"type": "text", "text": "Preview render (ISO, FRONT, RIGHT, TOP):"}]
            content += [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images[-1:]]
            messages.append({"role": "user", "content": content})
    if state["final"] is None and state["last_good"]:
        state["final"] = {"code": state["last_good"]["code"], "title": "", "summary": "Stopped at the step limit; saved the last design that rendered."}


CODE_TOOLS = ("check_design", "submit_design")
OMITTED_CODE = "// earlier revision omitted: superseded by the latest code below"
OMITTED_IMAGE = "(earlier preview image omitted: superseded by the latest preview)"
_FENCE = re.compile(r"```(?:openscad|scad)?\s*\n.*?```", re.S)


def _gist(report: str) -> str:
    """A superseded check report, cut to the lines worth remembering."""
    if report.startswith("(superseded"):
        return report
    keep = [ln for ln in report.splitlines() if ln.startswith(("Render", "Issues:", "Not saved"))][:3]
    return "(superseded by a later check) " + " | ".join(ln[:240] for ln in keep)


def _compact(messages: list[dict[str, Any]], start: int) -> None:
    """Within one run the model only needs the current code, report and preview to
    decide its next fix. Shrink every earlier revision in place: code in earlier tool
    calls and text-mode replies, their check reports and their preview images. Messages
    before start (system prompt, earlier conversation) are left alone. Running it again
    is a no-op."""
    latest = None
    for i in range(len(messages) - 1, start - 1, -1):
        m = messages[i]
        if m["role"] == "assistant" and (
            any(tc["function"]["name"] in CODE_TOOLS for tc in m.get("tool_calls") or [])
            or (isinstance(m.get("content"), str) and _FENCE.search(m["content"]))
        ):
            latest = i
            break
    if latest is None:
        return

    code_calls: set[str] = set()
    for m in messages[start:latest]:
        if m["role"] == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc["function"]
                if fn["name"] not in CODE_TOOLS:
                    continue
                code_calls.add(tc["id"])
                try:
                    args = json.loads(fn["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                if args.get("code") != OMITTED_CODE:
                    fn["arguments"] = json.dumps({**args, "code": OMITTED_CODE})
            if isinstance(m.get("content"), str):
                m["content"] = _FENCE.sub("```openscad\n" + OMITTED_CODE + "\n```", m["content"])
        elif m["role"] == "tool" and m.get("tool_call_id") in code_calls and isinstance(m.get("content"), str):
            m["content"] = _gist(m["content"])
        elif m["role"] == "user" and isinstance(m.get("content"), list):
            texts = [c["text"] for c in m["content"] if c.get("type") == "text"]
            if any(c.get("type") == "image_url" for c in m["content"]):
                body = "\n".join(t for t in texts if not t.startswith("Preview render"))
                m["content"] = (_gist(body) + "\n" if body.startswith("Render") else body + "\n" if body else "") + OMITTED_IMAGE
        elif m["role"] == "user" and isinstance(m.get("content"), str) and m["content"].startswith("Render"):
            m["content"] = _gist(m["content"])  # a text-mode check report


_CACHE_FAMILIES = ("bedrock", "vertex_ai", "openrouter", "azure_ai")


def _wants_cache_control(method, model: str) -> bool:
    """Anthropic models only cache a prompt at explicit cache_control breakpoints;
    OpenAI, Gemini and most others cache automatically and need nothing."""
    return method.family == "anthropic" or (method.family in _CACHE_FAMILIES and "claude" in model.lower())


def _with_cache_control(messages: list[dict[str, Any]], pinned: int) -> list[dict[str, Any]]:
    """A copy of messages with prompt-cache breakpoints on the system prompt, the end
    of the earlier conversation (which carries the current design code) and the end of
    the compacted part of this run. None of those change for the rest of the run, so
    each later step reads them from the cache instead of paying for them again."""
    marks = {0, pinned - 1}
    i = len(messages) - 1
    while i >= pinned and messages[i]["role"] != "assistant":
        i -= 1  # the newest assistant turn and its results change on the next step
    if i - 1 >= pinned:
        marks.add(i - 1)
    out = list(messages)
    for i in sorted(marks):
        m = dict(messages[i])
        if m["role"] == "tool":
            m["cache_control"] = {"type": "ephemeral"}
        elif isinstance(m.get("content"), str) and m["content"]:
            m["content"] = [{"type": "text", "text": m["content"], "cache_control": {"type": "ephemeral"}}]
        elif isinstance(m.get("content"), list) and m["content"]:
            m["content"] = m["content"][:-1] + [dict(m["content"][-1], cache_control={"type": "ephemeral"})]
        else:
            continue  # an assistant turn that only calls tools
        out[i] = m
    return out


async def _check(code: str, want_image: bool) -> tuple[str, dict[str, Any], str | None]:
    events: list[dict[str, Any]] = []
    try:
        res, stats = await openscad.validate(code)
    except openscad.RenderError as exc:
        events.append({"type": "check", "ok": False, "error": str(exc)})
        log = exc.log.strip().splitlines()[-25:]
        return "Render FAILED: " + str(exc) + ("\nLog:\n" + "\n".join(log) if log else ""), {"ok": False, "events": events}, None
    events.append({"type": "check", "ok": True, "stats": stats})
    img_b64 = None
    try:
        png, _ = await openscad.preview_sheet(code)
        img_b64 = base64.b64encode(png).decode()
        events.append({"type": "preview", "image": img_b64})
    except openscad.RenderError:
        pass
    return _report(stats, stats.get("openscad_warnings", [])), {"ok": True, "stats": stats, "events": events}, (img_b64 if want_image else None)


TEXT_MODE_SUFFIX = """

This session has no tool calling. Instead, reply with the complete OpenSCAD code in a single
```openscad fenced block. You will receive a validation report. When the report shows no issues
and the design is right, reply with the word DONE on its own line, plus a one-sentence summary."""


async def _text_mode_step(text: str, last_good: dict[str, Any] | None, vision: bool):
    events: list[dict[str, Any]] = []
    blocks = re.findall(r"```(?:openscad|scad)?\s*\n(.*?)```", text, re.S)
    if not blocks:
        if last_good and re.search(r"^\s*DONE\b", text, re.M):
            summary = re.sub(r"^\s*DONE\s*", "", text, flags=re.M).strip()
            return {"code": last_good["code"], "title": "", "summary": summary[:600]}, last_good, None, events
        return None, last_good, {"role": "user", "content": "Reply with the full code in an ```openscad block."}, events
    code = blocks[-1].strip()
    events.append({"type": "code", "code": code})
    result_text, check, img = await _check(code, vision)
    events += check["events"]
    if check["ok"]:
        last_good = {"code": code, "stats": check["stats"]}
    feedback_text = result_text + "\n\nFix any problems and resend the full code, or reply DONE if it is correct."
    if img:
        feedback: dict[str, Any] = {
            "role": "user",
            "content": [
                {"type": "text", "text": feedback_text},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}},
            ],
        }
    else:
        feedback = {"role": "user", "content": feedback_text}
    return None, last_good, feedback, events


def _title_from(prompt: str) -> str:
    words = re.sub(r"[^\w\s-]", "", prompt).split()
    return (" ".join(words[:6]) or "Untitled").capitalize()


def _friendly(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if "authentication" in low or "401" in low or "invalid x-api-key" in low or "api key" in low:
        return "The provider rejected the API key. Update the connection in Settings > AI connections."
    if "rate limit" in low or "429" in low:
        return "The provider rate-limited this request. Wait a moment and try again."
    if "connection" in low and ("refused" in low or "error" in low):
        return "Could not connect to the model endpoint. Check the base URL in Settings."
    return msg[:800]
