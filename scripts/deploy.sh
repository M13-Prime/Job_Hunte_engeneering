#!/usr/bin/env bash
# Provision a fresh Ubuntu ARM (or x86_64) VM with everything needed to run
# Signal Tracker behind a Cloudflare Tunnel.
#
# Idempotent: safe to re-run (re-installs missing pieces, pulls latest code).
#
# Usage (on the VM, as the default sudo-able user, e.g. `ubuntu`):
#
#     curl -fsSL https://raw.githubusercontent.com/M13-Prime/Score_Simulator/main/scripts/deploy.sh | bash
#
# Or, if you've already cloned the repo:
#
#     bash scripts/deploy.sh

set -euo pipefail

REPO_URL=${REPO_URL:-https://github.com/M13-Prime/Score_Simulator.git}
BRANCH=${BRANCH:-claude/signal-tracker-system-W4FNB}
APP_DIR=${APP_DIR:-/opt/signal-tracker}

log() { printf "\n\033[36m==> %s\033[0m\n" "$*"; }
warn() { printf "\n\033[33m!!  %s\033[0m\n" "$*" >&2; }

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
log "Installing system packages (docker, git, iptables-persistent)..."
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    git curl ca-certificates \
    docker.io docker-compose-v2 \
    iptables-persistent

sudo systemctl enable --now docker

if ! groups "$USER" | grep -qw docker; then
    sudo usermod -aG docker "$USER"
    warn "Added $USER to the docker group. You MUST log out and back in (or run 'newgrp docker') before re-running this script for docker to work without sudo."
fi

# ---------------------------------------------------------------------------
# 2. Firewall — open 80/443 for HTTP/S even though Cloudflare Tunnel doesn't
#    need them; nice to have if you ever switch to direct nginx + Let's Encrypt.
#    Some hosts ship empty iptables rulesets where -I INPUT 6 fails ("Index
#    of insertion too big"). Appending with -A is safe on any host.
# ---------------------------------------------------------------------------
log "Opening firewall ports 80 / 443 (idempotent)..."
add_rule() {
    local port=$1
    if ! sudo iptables -C INPUT -m state --state NEW -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
        sudo iptables -A INPUT -m state --state NEW -p tcp --dport "$port" -j ACCEPT
    fi
}
add_rule 80
add_rule 443
sudo netfilter-persistent save >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 3. Code
# ---------------------------------------------------------------------------
log "Cloning / updating $REPO_URL @ $BRANCH into $APP_DIR..."
sudo mkdir -p "$APP_DIR"
sudo chown "$USER:$USER" "$APP_DIR"

if [ ! -d "$APP_DIR/.git" ]; then
    git clone -b "$BRANCH" "$REPO_URL" "$APP_DIR"
else
    cd "$APP_DIR"
    git fetch origin
    git checkout "$BRANCH"
    git pull --ff-only
fi
cd "$APP_DIR"

# ---------------------------------------------------------------------------
# 4. .env
# ---------------------------------------------------------------------------
if [ ! -f .env ]; then
    log "First-time setup: copying .env.example -> .env"
    cp .env.example .env
    cat <<EOF

  -----------------------------------------------------------------
  ${APP_DIR}/.env created.

  Edit it with at minimum:
    - ANTHROPIC_API_KEY=sk-ant-...
    - DASHBOARD_AUTH_USER=<your-login>
    - DASHBOARD_AUTH_PASSWORD=<a-strong-password>
    - POSTGRES_PASSWORD=<a-strong-postgres-password>
    - DIGEST_TO_EMAIL=you@example.com   (optional)
    - DIGEST_FROM_EMAIL=tracker@example.com  (optional)

  Then re-run this script. It will skip the parts already done.
  -----------------------------------------------------------------

EOF
    exit 0
fi

# ---------------------------------------------------------------------------
# 5. Build + run
# ---------------------------------------------------------------------------
log "Building image and starting services..."
if groups "$USER" | grep -qw docker; then
    docker compose build
    docker compose up -d
else
    sudo docker compose build
    sudo docker compose up -d
fi

log "Services started. Status:"
if groups "$USER" | grep -qw docker; then
    docker compose ps
else
    sudo docker compose ps
fi

cat <<EOF

  Next steps:
   1. Tail logs:           docker compose logs -f dashboard
   2. Run doctor in pod:   docker compose exec dashboard python scripts/doctor.py
   3. Install Cloudflare Tunnel (separate guide): see docs/DEPLOY.md
   4. Trigger a manual run: docker compose exec dashboard python scripts/daily.py --dry-run

EOF
