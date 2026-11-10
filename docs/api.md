# REST API

Everything the web interface does is available over HTTP. An interactive reference is served at `/api/docs`.

When a web UI password is set, authenticate with a session cookie from `POST /api/auth/login`, or send the MCP token as `Authorization: Bearer <token>`.

## Design agent

### `POST /api/generate`

Runs the design agent and streams progress as newline-delimited JSON.

```bash
curl -N http://localhost:8080/api/generate \
  -H "content-type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"prompt": "A 40 mm cable clip for a 6 mm cable"}'
```

Body fields: `prompt` (required), `design_id` (to modify an existing design), `code` (start from this code instead of the saved version), `connection` (a connection id from `GET /api/connections`) and `model` (override the default). The older `provider` field (a provider family such as `anthropic`) still works and picks the first enabled connection of that family.

Event types, in order of appearance:

| Type | Fields |
| --- | --- |
| `run` | `run_id`, `provider` (family), `model`, `vision`, `connection`, `connection_label`, `billing` (`api`, `subscription`, `cloud` or `local`) |
| `status` | `message` |
| `usage` | `step`, `usage` (this call), `totals` (so far) |
| `assistant` | `text` |
| `tool` | `name` |
| `code` | `code` |
| `check` | `ok`, `stats` or `error` |
| `preview` | `image` (base64 PNG) |
| `done` | `design_id`, `version`, `title`, `code`, `summary`, `stats`, `params`, `totals` |
| `error` | `message`, `totals` |

## Rendering (no AI)

| Method and path | Description |
| --- | --- |
| `POST /api/render` | Body `{code, params?, format?: "stl"\|"3mf"\|"png", view?}`. Returns `{url, stats, warnings}` |
| `GET /api/cache/{file}` | Download a rendered file |
| `POST /api/parameters` | Body `{code}`. Returns the Customizer parameters |

## Library

| Method and path | Description |
| --- | --- |
| `GET /api/designs` | Search designs. `q` is free text plus optional qualifiers (see [search syntax](using-studio.md#library-and-search)). Filters: `tags` (comma-separated, all required), `provider`, `model`, `source`, `printer`, `min_size`, `max_size` (largest dimension in mm), `after`, `before` (`YYYY-MM-DD`), `sort` (`relevance`, `updated`, `created`, `title`, `largest`, `smallest`), `limit`. Each design includes `tags`; who made its current version (`origin`: `agent`, `manual` or `mcp`, plus `provider`, `model` and, for MCP saves, `client`); `run` with that version's tokens and cost (null when no Buildplate run produced it); and every `models` entry used across its versions |
| `GET /api/designs/facets` | Tags, provider/model pairs, sources and printers present in the library, with design counts |
| `POST /api/designs` | Create from code: `{code, title, tags?}` |
| `GET /api/designs/{id}` | Design with versions (each with `origin`, `provider`, `model`, `client` and `run` usage), messages, tags, latest code and parameters |
| `PATCH /api/designs/{id}` | Rename: `{title}` |
| `DELETE /api/designs/{id}` | Delete with all versions |
| `GET /api/designs/{id}/tags` | The design's tags |
| `PUT /api/designs/{id}/tags` | Replace tags: `{tags: [...]}`. Tags are lowercased, at most 30 per design |
| `POST /api/designs/{id}/tags` | Add tags: `{tags: [...]}` |
| `DELETE /api/designs/{id}/tags/{tag}` | Remove one tag |
| `GET /api/designs/{id}/versions/{v}` | One version |
| `POST /api/designs/{id}/versions` | Save code as a new version: `{code, summary?}` |
| `GET /api/designs/{id}/download?format=stl\|3mf\|scad\|png&version=&params=` | Download; `params` is URL-encoded JSON of overrides |

## Usage

| Method and path | Description |
| --- | --- |
| `GET /api/usage/summary?days=30` | Totals, daily series and per-model breakdown |
| `GET /api/usage/runs?limit=50&offset=0` | Requests, newest first |
| `GET /api/usage/runs/{id}` | One request with per-step usage |
| `GET /api/usage/export.csv` | All requests as CSV |

## Settings and system

| Method and path | Description |
| --- | --- |
| `GET /api/settings` | Settings with secrets masked |
| `PATCH /api/settings` | Partial update. Applied immediately |
| `GET /api/printers/catalog` | The printer catalog: manufacturers and printers with build volume, bed shape and nozzle sizes |
| `POST /api/printers/add` | Add catalog printers as profiles: `{items: [{key, nozzle}], activate?}` |
| `POST /api/printers/active` | Switch the active printer: `{id}` |
| `GET /api/auth-methods` | Every connection method with its field schema (which fields are secret), billing type and runtime |
| `GET /api/connections` | Connections with credentials masked (`{set, hint}`), the default connection, and `editable` (whether this request may change them) |
| `POST /api/connections` | Add a connection: `{method, label?, values: {field: value}, enabled?}`. Needs a password session or a request from the server itself |
| `PATCH /api/connections/{id}` | Change `label`, `enabled` or `values`. A secret given as `""` is cleared; one left out is kept. Same access rule |
| `DELETE /api/connections/{id}` | Remove a connection and its credentials. Same access rule |
| `POST /api/connections/{id}/test` | Test, save and return the model list. For a Claude plan connection, asks Claude Code whether it is signed in. Same access rule |
| `POST /api/connections/{id}/oauth/start` | Start Sign in with OpenRouter for a connection. Returns `{url}` to send the browser to; it comes back to `GET /api/oauth/openrouter/callback`. Same access rule as other connection changes |
| `POST /api/connections/{id}/chatgpt/start` | Start Sign in with ChatGPT for a ChatGPT plan connection. Returns `{url}` to open in a browser. Needs a password session |
| `POST /api/connections/{id}/chatgpt/finish` | `{url}`: the `http://127.0.0.1:1455/auth/callback?...` address the browser landed on after approving. Checks it, saves the sign-in, tests the connection and returns it with `first_time`. Needs a password session |
| `POST /api/connections/{id}/sign-out` | Sign a Claude plan connection out of Claude Code, or forget a ChatGPT plan sign-in. Needs a password session |
| `WS /api/connections/{id}/terminal?action=login\|logout` | Claude Code's own sign-in in a terminal. Needs a password session and a same-origin browser. Frames: `{type: "input"\|"resize"}` in; `{type: "output"\|"error"\|"exit"}` out |
| `GET /api/claude-code` | Whether Claude Code is installed, its pinned version and install progress |
| `POST /api/claude-code/install` | Start installing Claude Code. Needs a password session |
| `POST /api/settings/password` | `{current, new}`; an empty `new` removes the password |
| `POST /api/settings/mcp-token` | Generate a new MCP token (returned once) |
| `DELETE /api/settings/mcp-token` | Remove the MCP token |
| `GET /api/system` | Versions, libraries, storage |
| `POST /api/system/clear-cache` | Delete cached renders |
| `GET /api/health` | Health check (no auth) |

Printer profiles live in settings as `printers` (a list of `{id, name, shape, bed_x, bed_y, bed_z, nozzle, layer_height, clearance, max_overhang, preset?}`) and `active_printer` (an id). A `PATCH` that includes `printers` replaces the whole list. `GET /api/settings` also returns the active profile as `printer`.

Provider IDs: `anthropic`, `openai`, `gemini`, `openrouter`, `ollama`, `openai_compatible`.

Example: switch the default model without touching the UI.

```bash
curl -X PATCH http://localhost:8080/api/settings \
  -H "content-type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"agent": {"provider": "ollama", "model": "qwen2.5-coder:14b"}}'
```
