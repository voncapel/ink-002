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
- Manual parcel-label workshop with a full-page preview, draggable crop frame,
  linked or independent cut lines, and exact 554-dot roll generation. Printed
  strips use one scale calculated from the tallest source strip, giving every
  printed strip the same fitted feed length without trailing white padding.
  Optional AI calculation is available only when explicitly requested.
- MTG proxy composer: paste a decklist and resolve localized card data through
  Scryfall. Print either the complete card image or a compact text-first proxy
  with mana glyphs and optional artwork. Both formats use exact 554-dot live
  previews and are stacked into automatically split printable batches.

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

The **RESIZE** manual editor works without any API key. To enable its optional
**Auto calcul** button, copy `.env.example` to `.env` and add an OpenRouter key.
The configured model is `google/gemma-4-31b-it`. `.env` is ignored by Git and
should remain mode `600` on shared machines.

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
| `MTG_MAX_BATCH_HEIGHT` | `3000` | Max raster rows per MTG print lot (keeps each lot small enough for the S002 buffer) |
| `MTG_MAX_BATCH_BYTES` | `200000` | Max encoded size per MTG print lot |
| `MTG_FONT_PATH` | bundled Atkinson Hyperlegible Next Medium | Optional text font override for compact MTG proxies |
| `MTG_FONT_BOLD_PATH` | bundled Atkinson Hyperlegible Bold | Optional bold text font override for compact MTG proxies |
| `MTG_SYMBOL_FONT_PATH` | user Mana font | Optional path to the [Mana symbol font](https://github.com/andrewgioia/mana) |

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
mtg.py                  Scryfall lookup and MTG card-roll preparation
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
- The parcel editor currently uses only the first PDF page. Upload, manual crop,
  cut editing, rasterization, and print-roll generation remain local. A
  downsampled preview is sent to OpenRouter only after an explicit click on
  **Auto calcul**.
- The native macOS helper uses the deprecated IOBluetooth framework because the
  printer exposes Bluetooth Classic SPP rather than BLE.
- The MTG composer resolves cards through the public Scryfall API. The
  mtgdecks.net page is Cloudflare-blocked, so the app takes a pasted decklist
  instead of scraping that site.
