#!/usr/bin/env bash

# Usage:
#   ./install.sh places
#   ./install.sh queue
#   ./install.sh image
#
set -euo pipefail

HOST_TYPE="${1:-}"
if [[ "$HOST_TYPE" != "places" && "$HOST_TYPE" != "queue" && "$HOST_TYPE" != "image" ]]; then
    echo "Usage: $0 places|queue|image" >&2
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

# Weekly podman cleanup — all hosts
chmod +x "$REPO_ROOT/Deploy/podman-prune.sh"
ln -sf "$REPO_ROOT/Deploy/podman-prune.service" "$HOME/.config/systemd/user/podman-prune.service"
ln -sf "$REPO_ROOT/Deploy/podman-prune.timer"   "$HOME/.config/systemd/user/podman-prune.timer"
systemctl --user daemon-reload
systemctl --user enable --now podman-prune.timer

# Daily database backup to S3 — places host only. Mirrors the symlink pattern
# above: unit files live in the repo, symlinked into the user systemd dir; the
# symlinks and the gitignored .env both survive the boot-time `git reset`.
if [[ "$HOST_TYPE" == "places" ]]; then
    # AWS CLI v2 for the S3 backups, official self-contained installer
    if ! /usr/local/bin/aws --version >/dev/null 2>&1; then
        sudo dnf install -y unzip
        curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscliv2.zip
        ( cd /tmp && unzip -oq awscliv2.zip && sudo ./aws/install --update )
        rm -rf /tmp/aws /tmp/awscliv2.zip
    fi

    chmod +x "$DEPLOY_DIR/backup/pg-backup.sh" "$DEPLOY_DIR/backup/pg-restore.sh"
    ln -sf "$DEPLOY_DIR/backup/pg-backup.service" "$HOME/.config/systemd/user/pg-backup.service"
    ln -sf "$DEPLOY_DIR/backup/pg-backup.timer"   "$HOME/.config/systemd/user/pg-backup.timer"
    systemctl --user daemon-reload
    systemctl --user enable --now pg-backup.timer
fi

podman ps
