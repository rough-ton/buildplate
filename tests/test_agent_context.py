"""Within-run context: earlier revisions are compacted, cache breakpoints are set for
Anthropic models, and the persona is in the system prompt."""

import asyncio
import base64
import copy
import importlib
import json
from types import SimpleNamespace

import pytest

PNG = base64.b64encode(b"\x89PNG" + b"\0" * 6000).decode()  # stands in for a ~6 KB preview sheet


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDPLATE_DATA", str(tmp_path))
    from app import config

    importlib.reload(config)
    from app import settings

    importlib.reload(settings)
    from app import auth_methods, connections, db, openscad, parts, providers

    for m in (auth_methods, connections, providers, db, parts):
        importlib.reload(m)
    db.init()
    from app import agent

    importlib.reload(agent)

    async def validate(code, params=None):
        return None, {"size_mm": [30.0, 20.0, 10.0], "min_mm": [0, 0, 0], "max_mm": [30, 20, 10], "triangles": 12,
                      "watertight": True, "bodies": 1, "issues": ["thin wall"], "printer": "Test"}

    async def preview(code, params=None, views=None):
        return base64.b64decode(PNG), None

    monkeypatch.setattr(openscad, "validate", validate)
    monkeypatch.setattr(openscad, "preview_sheet", preview)
    settings.store.update({"agent": {"vision": "on", "hardware_mode": "none", "max_iterations": 8}})
    return SimpleNamespace(store=settings.store, agent=agent, connections=connections)


def _code(rev: int) -> str:
    # a realistic-size design: about 3 KB of OpenSCAD per revision
    body = "\n".join(f"module part_{i}() {{ translate([{i}, 0, 0]) cube([10, 20, {rev + 2}]); }} // rev {rev}" for i in range(40))
    return f"/* [Size] */\nwidth = {20 + rev}; // [10:1:80]\n{body}\npart_0();\n"


