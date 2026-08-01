from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import s002_protocol


def test_transport_aliases() -> None:
    assert s002_protocol.resolve_transport("mac") == "macos_rfcomm"
    assert s002_protocol.resolve_transport("serial") == "macos_serial"
    assert s002_protocol.resolve_transport("linux") == "linux_rfcomm"
    with pytest.raises(ValueError, match="unsupported"):
        s002_protocol.resolve_transport("carrier-pigeon")


def test_macos_serial_preserves_vendor_handshake_and_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[bytes] = []

    class FakeSerial:
        in_waiting = 0

        def __init__(self, *_args, **_kwargs):
            self.rts = False
            self.dtr = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def write(self, block: bytes) -> int:
            writes.append(block)
            return len(block)

        def read(self, _count: int) -> bytes:
            return b""

    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=FakeSerial))
    monkeypatch.setattr(s002_protocol.time, "sleep", lambda _seconds: None)
    ticks = iter((0.0, 0.0, 1.0, 1.0))
    monkeypatch.setattr(s002_protocol.time, "monotonic", lambda: next(ticks))

    payload = b"abcdef"
    result = s002_protocol._send_macos_serial_once(
        payload,
        port="/dev/cu.S002",
        baud=115200,
        chunk_size=2,
        chunk_delay=0,
        keepalive_seconds=0,
        write_timeout=20,
    )

    assert writes == [
        s002_protocol.FIRMWARE_QUERY,
        s002_protocol.SERIAL_QUERY,
        b"ab",
        b"cd",
        b"ef",
    ]
    assert result.sent_bytes == len(payload)
