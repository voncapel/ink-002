"""Pure-Python encoder and Linux/macOS transports for the Snap & Tag S002."""

from __future__ import annotations

import select
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from PIL import Image

PRINT_WIDTH = 554
WIRE_WIDTH = 576
BYTES_PER_ROW = WIRE_WIDTH // 8
ROWS_PER_FRAME = 4

FIRMWARE_QUERY = bytes.fromhex("64 11 01 00 00 00 00 00 00 9b")
SERIAL_QUERY = bytes.fromhex("64 12 02 00 00 00 00 00 00 9b")
STATUS_QUERY = bytes.fromhex("64 10 02 00 00 00 00 00 00 9b")
CANCEL_QUERY = bytes.fromhex("64 52 02 01 00 00 00 00 00 00 9b")


class PrintCancelled(RuntimeError):
    """Raised when the current physical print has been cancelled by the operator."""


def _raise_if_cancelled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise PrintCancelled("print cancelled by operator")


def _wait_or_cancel(cancel_event: Event | None, seconds: float) -> None:
    if cancel_event is None:
        time.sleep(seconds)
    elif cancel_event.wait(seconds):
        raise PrintCancelled("print cancelled by operator")


def cus_frame(command: int, sequence: int, payload: bytes) -> bytes:
    """Build the CUS frame used by the S002 (outgoing checksums are zero)."""
    if not 0 <= command <= 0xFF:
        raise ValueError("command must fit in one byte")
    if len(payload) > 0xFFFF:
        raise ValueError("CUS payload is too large")
    return (
        bytes((0x64, command, sequence & 0x3F))
        + len(payload).to_bytes(2, "little")
        + payload
        + b"\x00\x00\x00\x00\x9b"
    )


def raster_bytes(image: Image.Image, threshold: int = 85) -> bytes:
    """Pack a 554-pixel image as 576-dot, black-is-one, MSB-first rows."""
    if image.width != PRINT_WIDTH:
        raise ValueError(f"image width must be {PRINT_WIDTH}, got {image.width}")
    if not 0 <= threshold <= 255:
        raise ValueError("threshold must be between 0 and 255")

    gray = image.convert("L")
    pixels = gray.load()
    packed = bytearray(image.height * BYTES_PER_ROW)
    offset = 0
    for y in range(image.height):
        for byte_x in range(BYTES_PER_ROW):
            value = 0
            start_x = byte_x * 8
            for bit in range(8):
                x = start_x + bit
                if x < PRINT_WIDTH and pixels[x, y] < threshold:
                    value |= 1 << (7 - bit)
            packed[offset] = value
            offset += 1
    return bytes(packed)


def encode_print_job(
    image: Image.Image,
    *,
    density: int = 7,
    speed: int = 95,
    threshold: int = 85,
    start_sequence: int = 3,
    trailing_feed: int = 200,
) -> bytes:
    """Encode an image using the exact native S002 continuous-paper job shape."""
    if density not in (7, 12, 15):
        raise ValueError("S002 density must be 7, 12, or 15")
    if not 1 <= speed <= 255:
        raise ValueError("S002 speed must be between 1 and 255")
    if not 0 <= trailing_feed <= 0xFFFF:
        raise ValueError("S002 trailing feed must be between 0 and 65535")
    if image.height < 1:
        raise ValueError("cannot print an empty image")

    sequence = start_sequence & 0x3F
    frames = [cus_frame(0x0A, sequence, bytes((speed,)))]
    sequence = (sequence + 1) & 0x3F
    frames.append(cus_frame(0x09, sequence, bytes((density,))))
    sequence = (sequence + 1) & 0x3F

    raster = raster_bytes(image, threshold)
    chunk_size = ROWS_PER_FRAME * BYTES_PER_ROW
    for offset in range(0, len(raster), chunk_size):
        frames.append(cus_frame(0x00, sequence, raster[offset : offset + chunk_size]))
        sequence = (sequence + 1) & 0x3F

    frames.append(cus_frame(0x02, sequence, trailing_feed.to_bytes(2, "little")))
    return b"".join(frames)


