# Unraid

## Install from the template

Until Buildplate is listed in Community Applications, add the template manually:

```bash
mkdir -p /boot/config/plugins/dockerMan/templates-user
wget -O /boot/config/plugins/dockerMan/templates-user/my-Buildplate.xml \
  https://raw.githubusercontent.com/rough-ton/buildplate/main/deploy/unraid/buildplate.xml
```

Then in the Unraid web UI: **Docker > Add Container**, choose **Buildplate** from the **Template** list, review the settings and select **Apply**.

## Template settings

| Setting | Default | Notes |
| --- | --- | --- |
| Web UI port | `8080` | Web interface, REST API and MCP endpoint |
| App data | `/mnt/user/appdata/buildplate` | Settings, encrypted keys, library, usage history, render cache |
| PUID / PGID | `99` / `100` | Matches Unraid's `nobody:users` ownership |
| Time zone | `America/Denver` | Used for the daily usage chart |
| Initial admin password | empty | Optional; sets the web UI password on first start |

The template adds `--add-host=host.docker.internal:host-gateway`, so a model server running on the Unraid host (for example the Ollama container) is reachable at `http://host.docker.internal:11434`. Using the host's LAN IP also works.

## Adding OpenSCAD libraries

Copy library folders into `/mnt/user/appdata/buildplate/libraries`. For example, to add NopSCADlib:

```bash
cd /mnt/user/appdata/buildplate/libraries
git clone --depth 1 https://github.com/nophead/NopSCADlib
chown -R 99:100 NopSCADlib
```

They are available on the next render and listed in **Settings > About**.

## Opening STL files directly

Exports download through your browser. Save them to a share your slicer can see, such as `/mnt/user/3dprint`, to open them on any machine on your network.

## Updating

New releases are published to GitHub Container Registry automatically. In the Unraid web UI, select **Check for Updates** on the Docker tab, then **apply update** for the container. Your data folder is kept.

## Remote access

Do not forward the port directly to the internet. Use a VPN such as WireGuard or Tailscale, or publish it through Cloudflare Tunnel with an Access policy in front. Set a web UI password and an MCP token either way. See [Security](./security.md).
