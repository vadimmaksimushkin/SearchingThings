#!/usr/bin/env bash
#
# Daily logical backup of the `places` database, uploaded to S3
#
# Triggered by pg-backup.timer at 12:00 UTC (= 06:00 GMT-6). Config comes from
# Deploy/places-host/.env:
# S3_BACKUP_BUCKET   destination prefix, e.g. s3://pg-places-backups-xxxx/backups

set -euo pipefail

# systemd user services start with a minimal PATH; make our tools findable.
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"

# Load config from .env for anything the environment didn't already provide
# (systemd sets these via EnvironmentFile; this covers running the script by hand).
ENV_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env"
read_env() { [[ -f "$ENV_FILE" ]] && grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- || true; }
: "${S3_BACKUP_BUCKET:=$(read_env S3_BACKUP_BUCKET)}"
: "${DB_CONTAINER:=$(read_env DB_CONTAINER)}"
: "${DB_NAME:=$(read_env DB_NAME)}"
: "${DB_USER:=$(read_env DB_USER)}"
: "${S3_BACKUP_BUCKET:?set S3_BACKUP_BUCKET in Deploy/places-host/.env}"
: "${DB_CONTAINER:?set DB_CONTAINER in Deploy/places-host/.env}"
: "${DB_NAME:?set DB_NAME in Deploy/places-host/.env}"
: "${DB_USER:?set DB_USER in Deploy/places-host/.env}"

STAMP="$(date -u +%Y-%m-%d)"
DEST="${S3_BACKUP_BUCKET%/}/${DB_NAME}-${STAMP}.dump"

# Dumps to a temp file first
TMP="$(mktemp "/tmp/${DB_NAME}-XXXXXX.dump")"
trap 'rm -f "$TMP"' EXIT

log() { echo "[$(date -u +%FT%TZ)] $*"; }

log "Dumping '${DB_NAME}' from container '${DB_CONTAINER}'..."
podman exec "$DB_CONTAINER" pg_dump -Fc -U "$DB_USER" "$DB_NAME" > "$TMP"

log "Dump OK ($(du -h "$TMP" | cut -f1)). Uploading to ${DEST}..."
aws s3 cp "$TMP" "$DEST"

log "Backup complete: ${DEST}"
