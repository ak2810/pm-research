#!/usr/bin/env bash
# Idempotent installer for pm-research on Ubuntu 24.04.
# Run as root (or with sudo). Installs all services, creates user/dirs/venv.
# Expects /etc/pm-research/.env to exist (copy from .env.example, fill values).
set -euo pipefail

APP_USER=pm-research
APP_DIR=/opt/pm-research
DATA_DIR=/var/pm-research
ENV_FILE=/etc/pm-research/.env
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== pm-research installer ==="

# ── System packages ───────────────────────────────────────────────────────────
apt-get update -qq
apt-get install -y -qq python3.11 python3.11-venv python3.11-dev chrony curl

# ── User ──────────────────────────────────────────────────────────────────────
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --shell /usr/sbin/nologin --home "$APP_DIR" "$APP_USER"
    echo "Created user $APP_USER"
fi

# ── Directories ───────────────────────────────────────────────────────────────
for d in "$APP_DIR" "$DATA_DIR/data" "$DATA_DIR/state" "$DATA_DIR/logs"; do
    mkdir -p "$d"
    chown "$APP_USER:$APP_USER" "$d"
done

mkdir -p /etc/pm-research
chmod 700 /etc/pm-research

# ── App code ──────────────────────────────────────────────────────────────────
rsync -a --delete \
    --exclude='.git' \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='.mypy_cache' \
    --exclude='.ruff_cache' \
    --exclude='*.egg-info' \
    "$REPO_DIR/" "$APP_DIR/"

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ── Python virtualenv ─────────────────────────────────────────────────────────
if [[ ! -d "$APP_DIR/venv" ]]; then
    python3.11 -m venv "$APP_DIR/venv"
    echo "Created virtualenv"
fi

"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
"$APP_DIR/venv/bin/pip" install --quiet -e "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/venv"
echo "Dependencies installed"

# ── Environment file ──────────────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
    cp "$APP_DIR/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    chown root:root "$ENV_FILE"
    echo "Created $ENV_FILE — FILL IN VALUES BEFORE STARTING SERVICES"
fi

# ── Systemd units ─────────────────────────────────────────────────────────────
for unit_src in "$APP_DIR/systemd/"*.service; do
    unit_name="$(basename "$unit_src")"
    install -m644 "$unit_src" "/etc/systemd/system/$unit_name"
    echo "Installed $unit_name"
done

systemctl daemon-reload

for svc in pm-clob-collector binance-collector polygon-indexer \
           pm-metadata-snapshotter wallet-attribution pipeline-rotator \
           heartbeat-watchdog; do
    systemctl enable "$svc"
done

echo ""
echo "=== Installation complete ==="
echo "1. Edit $ENV_FILE with your credentials"
echo "2. Start services: sudo systemctl start pm-clob-collector binance-collector polygon-indexer pm-metadata-snapshotter wallet-attribution pipeline-rotator heartbeat-watchdog"
echo "3. Check status:   sudo systemctl status 'pm-*' binance-collector polygon-indexer"
