# Getting started

Buildplate turns a plain-language description into a parametric OpenSCAD model, checks that it will print, and hands you an STL or 3MF. Everything runs in one container on your own hardware.

## What you need

- Docker on Linux, macOS, Windows or Unraid (x86-64).
- An account with at least one AI provider, or a local model server such as Ollama.
- About 1.5 GB of disk for the image, plus space for your designs.

## 1. Start the container

```bash
docker run -d --name buildplate \
  -p 8080:8080 \
  -v buildplate-data:/data \
  --add-host=host.docker.internal:host-gateway \
  --restart unless-stopped \
  ghcr.io/rough-ton/buildplate:latest
```

Or with Compose, using the `docker-compose.yml` in the repository:

```bash
docker compose up -d
```

The image is published to GitHub Container Registry for every release, so there is nothing to build. Use `:latest` for the newest release or a version tag such as `:0.1` to stay on a release line.

Everything Buildplate keeps lives in `/data`. Back up that folder and you have backed up the whole app.

On Unraid, follow the [Unraid guide](./unraid.md) instead.

## 2. Open the web interface

Browse to `http://<your-host>:8080`. The Studio opens with an empty build plate.

## 3. Set a password

Go to **Settings > Access and MCP** and set a **Web interface password**. Buildplate asks for one before you add AI connections, because they hold credentials. You can also set `BUILDPLATE_ADMIN_PASSWORD` before the first start.

## 4. Connect an AI model

1. Go to **Settings > AI connections**.
2. Under **Add a connection**, choose a provider, then paste an API key or enter your Ollama address.
3. Select **Connect**. Buildplate saves the connection, lists the models you can use, and makes the first one the default.
4. Under **Design agent**, change the default model if you like.

You do not need to restart anything. See [AI connections](./providers.md) for model recommendations.

## 5. Add your printers

Under **Settings > Printers**, select **Add printers**. Choose your printer's manufacturer, then the nozzle sizes you use, and select **Add**. The catalog covers over 400 printers; use **Add custom printer** for anything else. Adjust the nozzle diameter and the clearance you use for parts that fit together. Add a profile for each printer you own; the menu above the viewer in Studio switches between them. The agent designs to the active printer, and the viewer draws its build plate.

## 6. Make something

Back in **Studio**, describe a part. Be specific about what matters:

> A wall hook for a 25 mm broom handle. 4 mm thick, two countersunk holes for #8 screws, 60 mm from the wall.

Buildplate shows each step as it works, then puts the part on the plate. Drag the parameter sliders to adjust it instantly, ask for changes in the chat, and export STL, 3MF or SCAD from the bottom of the right panel.

Next: [Using Studio](./using-studio.md).
