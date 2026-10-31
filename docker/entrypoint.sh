#!/bin/sh
# Runs Buildplate as an unprivileged user. Set PUID/PGID to match the owner of your
# data folder (Unraid uses 99/100). Defaults to 1000/1000.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

mkdir -p /data/libraries /data/cache /data/designs /data/.home/.config

# The app runs as PUID:PGID, so HOME must be somewhere that user can write.
# OpenSCAD reads its config from $HOME/.config and aborts if it cannot.
export HOME=/data/.home
export XDG_CONFIG_HOME=/data/.home/.config

if [ "$(id -u)" = "0" ]; then
  if [ "$(stat -c %u /data)" != "$PUID" ] || [ "$(stat -c %g /data)" != "$PGID" ]; then
    chown -R "$PUID:$PGID" /data
  fi
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

exec "$@"