@dataclass(frozen=True)
class PrintResult:
    sent_bytes: int
    reply: bytes
    elapsed_seconds: float


def resolve_transport(transport: str = "auto") -> str:
    """Resolve the portable transport name without importing platform SDKs."""
    normalized = transport.strip().lower().replace("-", "_")
    if normalized == "auto":
        if sys.platform == "darwin":
            return "macos_rfcomm"
        if sys.platform.startswith("linux"):
            return "linux_rfcomm"
        raise RuntimeError(f"no automatic S002 transport for {sys.platform}")
    aliases = {
        "mac": "macos_rfcomm",
        "macos": "macos_rfcomm",
        "native": "macos_rfcomm",
        "macos_rfcomm": "macos_rfcomm",
        "serial": "macos_serial",
        "macos_serial": "macos_serial",
        "linux": "linux_rfcomm",
        "rfcomm": "linux_rfcomm",
        "linux_rfcomm": "linux_rfcomm",
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported S002 transport: {transport}") from exc


def _send_linux_rfcomm(
    payload: bytes,
    *,
    mac: str,
    channel: int = 1,
    response_seconds: float = 8.0,
    cancel_event: Event | None = None,
) -> PrintResult:
    """Send a prepared job through BlueZ's native RFCOMM socket."""
    if not hasattr(socket, "AF_BLUETOOTH"):
        raise RuntimeError("this Python build has no Linux Bluetooth socket support")

    started = time.monotonic()
    replies = bytearray()
    connection = socket.socket(
        socket.AF_BLUETOOTH,
        socket.SOCK_STREAM,
        socket.BTPROTO_RFCOMM,
    )
    connection.settimeout(15)
    try:
        _raise_if_cancelled(cancel_event)
        connection.connect((mac, channel))
        connection.settimeout(0.25)

        def send_cancelable(block: bytes) -> None:
            pending = memoryview(block)
            while pending:
                _raise_if_cancelled(cancel_event)
                try:
                    sent = connection.send(pending[:4096])
                except TimeoutError:
                    continue
                if sent <= 0:
                    raise OSError("RFCOMM socket closed during write")
                pending = pending[sent:]

        send_cancelable(FIRMWARE_QUERY)
        _wait_or_cancel(cancel_event, 0.15)
        send_cancelable(SERIAL_QUERY)
        _wait_or_cancel(cancel_event, 0.25)
        send_cancelable(payload)

        connection.setblocking(False)
        deadline = time.monotonic() + response_seconds
        next_status = 0.0
        while time.monotonic() < deadline:
            _raise_if_cancelled(cancel_event)
            now = time.monotonic()
            if now >= next_status:
                connection.sendall(STATUS_QUERY)
                next_status = now + 0.75
            readable, _, _ = select.select([connection], [], [], 0.1)
            if readable:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                replies.extend(chunk)
    except PrintCancelled:
        try:
            connection.setblocking(True)
            connection.settimeout(0.5)
            connection.sendall(CANCEL_QUERY)
        except OSError:
            pass
        raise
    finally:
        connection.close()

    return PrintResult(
        sent_bytes=len(payload),
        reply=bytes(replies),
        elapsed_seconds=time.monotonic() - started,
    )


def _blueutil(mac: str, action: str, *, timeout: float) -> str:
    executable = shutil.which("blueutil")
    if executable is None:
        raise RuntimeError("blueutil is required on macOS; install it with: brew install blueutil")
    address = mac.replace(":", "-").lower()
    command = [executable, action, address]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"blueutil {action}: {detail}")
    return result.stdout.strip()


def _mac_is_connected(mac: str) -> bool:
    return _blueutil(mac, "--is-connected", timeout=5.0) == "1"


