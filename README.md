<p align="center">
  <img src="assets/icon.png" width="88" alt="">
</p>

<h1 align="center">Buildplate</h1>

<p align="center">
  A self-hosted AI design studio for 3D printing.<br>
  Describe a part. Get validated, parametric OpenSCAD and a ready-to-slice STL or 3MF.
</p>

<p align="center">
  <img src="assets/screenshots/studio.png" alt="Buildplate Studio with a generated part on the build plate, its parameters and the chat that produced it" width="900">
</p>

Buildplate runs as a single Docker container. Point it at the AI provider you already use (Claude, OpenAI, Gemini, OpenRouter, Ollama or any OpenAI-compatible server), describe what you need, and it writes parametric OpenSCAD, renders it, checks the mesh against your printer, looks at the result, fixes what is wrong and puts the part on your build plate. Every request shows exactly how many tokens it used and what it cost.

## Highlights

- **One container, nothing to install.** OpenSCAD with the Manifold engine, headless rendering, BOSL2 and MCAD, the backend, the web UI and the MCP endpoint are all inside the image.
- **Bring any model.** Claude, OpenAI, Gemini, OpenRouter, Ollama, LM Studio, vLLM and other OpenAI-compatible servers. Switch models per request.
- **Configure everything in the browser.** Add API keys, connect providers, pick models, change your printer profile and limits. Changes are live immediately; nothing restarts or rebuilds.
- **A design loop that checks its own work.** The agent renders every draft, validates it (watertight, on the bed, inside your build volume) and, with vision-capable models, reviews a four-view preview before saving.
- **All your printers.** Save a profile per printer, start from presets for popular models, and switch the target printer from Studio. The plate, checks and design limits follow.
- **Live parameters.** Customizer variables become sliders. Drag one and the part re-renders in seconds without spending a single token.
- **Token and cost tracking.** Each request shows input, output and cached tokens plus cost, with a per-step breakdown, daily charts, per-model totals and CSV export.
- **Library with version history.** Every generation and manual edit is a version you can reopen, compare and export.
- **MCP endpoint.** Use the renderer, validator, library and design agent from Claude Desktop, Claude Code or any MCP client.

## Quick start

```bash
docker run -d --name buildplate \
  -p 8080:8080 \
  -v buildplate-data:/data \
  --add-host=host.docker.internal:host-gateway \
  --restart unless-stopped \
  ghcr.io/rough-ton/buildplate:latest
```

Open `http://<your-host>:8080`, go to **Settings > AI providers**, paste an API key (or point it at Ollama) and select **Connect**. Then describe your first part in **Studio**.

Prefer Compose? See [docker-compose.yml](docker-compose.yml). Running Unraid? Use the [Unraid template](deploy/unraid/buildplate.xml) and the [Unraid guide](docs/unraid.md).

## Updating

```bash
docker pull ghcr.io/rough-ton/buildplate:latest
docker stop buildplate && docker rm buildplate
# then run the same docker run command again; /data is kept
```

With Compose: `docker compose pull && docker compose up -d`.

Images are built and published by GitHub Actions on every release. You never need to build anything yourself. Pin a version tag such as `ghcr.io/rough-ton/buildplate:0.1` instead of `latest` if you prefer to update deliberately.

## Screenshots

| Library | Usage |
| --- | --- |
| <img src="assets/screenshots/library.png" alt="Library of saved designs" width="440"> | <img src="assets/screenshots/usage.png" alt="Token usage dashboard" width="440"> |

| Settings | Dark mode |
| --- | --- |
| <img src="assets/screenshots/settings.png" alt="Provider settings" width="440"> | <img src="assets/screenshots/studio-dark.png" alt="Studio in dark mode" width="440"> |

## Documentation

The same documentation is built into the app under **Docs**.

| Guide | What it covers |
| --- | --- |
| [Getting started](docs/getting-started.md) | Install, first run, first design |
| [AI providers](docs/providers.md) | Setting up each provider, choosing models, local models |
| [Using Studio](docs/using-studio.md) | Prompting, parameters, code editing, versions, exports |
| [Token usage and cost](docs/token-usage.md) | What is counted, how cost is estimated, keeping usage low |
| [MCP integration](docs/mcp.md) | Claude Desktop, Claude Code and other MCP clients |
| [Unraid](docs/unraid.md) | Template install, paths, permissions, reverse proxy |
| [Configuration reference](docs/configuration.md) | Every setting and environment variable |
| [Security](docs/security.md) | Passwords, tokens, key storage, exposure |
| [REST API](docs/api.md) | Endpoints for scripting and integrations |
| [Troubleshooting](docs/troubleshooting.md) | Common problems and fixes |

## How it works

```
 Browser (Studio, Library, Usage, Settings)          MCP clients
            |                                             |
            v                                             v
 +------------------------------------------------------------------+
 |  Buildplate container                                             |
 |                                                                  |
 |  FastAPI  ----  Design agent  ----  LiteLLM  ---->  your AI provider
 |    |               |                                             |
 |    |               v                                             |
 |    |         OpenSCAD (Manifold) + Xvfb  ->  STL / 3MF / PNG      |
 |    |               |                                             |
 |    |               v                                             |
 |    |         Mesh checks (trimesh)                               |
 |    v                                                             |
 |  /data: settings.json (keys encrypted), SQLite, render cache,    |
 |         your OpenSCAD libraries                                  |
 +------------------------------------------------------------------+
```

Settings are read from `/data/settings.json` on every request, which is why changes made in the UI take effect immediately.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for running from source and the release process.

## License

[MIT](LICENSE). Bundled third-party components keep their own licenses: OpenSCAD (GPL-2.0, used as a separate program), BOSL2 (BSD-2-Clause), MCAD (LGPL-2.1), three.js (MIT), CodeMirror (MIT), marked (MIT), IBM Plex (OFL-1.1).
