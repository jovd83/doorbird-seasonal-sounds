#!/bin/sh
# Run the app as whoever owns /data.
#
# The data directory is a bind mount, so its ownership is decided by the host,
# not by this image. On a Synology NAS the files arrive over SMB owned by a DSM
# user (uid 1026 or similar); a container hardcoded to uid 1000 then cannot
# write, SQLite reports "attempt to write a readonly database", and startup
# dies in the middle of the migration.
#
# Rather than chowning the host's files — which would break the owner's own
# access to them over SMB — we read the directory's uid/gid and drop to that.
# PUID/PGID override it when you want a specific identity.
set -e

DATA_DIR="${DATA_DIR:-/data}"

if [ "$(id -u)" != "0" ]; then
    # Already unprivileged (someone set `user:` in compose) — nothing to do.
    exec "$@"
fi

if [ ! -d "$DATA_DIR" ]; then
    mkdir -p "$DATA_DIR"
fi

TARGET_UID="${PUID:-$(stat -c %u "$DATA_DIR")}"
TARGET_GID="${PGID:-$(stat -c %g "$DATA_DIR")}"

# Owned by root, or an unusable id: fall back to the image's own user.
if [ "$TARGET_UID" = "0" ]; then
    TARGET_UID=0
    TARGET_GID=0
fi

mkdir -p "$DATA_DIR/mp3" "$DATA_DIR/logs" "$DATA_DIR/ulaw"

# Only the subdirectories we create are adjusted, never the caller's own files.
chown "$TARGET_UID:$TARGET_GID" \
    "$DATA_DIR/mp3" "$DATA_DIR/logs" "$DATA_DIR/ulaw" 2>/dev/null || true

# Check writability as the target uid itself. `su` cannot be used here: the
# uid usually has no /etc/passwd entry inside this image.
if ! setpriv --reuid="$TARGET_UID" --regid="$TARGET_GID" --clear-groups         test -w "$DATA_DIR" 2>/dev/null; then
    echo "entrypoint: WARNING $DATA_DIR is not writable by uid $TARGET_UID." >&2
    echo "entrypoint: the app needs write access there for its SQLite database." >&2
fi

echo "entrypoint: starting as uid=$TARGET_UID gid=$TARGET_GID (owner of $DATA_DIR)" >&2

if [ "$TARGET_UID" = "0" ]; then
    exec "$@"
fi
exec setpriv --reuid="$TARGET_UID" --regid="$TARGET_GID" --clear-groups "$@"
