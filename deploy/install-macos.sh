#!/bin/zsh
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_ROOT="$HOME/Library/Application Support/Ink002"
APP_DIR="$INSTALL_ROOT/app"
VENV_DIR="$INSTALL_ROOT/venv"
DATA_DIR="$INSTALL_ROOT/data"
LOG_DIR="$HOME/Library/Logs/Ink002"
PLIST="$HOME/Library/LaunchAgents/com.tristan.ink002.plist"
LABEL="com.tristan.ink002"

if ! command -v blueutil >/dev/null 2>&1; then
  echo "blueutil is missing. Install it first with: brew install blueutil" >&2
  exit 1
fi

mkdir -p "$APP_DIR" "$DATA_DIR" "$LOG_DIR" "$(dirname "$PLIST")"
rsync -a \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude 'data' \
  "$SOURCE_DIR/" "$APP_DIR/"

mkdir -p "$APP_DIR/bin"
xcrun clang -fobjc-arc \
  -framework Foundation \
  -framework IOBluetooth \
  -o "$APP_DIR/bin/s002-rfcomm" \
  "$APP_DIR/native/macos_rfcomm.m"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 "$VENV_DIR"
  else
    python3 -m venv "$VENV_DIR"
  fi
fi

if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$VENV_DIR/bin/python" -r "$APP_DIR/requirements.txt"
else
  "$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
fi

sed \
  -e "s|__APP_DIR__|$APP_DIR|g" \
  -e "s|__VENV_DIR__|$VENV_DIR|g" \
  -e "s|__DATA_DIR__|$DATA_DIR|g" \
  -e "s|__LOG_DIR__|$LOG_DIR|g" \
  "$SOURCE_DIR/deploy/macos/com.tristan.ink002.plist.template" > "$PLIST"

launchctl bootout "gui/$UID" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl enable "gui/$UID/$LABEL"
launchctl kickstart -k "gui/$UID/$LABEL"

echo "Ink / 002 is installed and starts automatically with your Mac."
echo "Open: http://127.0.0.1:8092"
echo "Logs: $LOG_DIR"
open "http://127.0.0.1:8092"
