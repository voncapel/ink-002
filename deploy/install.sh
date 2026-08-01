#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer with sudo." >&2
  exit 1
fi

SOURCE_DIR=${1:-}
if [[ -z ${SOURCE_DIR} || ! -f ${SOURCE_DIR}/app.py ]]; then
  echo "Usage: sudo ./deploy/install.sh /path/to/s002_web" >&2
  exit 1
fi

apt-get update
apt-get install -y bluez python3 python3-venv fonts-dejavu-core

install -d -o tristan -g tristan /opt/s002-web /var/lib/s002-web/jobs
cp -R "${SOURCE_DIR}/." /opt/s002-web/
chown -R tristan:tristan /opt/s002-web /var/lib/s002-web

python3 -m venv /opt/s002-web/.venv
/opt/s002-web/.venv/bin/python -m pip install --upgrade pip
/opt/s002-web/.venv/bin/python -m pip install -r /opt/s002-web/requirements.txt

if [[ ! -f /etc/s002-web.env ]]; then
  install -m 600 /opt/s002-web/deploy/s002-web.env.example /etc/s002-web.env
  echo "Created /etc/s002-web.env; replace its web password before starting the service."
fi

install -m 644 /opt/s002-web/deploy/s002-web.service /etc/systemd/system/s002-web.service
systemctl daemon-reload
systemctl enable s002-web.service

echo "Installed. Pair the S002, edit /etc/s002-web.env, then run:"
echo "  sudo systemctl start s002-web"
