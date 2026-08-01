# Ink 002

Ink 002 is a local-first web application for the Snap & Tag S002 Bluetooth
thermal printer. It renders text, images, PDFs, and UTF-8 text files at the
printer's native 554-dot width, queues jobs, encodes the proprietary CUS raster
protocol, and sends it over native RFCOMM on macOS or Linux.

The project is a clean-room implementation. It does not bundle the vendor APK,
SDK, native Android library, or any extracted proprietary source.

## Features

- Dark Metro-inspired responsive interface.
- Unicode text with size and alignment controls.
- Large live 1-bit image preview.
- Contrast, brightness, and sharpness adjustments.
- Five monochrome treatments: threshold, Floyd–Steinberg, Atkinson, Bayer 4×4,
  and Bayer 8×8.
- PNG, JPEG, WebP, BMP, GIF, TIFF, PDF, and UTF-8 text input.
- Single-worker queue to prevent concurrent writes to the printer.
- Native macOS IOBluetooth helper; no Android device or home server required.
- Native Linux/BlueZ RFCOMM transport for a permanent home relay.
- Optional HTTP Basic authentication for remotely exposed installations.
- AI-assisted parcel-label isolation with full-scale strip tiling: a vision
  model finds the useful carrier label and protected regions, then a local
  algorithm chooses safe cuts and produces an exact 554-dot roll.

The **MTG** workspace is intentionally a placeholder for a future proxy-printing
workflow.

## Requirements

- Python 3.11 or newer.
- A paired Snap & Tag S002 printer.
- macOS: Xcode Command Line Tools and `blueutil`.
- Linux: BlueZ and a Python build with Bluetooth socket support.

The known S002 configuration uses RFCOMM channel `1`, a 554-dot print area, and
density values `7`, `12`, or `15`.

## Quick start for development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/flask --app app run --port 8092
```

Open <http://127.0.0.1:8092>.

To enable the **COLIS** workspace, copy `.env.example` to `.env` and add an
OpenRouter key. The configured default model is `google/gemma-4-31b-it`.
`.env` is ignored by Git and should remain mode `600` on shared machines.

`S002_TRANSPORT=auto` selects the native IOBluetooth helper on macOS and BlueZ
RFCOMM on Linux. The macOS helper is compiled by the installer; running the Flask
development server directly can render and queue jobs but cannot print until the
helper exists at `bin/s002-rfcomm`.

## Portable macOS installation

Pair the printer once in macOS Bluetooth settings, then install `blueutil` and
run the installer:

```bash
brew install blueutil
./deploy/install-macos.sh
```

The installer:

1. copies the application to `~/Library/Application Support/Ink002/app`;
2. compiles `native/macos_rfcomm.m` against Apple's IOBluetooth framework;
3. creates an isolated Python environment;
4. installs a LaunchAgent that starts the local service at login; and
5. opens <http://127.0.0.1:8092>.

Runtime data and logs remain outside the repository:

- data: `~/Library/Application Support/Ink002/data`
- logs: `~/Library/Logs/Ink002`
- LaunchAgent: `~/Library/LaunchAgents/com.tristan.ink002.plist`

Re-run the installer after source changes. To use the web app like a native app,
add the local page to the Dock from Safari.

## Linux home relay

Pair and trust the S002 with BlueZ, then install the service from the repository
directory:

```bash
sudo ./deploy/install.sh "$PWD"
sudoedit /etc/s002-web.env
sudo systemctl start s002-web
systemctl status s002-web
```

The supplied service runs one Gunicorn worker, binds to `127.0.0.1:8092`, stores
job previews under `/var/lib/s002-web`, and allows Bluetooth sockets while keeping
the rest of the systemd sandbox restrictive.

The Linux installer and service currently expect a local user and group named
`tristan`. Change `deploy/install.sh` and `deploy/s002-web.service` before using a
different account.

## Remote access

The app deliberately binds only to localhost. A Cloudflare Tunnel or another
authenticated reverse proxy can expose it without opening a router port. Copy
`deploy/cloudflared.yml.example`, replace its placeholders, and keep the live
tunnel file outside Git.

Always set `S002_WEB_USER` and `S002_WEB_PASSWORD` in the service environment.
For a public deployment, protect the hostname with Cloudflare Access as an
additional layer. See [SECURITY.md](SECURITY.md).

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `S002_MAC` | `06:03:86:00:97:AB` | Printer Bluetooth address |
| `S002_CHANNEL` | `1` | RFCOMM channel |
| `S002_TRANSPORT` | `auto` | `macos_rfcomm`, `linux_rfcomm`, or diagnostic `macos_serial` |
| `S002_PORT` | `/dev/cu.S002` | Legacy macOS serial device |
| `S002_BAUD` | `115200` | Legacy serial baud rate |
| `S002_SERIAL_CHUNK_SIZE` | `64` | Legacy serial write size |
| `S002_SERIAL_CHUNK_DELAY` | `0.05` | Legacy serial pacing |
| `S002_RFCOMM_CHUNK_SIZE` | `0` | Native macOS write size; `0` uses the negotiated MTU |
| `S002_RFCOMM_CHUNK_DELAY` | `0` | Native macOS pacing; keep at zero for continuous output |
| `S002_DATA_DIR` | `./data` | Runtime preview and transient job directory |
| `S002_FONT_PATH` | auto-detected | Unicode TrueType font |
| `S002_WEB_USER` | empty | Optional HTTP Basic username |
| `S002_WEB_PASSWORD` | empty | Enables HTTP Basic authentication when non-empty |
| `OPENROUTER_API_KEY` | empty | Enables vision-assisted parcel-label detection |
| `OPENROUTER_MODEL` | `google/gemma-4-31b-it` | OpenRouter vision model slug |
| `PORT` | `8092` | Development-server port |

## Tests

```bash
.venv/bin/pytest -q
node --check static/app.js
```

The protocol suite includes two small golden fixtures under `tests/fixtures`.
The full Iliad job must match the captured vendor payload byte-for-byte, which
protects the CUS frame layout, sequence numbers, raster packing, and completion
command from regressions. Tests never connect to or print from the S002.

## Repository layout

```text
app.py                  Flask app, queue, API, and worker
parcel.py               vision analysis, local validation, and strip tiling
rendering.py            text/document rendering and image processing
s002_protocol.py        CUS encoder and platform transports
native/macos_rfcomm.m   native macOS RFCOMM helper
templates/              web interface
static/                 Metro UI styles and live preview logic
deploy/                 macOS, systemd, and tunnel templates
tests/                  protocol, transport, and rendering tests
docs/                   architecture and protocol notes
```

For implementation details, read [Architecture](docs/architecture.md) and
[CUS protocol notes](docs/protocol.md).

## Current limitations

- One S002 printer per process.
- Print history is transient and intentionally hidden from the interface.
- PDF jobs are limited to 20 pages and all jobs to 30,000 raster rows.
- Parcel analysis currently uses only the first PDF page. A downsampled PNG of
  that page is sent to OpenRouter; crop snapping, cut validation, rasterization,
  and print-roll generation remain local.
- The native macOS helper uses the deprecated IOBluetooth framework because the
  printer exposes Bluetooth Classic SPP rather than BLE.
- The MTG proxy composer is not implemented yet.
