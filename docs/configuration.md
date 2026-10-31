# Configuration reference

Buildplate has two kinds of configuration:

- **Settings**, edited in the web interface and stored in `/data/settings.json`. They are read on every request, so changes take effect immediately with no restart.
- **Environment variables**, read when the container starts. Most are optional first-run conveniences.

## Settings

### AI providers

| Setting | Description |
| --- | --- |
| Enabled | Whether the provider's models are offered |
| API key | Stored encrypted. Never returned to the browser |
| Base URL | For Ollama and OpenAI-compatible servers |
| Models | Filled in when you test the connection |

### Design agent

| Setting | Default | Description |
| --- | --- | --- |
| Default model | none | Provider and model used when a request does not pick one |
| Max steps | 6 | Model calls allowed per request (1 to 25) |
| Temperature | 0.2 | Lower is more consistent; higher is more varied |
| Max output tokens | 8192 | Upper limit for each model response |
| Image previews | Automatic | Send render previews to the model: Automatic, Always send, Never send |
| Preview size | 768 px | Resolution of the four-view preview sheet |
| Extra instructions | empty | Added to every request |

### Printers

Save one profile per printer and nozzle size. One profile is active at a time; switch it in **Settings > Printers** or from the printer menu above the viewer in Studio. The switch is immediate.

**Add printers** opens the printer catalog: pick a manufacturer on the left (or search), then select the nozzle sizes you use on each printer. Each selection becomes a profile named like "Bambu Lab P1S 0.4 nozzle", with the printer's build volume, bed shape and sensible defaults for that nozzle. **Add custom printer** creates a blank profile for anything not in the catalog.

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

Per-model prices in US dollars per million input and output tokens, keyed `provider/model`. Used instead of the built-in price list.

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
| `ANTHROPIC_API_KEY` | none | Seeds the Anthropic key on first start |
| `OPENAI_API_KEY` | none | Seeds the OpenAI key on first start |
| `GEMINI_API_KEY` | none | Seeds the Gemini key on first start |
| `OPENROUTER_API_KEY` | none | Seeds the OpenRouter key on first start |
| `OLLAMA_BASE_URL` | none | Seeds and enables the Ollama provider on first start |

Seeded values are copied into settings once and can then be changed in the UI. Changing the environment variable later does not overwrite a value already saved.

## Data folder layout

```
/data
├── settings.json      settings (secrets encrypted)
├── .secret            encryption key (keep it with your backups)
├── buildplate.db       designs, versions, conversations, usage (SQLite)
├── cache/             rendered STL, 3MF and PNG files (safe to delete)
├── designs/           reserved for exports
└── libraries/         your own OpenSCAD libraries
```

If you lose `.secret`, saved API keys and the MCP token cannot be decrypted. Enter them again in Settings.
