#!/usr/bin/env bash
# One-shot installer for Ubuntu 22.04 / 24.04 (ARM64 or x86_64).
# Run as root or with sudo on a fresh Oracle Cloud free-tier instance:
#
#     curl -fsSL https://raw.githubusercontent.com/MaliciousMusic/pam-trading-bot/main/deploy/install.sh | sudo bash
#
# Re-running is safe — it updates the repo to origin/main and restarts the service.

set -euo pipefail

REPO_URL="https://github.com/MaliciousMusic/pam-trading-bot.git"
BOT_USER="pam"
BOT_HOME="/home/${BOT_USER}"
REPO_DIR="${BOT_HOME}/pam-trading-bot"
VENV_DIR="${REPO_DIR}/.venv"
SERVICE_NAME="pam-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

echo "==> apt: base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates

echo "==> user: ${BOT_USER}"
if ! id -u "${BOT_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/bash "${BOT_USER}"
fi

echo "==> repo: ${REPO_DIR}"
if [[ -d "${REPO_DIR}/.git" ]]; then
  sudo -u "${BOT_USER}" git -C "${REPO_DIR}" fetch --quiet origin
  sudo -u "${BOT_USER}" git -C "${REPO_DIR}" reset --quiet --hard origin/main
else
  sudo -u "${BOT_USER}" git clone --quiet "${REPO_URL}" "${REPO_DIR}"
fi

echo "==> venv + deps"
if [[ ! -d "${VENV_DIR}" ]]; then
  sudo -u "${BOT_USER}" python3 -m venv "${VENV_DIR}"
fi
sudo -u "${BOT_USER}" "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
sudo -u "${BOT_USER}" "${VENV_DIR}/bin/pip" install --quiet -r "${REPO_DIR}/requirements.txt"

echo "==> systemd unit"
install -m 644 "${REPO_DIR}/deploy/pam-bot.service" "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null
systemctl restart "${SERVICE_NAME}"

sleep 2
systemctl --no-pager --full status "${SERVICE_NAME}" | head -12

cat <<EOF

==> installed. useful commands:
    journalctl -u ${SERVICE_NAME} -f        # live logs
    systemctl restart ${SERVICE_NAME}       # restart after pulling changes
    sudo bash ${REPO_DIR}/deploy/install.sh # re-run to pull + restart

==> access the UI from your laptop via SSH tunnel (recommended):
    ssh -L 8765:localhost:8765 ubuntu@<instance-ip>
    then open http://localhost:8765 in your browser

EOF
