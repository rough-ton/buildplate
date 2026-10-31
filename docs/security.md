# Security

Buildplate holds API keys that can spend money, so treat it like any other service with credentials.

## Recommended setup

1. **Set a web UI password** in **Settings > Access and MCP**, or set `BUILDPLATE_ADMIN_PASSWORD` before first start.
2. **Generate an MCP token** so `/mcp` is not open.
3. **Keep it off the open internet.** Reach it over your LAN, a VPN, or a tunnel with an authentication layer such as Cloudflare Access.
4. **Use provider spending limits.** Most providers let you cap monthly spend per key. Create a dedicated key for Buildplate.

## How secrets are stored

- API keys and the MCP token are encrypted with Fernet (AES-128 in CBC mode with HMAC-SHA256) before being written to `/data/settings.json`.
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

## Running the code the model writes

OpenSCAD code is rendered by the OpenSCAD program inside the container, as an unprivileged user. OpenSCAD's language cannot run shell commands, and it can only read files through `include`, `use`, `import` and `surface`. Paths are resolved relative to a temporary working folder and the library folders.
