#!/usr/bin/env bash
# Official Docker Engine + Compose install for Ubuntu 22.04. Run: sudo bash scripts/install_docker.sh
# Source: https://docs.docker.com/engine/install/ubuntu/
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo"; exit 1; fi
apt-get update
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker "${SUDO_USER:-$USER}"
docker --version; docker compose version
echo "Log out and back in (or run 'newgrp docker') so the docker group applies."
