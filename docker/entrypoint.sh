#!/bin/sh
# Runs Buildplate as an unprivileged user. Set PUID/PGID to match the owner of your
# data folder (Unraid uses 99/100). Defaults to 1000/1000.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# The app runs as PUID:PGID, so HOME must be somewhere that user can write.
# OpenSCAD reads its config from $HOME/.config and aborts if it cannot.
export HOME=/data/.home
export XDG_CONFIG_HOME=/data/.home/.config

DIRS="/data/libraries /data/cache /data/designs /data/.home/.config"

if [ "$(id -u)" = "0" ]; then
  mkdir -p $DIRS
  # Fix ownership of anything under /data that the app user does not own,
  # including folders just created above. Checking only the top of /data misses
  # subfolders created as root when /data itself is already owned correctly.
  find /data \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown -h "$PUID:$PGID" {} +
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

mkdir -p $DIRS
exec "$@"
