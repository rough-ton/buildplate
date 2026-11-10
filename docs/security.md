# Security

Buildplate holds credentials for AI connections that can spend money, so treat it like any other service with credentials.

## Reporting a vulnerability

Please report security problems privately through GitHub's
[private vulnerability reporting](https://github.com/rough-ton/buildplate/security/advisories/new)
(the repository's **Security** tab, then **Report a vulnerability**), not in a public issue.
Include the Buildplate version, how you run it, and steps to reproduce.

## Recommended setup

1. **Set a web UI password** in **Settings > Access and MCP**, or set `BUILDPLATE_ADMIN_PASSWORD` before first start. Adding or changing AI connections requires it.
2. **Generate an MCP token** so `/mcp` is not open.
3. **Keep it off the open internet.** Reach it over your LAN, a VPN, or a tunnel with an authentication layer such as Cloudflare Access.
4. **Use provider spending limits.** Most providers let you cap monthly spend per key. Create a dedicated key for Buildplate.

## How secrets are stored

- Connection credentials (each method declares which of its fields are secret) and the MCP token are encrypted with Fernet (AES-128 in CBC mode with HMAC-SHA256) before being written to `/data/settings.json`.
- The encryption key is in `/data/.secret` (mode 600), or derived from `BUILDPLATE_SECRET_KEY` if you set it.
- The web UI password is stored as a salted scrypt hash.
- The browser never receives saved keys. It only sees whether one is set and its last four characters.

Anyone with read access to both `settings.json` and `.secret` can recover the keys, so protect the data folder.

## Sessions

Signing in sets an HTTP-only, SameSite=Lax cookie signed with a key derived from the install secret. Sessions last 7 days by default. Changing the password does not end other sessions; to sign everyone out, delete `/data/.secret` (you will then need to re-enter API keys).

## What is open without signing in

- The static web interface files.
- `GET /api/health` (for container health checks).
- `/api/auth/*` (to sign in).
- `/api/docs` (the API reference page; it contains no data).

Everything else under `/api` requires a session or the MCP bearer token when a password is set.

## Changing AI connections

Creating, editing, testing or removing a connection (`POST`, `PATCH` and `DELETE` under `/api/connections`) needs one of:

- a signed-in web UI password session, or
- a request from the server itself: a direct connection from `127.0.0.1` or `::1` with no `X-Forwarded-For`, `Forwarded` or `X-Real-IP` header, so a reverse proxy on the same host does not count.

The MCP bearer token is not enough. Without a password, connections can still be listed (credentials masked) and used, and environment-variable connections are still added, but they cannot be changed from the browser. Inside Docker, a browser on the host connects through the Docker network rather than loopback, so in practice you need a password.

## Claude plan connections

- Adding, changing, signing in or out of a Claude plan connection needs a web UI password and a signed-in session. Unlike other connections, a request from the server itself is not enough.
- Sign-in uses a browser terminal over a WebSocket. It needs that session and a same-origin browser, and only runs `claude auth login` or `claude auth logout`: never a shell or the interactive Claude Code app. It closes after 10 minutes without activity.
- Claude Code keeps its own credentials in `/data/claude-code/<connection>` (mode 700). Buildplate does not read them.
- Each request runs Claude Code with no built-in tools (`--tools ""`, `--restricted`), only Buildplate's MCP server (`--strict-mcp-config`), in an empty temporary folder, with a minimal environment that excludes `ANTHROPIC_*`, cloud-provider and token variables.
- Claude Code reaches Buildplate's tools at `/mcp-run`, which accepts only loopback requests carrying the request's one-time token. The token stops working when the request ends.
- Claude Code is downloaded from Anthropic's release bucket and verified against the SHA-256 in Anthropic's release manifest before it is used. Its self-updater is turned off so the pinned version stays in place.

## Copilot plan connections

- Adding or changing a Copilot plan connection needs a web UI password and a signed-in session, even from the server itself.
- The GitHub token is stored encrypted in `settings.json` like other keys. Buildplate asks for a fine-grained token with only the **Copilot requests** permission and rejects classic `ghp_` tokens.
- Each request runs GitHub's Copilot runtime through the Copilot SDK in its isolated ("empty") mode, with an allowlist of only Buildplate's `check_design`, `list_parts` and `submit_design`. It has no shell, file or web tools, runs in an empty temporary folder, and gets a minimal environment with its own home folder (`/data/copilot/<connection>`) and none of the server's `GH_TOKEN`, `GITHUB_TOKEN` or other variables. Tool permission requests are refused.
- The runtime is downloaded by the SDK from GitHub's `github/copilot-cli` releases into `/data/tools/copilot` and verified against GitHub's published SHA-256 sums before it is used. The SDK version (and so the runtime version) is pinned in `requirements.txt`.

## Update check

To show whether a newer release is out, Settings asks GitHub's API for the latest Buildplate release (`https://api.github.com/repos/rough-ton/buildplate/releases/latest`).

- It sends nothing about you, your designs or your connections: only a `Buildplate/<version>` user agent and your server's IP address, which GitHub sees for any request.
- It runs when you open Settings, at most every 6 hours (30 minutes after a failed check), and never blocks the page. Offline or blocked, Settings just shows the version.
- Builds without a release version (`dev` and `edge-...`) don't check.
- Turn it off under **Settings, About, Check for updates**.

## Running the code the model writes

OpenSCAD code is rendered by the OpenSCAD program inside the container, as an unprivileged user. OpenSCAD's language cannot run shell commands, and it can only read files through `include`, `use`, `import` and `surface`. Paths are resolved relative to a temporary working folder and the library folders.
