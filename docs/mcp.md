# MCP integration

Buildplate includes a [Model Context Protocol](https://modelcontextprotocol.io) server at `/mcp` on the same port as the web interface. Any MCP client can use it to render and validate OpenSCAD, save designs to your library, and run the built-in design agent.

The endpoint uses the Streamable HTTP transport.

## Tools

| Tool | What it does |
| --- | --- |
| `check_design` | Renders code, validates the mesh for printing, returns a report and a four-view preview image |
| `preview` | Renders a PNG from one or more views: `iso`, `front`, `right`, `top`, `back_iso` |
| `save_design` | Validates and saves code to the library (as a new design, or a new version of `design_id`), optionally with `tags`, returns STL, 3MF and SCAD links |
| `generate_design` | Runs Buildplate's own design agent with the model configured in Settings, tracking tokens in Buildplate |
| `list_designs` | Searches saved designs by title, description, tags, prompt, provider and model, with filters for tags, provider, model, source, printer, size and date. Accepts the same [search syntax](using-studio.md#library-and-search) as the Library |
| `set_design_tags` | Replaces the tags on a saved design |
| `get_design` | Returns the code, tags and stats of a saved design |
| `get_parameters` | Extracts Customizer parameters from code |
| `environment` | Returns the active printer, all saved printer profiles, available libraries and the OpenSCAD version |
| `set_printer` | Switches the active printer profile by id |
| `find_printers` | Searches the printer catalog by name |
| `add_printer` | Adds a catalog printer with a nozzle size as a profile, optionally making it active |

With `check_design` and `save_design`, the client's own model does the design work and Buildplate supplies rendering, validation and storage. With `generate_design`, Buildplate's configured model does the work and the client just receives the result.

## Secure the endpoint first

By default the endpoint is open to anyone who can reach the port. Go to **Settings > Access and MCP** and select **Generate token**. Copy the token when it is shown; it is not displayed again. Clients must then send it as `Authorization: Bearer <token>`.

## Claude Code

```bash
claude mcp add --transport http buildplate http://<your-host>:8080/mcp \
  --header "Authorization: Bearer <token>"
```

## Claude Desktop

Claude Desktop starts local MCP servers from its configuration file. Use the `mcp-remote` bridge to reach Buildplate over HTTP. In `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "buildplate": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "http://<your-host>:8080/mcp",
        "--allow-http",
        "--header",
        "Authorization: Bearer ${BUILDPLATE_TOKEN}"
      ],
      "env": { "BUILDPLATE_TOKEN": "<token>" }
    }
  }
}
```

Omit `--allow-http` when you use an `https://` address. Restart Claude Desktop after editing the file.

## Other clients

Any client that supports Streamable HTTP works. Use the URL `http(s)://<your-host>:<port>/mcp` and add the bearer token header.

## Behind a reverse proxy or Zero Trust gateway

Buildplate works behind Nginx, Caddy, Traefik, Cloudflare Tunnel and similar. When you publish it on a domain:

1. Set **Settings > Access and MCP > Public URL** to the address clients use, such as `https://buildplate.example.com`. Download links returned by MCP tools are built from it.
2. Make sure the proxy passes the `Authorization` header and does not buffer responses.
3. If your gateway already authenticates requests (for example a Cloudflare Access application or an MCP portal), you can rely on it, but keeping the Buildplate token as well is recommended.

## Example prompts for an MCP client

> Use buildplate to design a 40 mm fan grille with 4.3 mm mounting holes on a 32 mm pattern. Check it before saving and give me the STL link.

> List my buildplate designs and make version 2 of the cable comb with ten slots instead of eight.
