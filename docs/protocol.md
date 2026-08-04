# Snap & Tag S002 CUS protocol notes

These notes document the clean-room protocol behavior implemented by Ink 002.
They are based on observing byte streams produced for a printer owned by the
project author. No vendor binaries or extracted source are included.

## Printer geometry

- Printable width: 554 dots.
- Wire width: 576 dots, padded to 72 bytes per row.
- Bit order: black is `1`, most-significant bit first.
- Raster frame: four rows, normally 288 payload bytes.
- Supported density values: 7, 12, and 15.

## Frame shape

Outgoing CUS frames use this shape:

```text
64 CC SS LL LL [payload…] 00 00 00 00 9b
```

- `64`: frame start.
- `CC`: command byte.
- `SS`: six-bit sequence number.
- `LL LL`: little-endian payload length.
- four zero bytes: outgoing checksum/reserved field used by this printer path.
- `9b`: frame end.

The implementation uses these commands:

| Command | Payload | Meaning |
|---:|---|---|
| `0x0a` | `55` | print setup |
| `0x09` | density byte | thermal density |
| `0x00` | up to four packed rows | raster data |
| `0x02` | `c8 00` | job completion/feed |

The `0x02` command is the job-completion marker and carries a feed value in its
payload. Ink 002 matches the vendor value of `200` and exposes it as
`S002_TRAIL_FEED`. Tests on the physical printer showed that lowering this value
does not reduce the paper gap: the firmware still applies its own leading and
trailing margins (~20 mm each side). Keep the value at the vendor default.

Firmware and serial-number queries consume sequence numbers 1 and 2, so the
first job frame starts at sequence 3. Sequences wrap on six bits.

## Handshake and keepalive

The known valid queries are:

```text
firmware  64 11 01 00 00 00 00 00 00 9b
serial    64 12 02 00 00 00 00 00 00 9b
status    64 10 02 00 00 00 00 00 00 9b
```

The connection is short-lived when idle. Ink 002 sends the firmware and serial
queries before the raster payload and continues sending status queries while the
printer finishes feeding paper.

On macOS, arbitrary pauses between small RFCOMM writes caused visible white bands
and stop/start motion. The native helper therefore writes continuously using the
negotiated RFCOMM MTU by default. `S002_RFCOMM_CHUNK_DELAY` should remain zero.

## Golden fixtures

`tests/fixtures/iliad10-preview.png` is a 554×346 monochrome reference image. Its
encoded stream is exactly 25,816 bytes and contains 90 CUS frames: two 11-byte
setup frames, 86 full 298-byte raster frames, one partial raster frame, and one
12-byte completion frame.

The test suite compares the complete encoded stream with
`iliad10-appseq.bin` byte-for-byte. A second, smaller fixture verifies raster
extraction independently.

| Fixture | SHA-256 |
|---|---|
| `iliad1-test.png` | `ed05c175a3c3d6f633b0fc82615ddd2f7ad0b7782a533d6499889a06b18f6de9` |
| `iliad1-appseq.bin` | `a7c70b84ef764420312fe8ad88c611f9608912567ea2125481d20baea0ab9571` |
| `iliad10-preview.png` | `abe04d6af1aaeebdd579eae9d8a7f85e8e4d2907aa8e974a61035f5c89b6aa53` |
| `iliad10-appseq.bin` | `c4d63f0da2c51161917e32dec2f981afb010be572b2f31c908c84b10a5cf25ed` |

## Provenance boundary

The original research workspace contained a vendor APK, a decompiled source
tree, Android harnesses, and extracted native libraries. Those artifacts remain
outside this repository and are unnecessary for building, testing, installing,
or operating Ink 002.
