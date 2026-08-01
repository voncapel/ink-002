# Architecture

Ink 002 is deliberately small: one Flask process, one in-memory queue, one print
worker, and one physical printer.

```text
Browser
  │  multipart form / JSON status
  ▼
Flask API ──► renderer ──► 554-dot monochrome PNG
  │                            │
  │ enqueue                    ▼
  └────────────────────► print worker ──► CUS encoder
                                              │
                          ┌───────────────────┴───────────────────┐
                          ▼                                       ▼
                 macOS IOBluetooth                         Linux BlueZ
                 native helper                             RFCOMM socket
                          └───────────────────┬───────────────────┘
                                              ▼
                                           S002
```

Parcel labels take a separate, deliberately split path:

```text
PDF/image ──► first-page preview ──► OpenRouter vision geometry
                                         │
                                         ▼
                         local frame snap + critical-region checks
                                         │
                                         ▼
                         full-resolution crop + safe cut optimizer
                                         │
                                         ▼
                              554-dot continuous print roll
```

The model never produces the printable raster. It only proposes normalized
geometry; the crop, rotation, cut search, full-resolution PDF render, threshold,
and roll assembly are deterministic local operations in `parcel.py`.

## Web and API layer

`app.py` owns the Flask routes, optional Basic authentication, bounded job
metadata, and the single background worker. `POST /api/jobs` renders the input
before enqueueing it, so the worker only handles an immutable PNG and printer
parameters. A single Gunicorn worker is required because the queue lives in
process memory and two workers could write to the printer concurrently.

The browser performs image adjustments and dithering locally for immediate
feedback. The submitted values are applied again by Pillow in `rendering.py`;
the server-rendered PNG, not the browser canvas, is the authoritative print
input.

## Rendering layer

`rendering.py` converts every source to an `L` image exactly 554 pixels wide.
Text is wrapped with a Unicode TrueType font. Images are composited on white,
autocontrasted, resized to a 518-pixel content area, adjusted, dithered to strict
black/white, and centered with 18-dot margins. PDF pages use PyMuPDF and are
stacked vertically.

## Protocol layer

`s002_protocol.py` has no platform dependencies in its encoder. It packs black
pixels MSB-first into 72-byte rows, groups four rows per CUS raster frame, and
adds the setup and completion commands. This separation lets the golden tests
validate the byte stream without Bluetooth hardware.

## Transport layer

- **macOS:** `native/macos_rfcomm.m` opens RFCOMM channel 1 through Apple's
  IOBluetooth framework. The installer compiles it as `bin/s002-rfcomm`.
- **Linux:** Python opens a BlueZ RFCOMM socket directly.
- **Legacy diagnostic:** `/dev/cu.S002` remains available as `macos_serial`, but
  it is not the recommended path for raster jobs.

Both native transports send firmware and serial-number queries before the job,
then valid status queries while the thermal mechanism drains its buffer.

## Runtime data

Rendered jobs are transient files beneath `S002_DATA_DIR`. They are bounded by
`MAX_HISTORY` in memory, excluded from Git, and not exposed as a history view in
the UI. Parcel previews and rolls are separately bounded to eight active
analyses. Restarting the process clears all metadata and deletes both job and
parcel PNGs because they can contain private document data.