def _connect_macos(mac: str, port: str, timeout: float) -> None:
    if not _mac_is_connected(mac):
        _blueutil(mac, "--connect", timeout=timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _mac_is_connected(mac) and Path(port).exists():
            return
        time.sleep(0.15)
    raise TimeoutError(
        f"S002 did not expose {port} within {timeout:g}s; "
        "make sure it is powered on and paired with this Mac"
    )


class MacSerialWriteError(RuntimeError):
    """Serial failure carrying how much raster data may already have printed."""

    def __init__(self, message: str, payload_bytes_sent: int = 0):
        super().__init__(message)
        self.payload_bytes_sent = payload_bytes_sent


def _send_macos_serial_once(
    payload: bytes,
    *,
    port: str,
    baud: int,
    chunk_size: int,
    chunk_delay: float,
    keepalive_seconds: float,
    write_timeout: float,
    cancel_event: Event | None = None,
) -> PrintResult:
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError("macOS serial printing requires pyserial") from exc

    started = time.monotonic()
    replies = bytearray()
    payload_sent = 0
    try:
        with serial.Serial(port, baud, timeout=0, write_timeout=write_timeout) as connection:
            connection.rts = True
            connection.dtr = True

            def write_all(block: bytes) -> None:
                written = connection.write(block)
                if written != len(block):
                    raise OSError(f"short serial write: {written}/{len(block)} bytes")

            def read_available() -> None:
                waiting = connection.in_waiting
                if waiting:
                    replies.extend(connection.read(waiting))

            def check_cancelled() -> None:
                if cancel_event is not None and cancel_event.is_set():
                    try:
                        write_all(CANCEL_QUERY)
                    except OSError:
                        pass
                    raise PrintCancelled("print cancelled by operator")

            # This mirrors the vendor app handshake. It also prevents the Mac's
            # unusually short idle SPP timeout from closing the port at startup.
            check_cancelled()
            write_all(FIRMWARE_QUERY)
            _wait_or_cancel(cancel_event, 0.10)
            write_all(SERIAL_QUERY)
            _wait_or_cancel(cancel_event, 0.20)
            read_available()

            while payload_sent < len(payload):
                check_cancelled()
                end = min(payload_sent + chunk_size, len(payload))
                write_all(payload[payload_sent:end])
                payload_sent = end
                read_available()
                if chunk_delay:
                    _wait_or_cancel(cancel_event, chunk_delay)

            # The S002 can still be draining its tiny receive buffer after the
            # last raster frame. Valid status frames keep macOS SPP alive while
            # the thermal head finishes the job.
            deadline = time.monotonic() + keepalive_seconds
            while time.monotonic() < deadline:
                check_cancelled()
                write_all(STATUS_QUERY)
                _wait_or_cancel(cancel_event, 0.15)
                read_available()
    except PrintCancelled:
        raise
    except Exception as exc:
        raise MacSerialWriteError(
            f"macOS serial link failed after {payload_sent}/{len(payload)} payload bytes: {exc}",
            payload_bytes_sent=payload_sent,
        ) from exc

    return PrintResult(
        sent_bytes=len(payload),
        reply=bytes(replies),
        elapsed_seconds=time.monotonic() - started,
    )


def _send_macos_serial(
    payload: bytes,
    *,
    mac: str,
    port: str,
    baud: int,
    connect_timeout: float,
    chunk_size: int,
    chunk_delay: float,
    keepalive_seconds: float,
    write_timeout: float,
    connect_attempts: int,
    cancel_event: Event | None = None,
) -> PrintResult:
    last_error: Exception | None = None
    for attempt in range(max(1, connect_attempts)):
        try:
            _connect_macos(mac, port, connect_timeout)
            return _send_macos_serial_once(
                payload,
                port=port,
                baud=baud,
                chunk_size=chunk_size,
                chunk_delay=chunk_delay,
                keepalive_seconds=keepalive_seconds,
                write_timeout=write_timeout,
                cancel_event=cancel_event,
            )
        except MacSerialWriteError as exc:
            # Replaying a partially transmitted job could waste paper or print
            # it twice. Only reconnect automatically before raster bytes leave.
            if exc.payload_bytes_sent:
                raise
            last_error = exc
        except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
            last_error = exc
        if attempt + 1 < max(1, connect_attempts):
            time.sleep(0.6)
    assert last_error is not None
    raise RuntimeError(
        f"could not connect to S002 after {max(1, connect_attempts)} attempts: {last_error}"
    ) from last_error


def _send_macos_rfcomm(
    payload: bytes,
    *,
    mac: str,
    channel: int,
    helper: str,
    keepalive_seconds: float,
    chunk_size: int,
    chunk_delay: float,
    connect_timeout: float,
    cancel_event: Event | None = None,
) -> PrintResult:
    helper_path = Path(helper)
    if not helper_path.is_file():
        raise RuntimeError(
            f"native macOS RFCOMM helper is missing: {helper_path}; "
            "re-run deploy/install-macos.sh"
        )
    started = time.monotonic()
    _raise_if_cancelled(cancel_event)
    timeout = max(connect_timeout + keepalive_seconds + 90.0, 120.0)
    process = subprocess.Popen(
        [
            str(helper_path),
            mac,
            str(channel),
            str(keepalive_seconds),
            str(chunk_size),
            str(chunk_delay),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    communicate_started = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                stdout, stderr = process.communicate(timeout=3)
                raise PrintCancelled("print cancelled by operator")
            if time.monotonic() >= deadline:
                process.kill()
                process.communicate()
                raise TimeoutError("native macOS RFCOMM print timed out")
            try:
                stdout, stderr = process.communicate(
                    input=None if communicate_started else payload,
                    timeout=0.1,
                )
                break
            except subprocess.TimeoutExpired:
                communicate_started = True
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    if process.returncode == 70:
        raise PrintCancelled("print cancelled by operator")
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"native RFCOMM helper exited {process.returncode}")
    return PrintResult(
        sent_bytes=len(payload),
        reply=stdout,
        elapsed_seconds=time.monotonic() - started,
    )


def send_print_job(
    payload: bytes,
    *,
    mac: str,
    channel: int = 1,
    transport: str = "auto",
    port: str = "/dev/cu.S002",
    baud: int = 115200,
    response_seconds: float = 8.0,
    connect_timeout: float = 12.0,
    chunk_size: int = 64,
    chunk_delay: float = 0.05,
    write_timeout: float = 20.0,
    connect_attempts: int = 2,
    native_helper: str | None = None,
    cancel_event: Event | None = None,
) -> PrintResult:
    """Send a prepared job with the platform's native S002 transport."""
    selected = resolve_transport(transport)
    if selected == "linux_rfcomm":
        return _send_linux_rfcomm(
            payload,
            mac=mac,
            channel=channel,
            response_seconds=response_seconds,
            cancel_event=cancel_event,
        )
    if selected == "macos_rfcomm":
        return _send_macos_rfcomm(
            payload,
            mac=mac,
            channel=channel,
            helper=native_helper or str(Path(__file__).resolve().parent / "bin" / "s002-rfcomm"),
            keepalive_seconds=response_seconds,
            chunk_size=chunk_size,
            chunk_delay=chunk_delay,
            connect_timeout=connect_timeout,
            cancel_event=cancel_event,
        )
    return _send_macos_serial(
        payload,
        mac=mac,
        port=port,
        baud=baud,
        connect_timeout=connect_timeout,
        chunk_size=chunk_size,
        chunk_delay=chunk_delay,
        keepalive_seconds=response_seconds,
        write_timeout=write_timeout,
        connect_attempts=connect_attempts,
        cancel_event=cancel_event,
    )
