# Configuration reference

Buildplate has two kinds of configuration:

- **Settings**, edited in the web interface and stored in `/data/settings.json`. They are read on every request, so changes take effect immediately with no restart.
- **Environment variables**, read when the container starts. Most are optional first-run conveniences.

## Settings

### AI connections

Each connection is one way to reach a model. See [AI connections](./providers.md).

| Setting | Description |
| --- | --- |
| Name | Shown in model pickers and on usage records |
| Method | The provider and how it is reached, such as Claude API key or Ollama |
| Enabled | Whether the connection's models are offered |
| API key | Stored encrypted. Never returned to the browser |
| Base URL | For Ollama and OpenAI-compatible servers |
| Models | Filled in when you test the connection |

Adding or changing connections needs a signed-in password session, or a request from the server itself.

### Design agent

| Setting | Default | Description |
| --- | --- | --- |
| Default model | none | Connection and model used when a request does not pick one |
| Max steps | 6 | Model calls allowed per request (1 to 25) |
| Temperature | 0.2 | Lower is more consistent; higher is more varied |
| Max output tokens | 8192 | Upper limit for each model response |
| Image previews | Automatic | Send render previews to the model: Automatic, Always send, Never send |
| Preview size | 768 px | Resolution of the four-view preview sheet |
| Extra instructions | empty | Added to every request |

### Printers

Save one profile per printer and nozzle size. One profile is active at a time; switch it in **Settings > Printers** or from the printer menu above the viewer in Studio. The switch is immediate.

**Add printers** opens the printer catalog: pick a manufacturer on the left (or search), then select the nozzle sizes you use on each printer. Each selection becomes a profile named like "Bambu Lab P1S 0.4 nozzle", with the printer's build volume, bed shape and sensible defaults for that nozzle. **Add custom printer** creates a blank profile for anything not in the catalog.

Each printer is shown with an illustration of its machine style. Catalog printers get their style automatically from their model; change it in the profile editor if it is wrong for your machine. Printers the catalog cannot classify use the generic illustration.

The catalog lists over 400 printers from about 70 manufacturers. It is generated from OrcaSlicer's printer profiles, so build volumes are the slicer's usable printable area, which can be a few millimetres smaller than the marketing figure.

| Field | Default | Used for |
| --- | --- | --- |
| Name | My printer | Shown in Studio, on checks and in version history |
| Bed X, Bed Y, Max height | 256 mm | Build volume checks and the viewer grid |
| Nozzle | 0.4 mm | Minimum wall guidance and thin-part checks |
| Layer height | 0.2 mm | Design guidance |
| Fit clearance | 0.25 mm | Gap between mating parts |
| Max overhang | 45 degrees | Design guidance |
| Bed shape | Rectangular | Round for delta printers; Bed X is then the diameter |
| Printer type | From catalog | Machine style shown in the illustration: bed slinger, cantilever, open CoreXY, enclosed CoreXY, delta or generic |

At least one profile must exist. Settings from earlier versions with a single printer are converted to a profile automatically.

### Rendering

| Setting | Default | Description |
| --- | --- | --- |
| Geometry engine | Automatic | Uses Manifold when the OpenSCAD build supports it; CGAL otherwise |
| Timeout | 180 s | Maximum time for one render |
| Parallel renders | 2 | Renders allowed at once. Raise on machines with many cores |
| Preview colours | Tomorrow | OpenSCAD colour scheme for preview images |

### Access and MCP

| Setting | Description |
| --- | --- |
| Web interface password | Required to open the app when set. Stored as a scrypt hash |
| MCP token | Bearer token required on `/mcp` (and accepted on `/api`) when set |
| Public URL | Base address used in links returned by MCP tools |

### Pricing overrides

Per-model prices in US dollars per million input and output tokens, keyed `provider/model`, where provider is the provider family (`anthropic`, `openai`, `ollama` and so on), whichever connection is used. Used instead of the built-in price list.

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `PUID`, `PGID` | `1000` | User and group the app runs as. Use `99` and `100` on Unraid |
| `TZ` | UTC | Time zone for daily usage grouping |
| `BUILDPLATE_PORT` | `8080` | Port inside the container |
| `BUILDPLATE_HOST` | `0.0.0.0` | Listen address |
| `BUILDPLATE_DATA` | `/data` | Data folder |
| `BUILDPLATE_SECRET_KEY` | generated | Key used to encrypt secrets. If unset, one is generated and stored in `/data/.secret` |
| `BUILDPLATE_ADMIN_PASSWORD` | none | Sets the web UI password on first start, if none is set |
| `BUILDPLATE_MCP_TOKEN` | none | Sets the MCP token on first start, if none is set |
| `ANTHROPIC_API_KEY` | none | Adds a Claude API key connection |
| `OPENAI_API_KEY` | none | Adds an OpenAI API key connection |
| `GEMINI_API_KEY` | none | Adds a Gemini API key connection |
| `OPENROUTER_API_KEY` | none | Adds an OpenRouter API key connection |
| `XAI_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY` | none | Add a connection for that provider |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION` | none | Add an Amazon Bedrock connection with IAM credentials |
| `AWS_BEARER_TOKEN_BEDROCK` (with `AWS_REGION`) | none | Add an Amazon Bedrock connection with a Bedrock API key |
| `GOOGLE_APPLICATION_CREDENTIALS` (with optional `GOOGLE_CLOUD_PROJECT`, `VERTEXAI_LOCATION`) | none | Add a Vertex AI connection from the service account key file at that path |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENTS` | none | Add an Azure OpenAI connection. Use `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET` instead of the key for a service principal |
| `OLLAMA_BASE_URL` | none | Adds an Ollama connection |
| `BUILDPLATE_CLAUDE_CODE_VERSION` | `2.1.311` | Claude Code version installed for Claude plan connections |
| `BUILDPLATE_CLAUDE_BIN` | none | Use this Claude Code binary instead of installing one (for example one you installed yourself) |

A variable adds its connection only when no connection of that method exists yet. It can then be changed in the UI; changing the environment variable later does not overwrite it. If you remove the connection while the variable is still set, it is added again on the next start.

Settings files from before connections existed are converted automatically on first start: every provider with a saved key, or that was turned on, becomes a connection, and the default model keeps pointing at it. For one release, `GET /api/settings` still includes the old `providers` object (read-only) and `agent.provider`.

## Data folder layout

```
/data
├── settings.json      settings (secrets encrypted)
├── .secret            encryption key (keep it with your backups)
├── buildplate.db       designs, versions, conversations, usage (SQLite)
├── cache/             rendered STL, 3MF and PNG files (safe to delete)
├── designs/           reserved for exports
├── libraries/         your own OpenSCAD libraries
├── tools/claude-code/ Claude Code, installed when you add a Claude plan connection
└── claude-code/       Claude Code's own folder per Claude plan connection, including its sign-in
```

Backups of the data folder include Claude plan sign-ins. Treat them like the credentials they are.

To ship Claude Code inside the image instead of downloading it on first use, build with `docker build --build-arg CLAUDE_CODE_VERSION=2.1.311 .`. Environment variables such as `ANTHROPIC_API_KEY` are never passed to Claude Code, so they cannot change how a Claude plan request is billed.

If you lose `.secret`, saved connection credentials and the MCP token cannot be decrypted. Enter them again in Settings.
