"""A stand-in for the Claude Code CLI, used by tests. Implements just enough:
--version, auth status|login|logout, and -p with stream-json output that drives
Buildplate's run-scoped MCP endpoint like Claude Code would."""

import json
import os
import sys
import urllib.request
from pathlib import Path

cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR", "."))
marker = cfg / "fake-signed-in"
args = sys.argv[1:]


def out(obj):
    print(json.dumps(obj), flush=True)


if args[:1] == ["--version"]:
    print("2.1.311 (Claude Code)")
    sys.exit(0)

if args[:2] == ["auth", "status"]:
    if marker.exists():
        out({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"})
        sys.exit(0)
    out({"loggedIn": False, "authMethod": "none"})
    sys.exit(1)

if args[:2] == ["auth", "login"]:
    print("Browser didn't open? Use the url below to sign in:\r\n\r\nhttps://claude.ai/oauth/authorize?code=true&fake=1\r\n", flush=True)
    sys.stdout.write("Paste code here if prompted > ")
    sys.stdout.flush()
    code = sys.stdin.readline().strip()
    if code == "good-code":
        marker.write_text("1")
        print("\r\nLogin successful. Press Enter to continue", flush=True)
        sys.exit(0)
    print("\r\nInvalid code", flush=True)
    sys.exit(1)

if args[:2] == ["auth", "logout"]:
    marker.unlink(missing_ok=True)
    print("Successfully logged out")
    sys.exit(0)

if args[:2] == ["-p", "/usage"]:
    if not marker.exists():
        out({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"})
        sys.exit(1)
    report = {"session": {"total_cost_usd": 0, "model_usage": {}}, "rate_limits": {"limits": [
        {"kind": "session", "group": "session", "percent": 23.5, "resets_at": "2026-11-10T18:00:00Z", "scope": None, "severity": "normal", "is_active": True},
        {"kind": "weekly_all", "group": "weekly", "percent": 41, "resets_at": "2026-11-14T09:00:00Z", "scope": None, "severity": "normal", "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 88, "resets_at": "2026-11-14T09:00:00Z", "scope": {"model": {"display_name": "Opus"}}, "severity": "warning", "is_active": False},
    ], "extra_usage": {"is_enabled": True, "monthly_limit": 5000, "used_credits": 1250, "utilization": 25, "currency": "usd"}}}
    out({"type": "assistant", "message": {"id": "u1", "content": [{"type": "text", "text": "Current session: 23% used"}], "usage": {"input_tokens": 0, "output_tokens": 0}}, "usage_report": report, "parent_tool_use_id": None})
    out({"type": "result", "subtype": "success", "is_error": False, "result": "Current session: 23% used", "num_turns": 0, "total_cost_usd": 0, "usage": {"input_tokens": 0, "output_tokens": 0}, "modelUsage": {}})
    sys.exit(0)

if args[:1] == ["-p"]:
    prompt = sys.stdin.read()
    opt = {args[i]: args[i + 1] for i in range(1, len(args) - 1) if args[i].startswith("--")}
    (cfg / "last-run.json").write_text(json.dumps({"args": args, "env": dict(os.environ), "cwd": os.getcwd(), "cwd_files": sorted(os.listdir(".")), "prompt": prompt}))
    model = opt.get("--model", "sonnet")
    out({"type": "system", "subtype": "init", "model": "claude-sonnet-5-5" if model == "sonnet" else model, "tools": []})
    if "FAIL_AUTH" in prompt:
        out({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"})
        sys.exit(1)
    server = json.loads(Path(opt["--mcp-config"]).read_text())["mcpServers"]["buildplate"]
    headers = {**server["headers"], "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    proto = {}

    def rpc(method, params, rid):
        h = dict(headers, **({"mcp-protocol-version": proto["v"]} if proto else {}))
        body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode()
        req = urllib.request.Request(server["url"], data=body, headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake-claude", "version": "1"}}, 1)
    proto["v"] = init["result"]["protocolVersion"]
    code = "cube([20, 10, 5]);"
    resolved = "claude-sonnet-5-5" if model == "sonnet" else model

    def response(mid, usage, final_output, content):
        """One API response the way Claude Code streams it: message_start with a
        placeholder output count, an assistant message per content block repeating
        that placeholder, then message_delta with the real output count."""
        start = dict(usage, output_tokens=1)
        out({"type": "stream_event", "event": {"type": "message_start", "message": {"id": mid, "usage": start}}, "parent_tool_use_id": None})
        for block in content:
            out({"type": "assistant", "message": {"id": mid, "content": [block], "usage": start}, "parent_tool_use_id": None})
        out({"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": final_output}}, "parent_tool_use_id": None})
        out({"type": "stream_event", "event": {"type": "message_stop"}, "parent_tool_use_id": None})

    # step 1: 1000 new input, 3000 written to the 1-hour cache, 250 output
    response("m1", {"input_tokens": 1000, "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 0,
                    "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 3000}}, 250,
             [{"type": "text", "text": "Drafting the part."}, {"type": "tool_use", "name": "mcp__buildplate__check_design", "input": {"code": code}}])
    # a subagent's traffic must not be counted per step (modelUsage covers the run)
    out({"type": "assistant", "message": {"id": "sub1", "content": [{"type": "text", "text": "sub"}], "usage": {"input_tokens": 99999, "output_tokens": 1}}, "parent_tool_use_id": "toolu_x"})
    out({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning", "utilization": 0.82, "resetsAt": 1763000000}})
    checked = rpc("tools/call", {"name": "check_design", "arguments": {"code": code}}, 2)
    kinds = [c["type"] for c in checked["result"]["content"]]
    (cfg / "last-check.json").write_text(json.dumps({"kinds": kinds, "text": checked["result"]["content"][0]["text"]}))
    if "CRASH" in prompt:
        sys.exit(1)  # no result message: totals must come from the steps
    if "RATE_LIMIT" in prompt:
        out({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1763000000, "utilization": 1.0}})
        out({"type": "result", "subtype": "success", "is_error": True, "result": "Claude AI usage limit reached",
             "num_turns": 1, "total_cost_usd": 0.0165, "usage": {"input_tokens": 1000, "output_tokens": 250, "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 0}, "modelUsage": {}})
        sys.exit(1)
    if "NO_SUBMIT" not in prompt:
        # step 2: 200 new, 3000 read from cache, 500 written (5-minute), 120 output
        response("m2", {"input_tokens": 200, "cache_creation_input_tokens": 500, "cache_read_input_tokens": 3000,
                        "cache_creation": {"ephemeral_5m_input_tokens": 500, "ephemeral_1h_input_tokens": 0}}, 120,
                 [{"type": "tool_use", "name": "mcp__buildplate__submit_design", "input": {}}])
        rpc("tools/call", {"name": "submit_design", "arguments": {"code": code, "title": "Fake block", "summary": "A 20 mm block.", "tags": ["test", "block"]}}, 3)
    # step 3: 50 new, 3500 read from cache, 15 output
    response("m3", {"input_tokens": 50, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3500}, 15, [{"type": "text", "text": "Saved."}])
    out({"type": "result", "subtype": "success", "is_error": False, "result": "Saved.", "num_turns": 3, "total_cost_usd": 0.0321,
         "usage": {"input_tokens": 1250, "output_tokens": 385, "cache_read_input_tokens": 6500, "cache_creation_input_tokens": 3500},
         "modelUsage": {resolved: {"inputTokens": 1250, "outputTokens": 385, "cacheReadInputTokens": 6500, "cacheCreationInputTokens": 3500,
                                   "webSearchRequests": 0, "costUSD": 0.0321, "contextWindow": 200000, "maxOutputTokens": 64000, "costBasis": "list"}}})
    sys.exit(0)

print("fake claude: unsupported " + " ".join(args), file=sys.stderr)
sys.exit(2)
