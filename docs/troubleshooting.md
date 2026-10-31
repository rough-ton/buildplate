# Troubleshooting

## "No AI model selected"

Open **Settings > AI providers**, connect a provider, then choose a **Default model** under **Design agent** and select **Save changes**.

## "The provider rejected the API key"

The key was revoked, mistyped or has no billing set up. Paste it again and select **Test and load models**. Check the provider's console for spending limits.

## Ollama: "Could not reach ..."

- Inside the container, `localhost` is the container itself. Use the host's LAN IP or `http://host.docker.internal:11434`.
- Make sure Ollama listens on all interfaces: set `OLLAMA_HOST=0.0.0.0` where Ollama runs.
- Check a firewall is not blocking port 11434.

## The model keeps failing to render

- Look at the request's steps on the **Usage** page; failed renders show the OpenSCAD error.
- Try a more capable model, or one that supports tool calling and vision.
- Be more specific about the geometry, or ask for a simpler first version and refine it.
- Raise **Max steps** if the agent runs out of attempts while making progress.

## Renders time out

Complex models, high `$fn` values and many boolean operations take longer. Check **Settings > System** shows OpenSCAD "with Manifold"; the Manifold engine is many times faster than CGAL. Raise **Rendering > Timeout** if needed.

## "Model has no tool support; switching to text mode"

The selected model rejected tool definitions. Buildplate falls back to asking for fenced code blocks, which works but is less reliable. Use a model with tool calling for best results.

## Cost shows "n/a"

The model is not in LiteLLM's price list. Add a price in **Settings > Pricing overrides**.

## Forgot the web UI password

Stop the container and edit `/data/settings.json`: set `"ui_password_hash"` under `"security"` to `""`. Start the container and set a new password in Settings. Or start the container with `BUILDPLATE_ADMIN_PASSWORD` after clearing the hash.

## Permission errors on the data folder

Set `PUID` and `PGID` to the owner of the folder. On Unraid that is `99` and `100`. The container fixes ownership of `/data` on start when they differ.

## Settings > System shows "OpenSCAD: Not working"

The message after it is OpenSCAD's own error. A permission error on a `.config` folder means the image predates the fix for running as an unprivileged user; pull the latest image. Otherwise check `docker logs buildplate`.

## Library thumbnails are blank

Thumbnails are rendered by OpenSCAD on first view. If they stay blank, check the container log for OpenSCAD errors and confirm the design still renders in Studio.

## Where are the logs?

```bash
docker logs -f buildplate
```
