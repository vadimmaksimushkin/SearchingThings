#!/usr/bin/env bash
#
# Restore a `places` backup from S3 into a SEPARATE database, to verify that
# backups are actually recoverable. This never touches the live `places` DB.
#
# Usage:
#   ./pg-restore.sh latest                 # newest backup in the bucket
#   ./pg-restore.sh 2026-05-29             # backup for a specific date
#   ./pg-restore.sh s3://bucket/backups/places-2026-05-29.dump
#   ./pg-restore.sh latest places_check    # custom target DB name
#
# Reads S3_BACKUP_BUCKET from the environment or from Deploy/places-host/.env.

set -euo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"

# Load config from .env for anything the environment didn't already provide.
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

SRC="${1:?Usage: $0 <latest|YYYY-MM-DD|s3://.../file.dump> [target_db]}"
TARGET_DB="${2:-places_restore}"
PREFIX="${S3_BACKUP_BUCKET%/}"

# Resolve the source argument to a full s3:// key.
case "$SRC" in
    s3://*) KEY="$SRC" ;;
    latest) KEY="${PREFIX}/$(aws s3 ls "${PREFIX}/" | awk '{print $4}' | sort | tail -n1)" ;;
    *)      KEY="${PREFIX}/${DB_NAME}-${SRC}.dump" ;;
esac

echo "Restoring ${KEY} -> database '${TARGET_DB}' ('${DB_NAME}' DB is untouched)"

TMP="$(mktemp /tmp/restore-XXXXXX.dump)"
trap 'rm -f "$TMP"' EXIT
aws s3 cp "$KEY" "$TMP"

# Fresh target DB, then load the dump inside the container.
podman exec "$DB_CONTAINER" psql -U "$DB_USER" -c "DROP DATABASE IF EXISTS ${TARGET_DB};"
podman exec "$DB_CONTAINER" psql -U "$DB_USER" -c "CREATE DATABASE ${TARGET_DB};"
podman cp "$TMP" "${DB_CONTAINER}:/tmp/restore.dump"
podman exec "$DB_CONTAINER" pg_restore -U "$DB_USER" -d "$TARGET_DB" --no-owner /tmp/restore.dump
podman exec "$DB_CONTAINER" rm -f /tmp/restore.dump
