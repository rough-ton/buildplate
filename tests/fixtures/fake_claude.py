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
    out({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Drafting the part."}, {"type": "tool_use", "name": "mcp__buildplate__check_design", "input": {"code": code}}], "usage": {"input_tokens": 1000, "output_tokens": 200}}})
    checked = rpc("tools/call", {"name": "check_design", "arguments": {"code": code}}, 2)
    kinds = [c["type"] for c in checked["result"]["content"]]
    (cfg / "last-check.json").write_text(json.dumps({"kinds": kinds, "text": checked["result"]["content"][0]["text"]}))
    if "NO_SUBMIT" not in prompt:
        out({"type": "assistant", "message": {"id": "m2", "content": [{"type": "tool_use", "name": "mcp__buildplate__submit_design", "input": {}}], "usage": {"input_tokens": 1500, "output_tokens": 100}}})
        rpc("tools/call", {"name": "submit_design", "arguments": {"code": code, "title": "Fake block", "summary": "A 20 mm block.", "tags": ["test", "block"]}}, 3)
    out({"type": "assistant", "message": {"id": "m3", "content": [{"type": "text", "text": "Saved."}], "usage": {"input_tokens": 1600, "output_tokens": 20}}})
    out({"type": "result", "subtype": "success", "is_error": False, "result": "Saved.", "num_turns": 3, "total_cost_usd": 0.0421,
         "usage": {"input_tokens": 4100, "output_tokens": 320, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0}})
    sys.exit(0)

print("fake claude: unsupported " + " ".join(args), file=sys.stderr)
sys.exit(2)