def _scripted(monkeypatch, cycles: int):
    """A model that checks `cycles` revisions, then submits. Each call's messages are
    snapshotted, since the loop edits its list in place."""
    import litellm

    script = [[("check_design", {"code": _code(n)})] for n in range(cycles)]
    script.append([("submit_design", {"code": _code(cycles), "title": "Bracket", "summary": "Done."})])
    calls = iter(enumerate(script))
    seen: list[list[dict]] = []

    async def acompletion(**kw):
        seen.append(copy.deepcopy(kw["messages"]))
        step, todo = next(calls)
        tcs = [SimpleNamespace(id=f"call_{step}_{n}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
               for n, (name, args) in enumerate(todo)]
        msg = SimpleNamespace(content="", tool_calls=tcs)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    return seen


def _run(env, prompt, **kw):
    events = []

    async def go():
        async for ev in env.agent.run(prompt, **kw):
            events.append(ev)

    asyncio.run(go())
    return events


def _connection(env, method="ollama.local", fields=None, model="qwen3"):
    c = env.connections.create(method, "Test", fields or {"base_url": "http://box:11434"})
    env.store.update({"agent": {"connection": c["id"], "model": model}})
    return c


def _images(msgs):
    return sum(1 for m in msgs if isinstance(m.get("content"), list) for c in m["content"] if c.get("type") == "image_url")


def _input_tokens(msgs) -> int:
    """Input size the way a vision model is billed: text at ~4 chars a token, and a
    flat ~1,000 tokens per 768 px preview image."""
    text = 0
    for m in msgs:
        c = m.get("content")
        if isinstance(c, list):
            text += sum(len(b.get("text", "")) for b in c)
        else:
            text += len(c or "")
        text += sum(len(tc["function"]["arguments"]) for tc in m.get("tool_calls") or [])
    return text // 4 + 1000 * _images(msgs)


def test_earlier_revisions_are_compacted(env, monkeypatch):
    _connection(env)
    seen = _scripted(monkeypatch, cycles=5)
    events = _run(env, "a wall bracket")
    assert events[-1]["type"] == "done" and events[-1]["code"] == _code(5)

    last = seen[-1]
    assert _images(last) == 1  # only the newest preview is still sent
    codes = [json.loads(tc["function"]["arguments"])["code"] for m in last for tc in m.get("tool_calls") or []]
    assert codes[-1] == _code(4)  # the latest revision is intact...
    assert set(codes[:-1]) == {env.agent.OMITTED_CODE}  # ...earlier ones are not resent
    reports = [m["content"] for m in last if m["role"] == "tool"]
    assert reports[-1].startswith("Render: OK") and "Size:" in reports[-1]
    assert all(r.startswith("(superseded") and "Issues: thin wall" in r for r in reports[:-1])
    # the request and the system prompt are never touched
    assert last[1]["content"] == "Design request: a wall bracket"
    for msgs in seen:  # every step still sees the current code, report and image
        if len(msgs) > 2:
            assert _images(msgs) == 1


def test_compaction_cuts_input_tokens(env, monkeypatch):
    """Before/after on a 5 check-and-fix cycle run: same script, compaction off then on."""
    _connection(env)
    real = env.agent._compact
    monkeypatch.setattr(env.agent, "_compact", lambda *a: None)
    seen = _scripted(monkeypatch, cycles=5)
    _run(env, "a wall bracket")
    before = sum(_input_tokens(m) for m in seen)
    monkeypatch.setattr(env.agent, "_compact", real)
    seen = _scripted(monkeypatch, cycles=5)
    _run(env, "a wall bracket")
    after = sum(_input_tokens(m) for m in seen)
    print(f"input tokens over the run: {before} without compaction, {after} with ({1 - after / before:.0%} less)")
    assert after < before * 0.6


def test_compaction_in_text_mode():
    from app import agent

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Design request: a cube"},
        {"role": "assistant", "content": "Here:\n```openscad\ncube(10);\n```"},
        {"role": "user", "content": [{"type": "text", "text": "Render: OK\nIssues: none\n\nFix any problems"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
        {"role": "assistant", "content": "Better:\n```openscad\ncube(12);\n```"},
        {"role": "user", "content": [{"type": "text", "text": "Render: OK\nIssues: none"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}}]},
    ]
    agent._compact(msgs, 2)
    assert "cube(10)" not in msgs[2]["content"] and agent.OMITTED_CODE in msgs[2]["content"]
    assert msgs[3]["content"].startswith("(superseded") and msgs[3]["content"].endswith(agent.OMITTED_IMAGE)
    assert "cube(12)" in msgs[4]["content"] and isinstance(msgs[5]["content"], list)
    once = copy.deepcopy(msgs)
    agent._compact(msgs, 2)
    assert msgs == once  # idempotent


def test_cache_breakpoints_for_anthropic(env, monkeypatch):
    _connection(env, "anthropic.api_key", {"api_key": "sk-ant-test-0123456789"}, "claude-sonnet-4-5")
    seen = _scripted(monkeypatch, cycles=3)
    _run(env, "a wall bracket")

    def marked(msgs):
        out = []
        for i, m in enumerate(msgs):
            blocks = m["content"] if isinstance(m.get("content"), list) else []
            if m.get("cache_control") or any(b.get("cache_control") for b in blocks):
                out.append(i)
        return out

    assert marked(seen[0]) == [0, 1]  # system prompt and the request
    assert marked(seen[1]) == [0, 1]  # nothing compacted yet
    assert marked(seen[3])[:2] == [0, 1] and len(marked(seen[3])) == 3  # plus the end of the compacted part
    assert len(marked(seen[3])) <= 4  # Anthropic allows at most four
    # what the step before cached up to its compacted-part breakpoint is unchanged at the next step,
    # so it is a cache hit there (the marker itself is not part of the cached content)
    def plain(msgs):
        out = []
        for m in msgs:
            m = {k: v for k, v in m.items() if k != "cache_control"}
            if isinstance(m["content"], list) and len(m["content"]) == 1 and m["content"][0].get("cache_control"):
                m["content"] = m["content"][0]["text"]
            out.append(m)
        return out

    i = marked(seen[2])[2]
    assert plain(seen[3][: i + 1]) == plain(seen[2][: i + 1])


def test_no_cache_breakpoints_elsewhere(env, monkeypatch):
    _connection(env)
    seen = _scripted(monkeypatch, cycles=1)
    _run(env, "a wall bracket")
    assert all(isinstance(m["content"], str) for m in seen[0])
    assert not any("cache_control" in m for msgs in seen for m in msgs)


def test_cache_control_only_for_claude_models():
    from app import agent

    fam = lambda f: SimpleNamespace(family=f)  # noqa: E731
    assert agent._wants_cache_control(fam("anthropic"), "claude-opus-4-1")
    assert agent._wants_cache_control(fam("bedrock"), "us.anthropic.claude-sonnet-4-5")
    assert agent._wants_cache_control(fam("openrouter"), "anthropic/claude-sonnet-4.5")
    assert not agent._wants_cache_control(fam("bedrock"), "amazon.nova-pro")
    assert not agent._wants_cache_control(fam("openai"), "gpt-5")


def test_persona(env):
    p = env.agent.system_prompt(True)
    assert "senior 3D CAD designer" in p and "senior structural engineer" in p
    assert "expert mechanical designer" not in p
    # the printer's hard rules are still there
    assert "clearance for mating parts" in p and "Minimum wall thickness" in p
