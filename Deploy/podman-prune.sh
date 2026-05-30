#!/usr/bin/env bash
#
# Weekly podman housekeeping, installed on every host. `podman-compose up -d
# --build` runs on each boot and leaves superseded (dangling) image layers and
# build cache behind; on a small disk that slowly fills up and can take the
# containers (incl. Postgres) down. This reclaims that space.
#
# Deliberately NOT used:
#   -a / --all   would remove tagged images not tied to a *running* container
#                (e.g. searchapi:local while it is briefly down) -> forces rebuild.
#   --volumes    would make data volumes (e.g. pg_places_data, the database!)
#                eligible for deletion. NEVER add this here.

set -euo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"

log() { echo "[$(date -u +%FT%TZ)] $*"; }

log "Disk before: $(df -h / | awk 'NR==2{print $4" free of "$2}')"
log "Pruning dangling images, build cache, stopped containers, unused networks..."
podman system prune -f
log "Disk after:  $(df -h / | awk 'NR==2{print $4" free of "$2}')"
