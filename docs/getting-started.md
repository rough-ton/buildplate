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

If the address is reachable by anyone else on your network, set a password first: **Settings > Access and MCP > Web interface password**.

## 3. Connect an AI provider

1. Go to **Settings > AI providers**.
2. Paste an API key into one provider, or enter your Ollama address.
3. Select **Connect**. Buildplate tests the key, lists the models you can use, and turns the provider on.
4. Under **Design agent**, pick the default model.

You do not need to restart anything. See [AI providers](./providers.md) for model recommendations.

## 4. Set your printer

Under **Settings > Printer profile**, enter your bed size, nozzle diameter and the clearance you use for parts that fit together. The agent designs to these numbers, and the viewer draws your build plate at the right size.

## 5. Make something

Back in **Studio**, describe a part. Be specific about what matters:

> A wall hook for a 25 mm broom handle. 4 mm thick, two countersunk holes for #8 screws, 60 mm from the wall.

Buildplate shows each step as it works, then puts the part on the plate. Drag the parameter sliders to adjust it instantly, ask for changes in the chat, and export STL, 3MF or SCAD from the bottom of the right panel.

Next: [Using Studio](./using-studio.md).
