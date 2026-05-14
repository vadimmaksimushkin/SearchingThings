#!/usr/bin/env bash

# Usage:
#   ./install.sh places
#   ./install.sh queue
#
set -euo pipefail

HOST_TYPE="${1:-}"
if [[ "$HOST_TYPE" != "places" && "$HOST_TYPE" != "queue" ]]; then
    echo "Usage: $0 places|queue" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/Deploy/${HOST_TYPE}-host"
SERVICE_NAME="${HOST_TYPE}-stack"

if [[ ! -f "$DEPLOY_DIR/.env" ]]; then
    echo "No .env found. Terminating..."
    exit 1
fi

sudo dnf install -y spal-release git python3-pip
sudo dnf install -y podman
sudo pip3 install podman-compose
sudo loginctl enable-linger "$(id -un)"
cd "$DEPLOY_DIR"
podman-compose up -d --build
sed -i "s|^WorkingDirectory=.*|WorkingDirectory=${DEPLOY_DIR}|" "$DEPLOY_DIR/podman-compose.service"
mkdir -p "$HOME/.config/systemd/user"
ln -sf "$DEPLOY_DIR/podman-compose.service" "$HOME/.config/systemd/user/${SERVICE_NAME}.service"
systemctl --user daemon-reload
systemctl --user enable "${SERVICE_NAME}.service"
podman ps
