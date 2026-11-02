"""DRO:int4 host-side ASCII wire codec, using only the Python standard library.

Source: yadro-de/in-detail-interface-dro-int-4.html and debugging-scales.html.
Commands have no terminator; replies use CR/LF. Scale readings retain their
exact hexadecimal payload: see docs/yadro-protocol.html for packing limitations.

Use encode_command() for writes and iterate ResponseParser.feed() for received
chunks. The caller owns serial I/O and must finish one command before sending
another. This module does no I/O, waiting, threading, or automatic retries.
"""

from dataclasses import dataclass
import re
from typing import Iterator


SERIAL_SETTINGS = {
    "baudrate": 19200,
    "bytesize": 8,
    "parity": "N",
    "stopbits": 1,
    "xonxoff": False,
    "rtscts": False,
    "dsrdtr": False,
}
MAX_LINE_BYTES = 256
_COMMAND = re.compile(r"(?:v|d[0-3awr]|[rs][0-3]|[apR]|c[0-3][CD])")
_READING = re.compile(r"DRO#([0-3])([BL]):0x([0-9A-Fa-f]+)")


@dataclass(frozen=True)
class Response:
    """One reply line; payload is unmodified hex, never a scaled coordinate.

    Kinds: reading, ack, configuration, version, welcome, copyright, detect,
    clock, data, device, text, invalid. Unknown printable replies remain text.
    channel is set on reading/detect; scale_type on reading/recognized device;
    payload on reading/configuration/unknown-device bit count. error is set
    only for invalid stream records. text excludes the line terminator.
    """

    kind: str
    text: str
    channel: int | None = None
    scale_type: str | None = None
    payload: str | None = None
    error: str | None = None


def encode_command(command: str) -> bytes:
    """Validate one documented, case-sensitive command and encode without CR/LF."""
    if not isinstance(command, str):
        raise TypeError("Command must be a string")
    if _COMMAND.fullmatch(command) is None:
        raise ValueError(f"Not a documented DRO:int4 command: {command!r}")
    return command.encode("ascii")


def decode_reading_mm(response: Response) -> float:
    """Decode a complete reading, preserving the original payload on the response.

    B: six decimal digits below one flag nibble, optionally zero-padded to 32
    bits (unverified on hardware). L: two LSB-first 24-bit packets, absolute
    first and relative last, as in the captured DRO:int4 V0.2d output.
    """
    payload = response.payload or ""
    if response.kind != "reading" or re.fullmatch(r"[0-9A-Fa-f]+", payload) is None:
        raise ValueError("Expected a scale reading with a hexadecimal payload")
    if response.scale_type == "L":
        if len(payload) != 12:
            raise ValueError(f"L reading needs 12 hex digits; received {len(payload)}")
        # The controller preserves wire order; reverse the relative packet's bits
        # before interpreting its two's-complement sign. Keep all 24 bits.
        counts = int(f"{int(payload[-6:], 16):024b}"[::-1], 2)
        if counts & 0x800000:
            counts -= 0x1000000
        return counts * 25.4 / 20480
    if response.scale_type == "B":
        if len(payload) not in (7, 8) or (len(payload) == 8 and payload[0] != "0"):
            raise ValueError("B reading needs seven nibbles with optional leading zero")
        if not payload[-6:].isdigit():
            raise ValueError("B reading contains a non-decimal BCD digit")
        flags, digits = int(payload[-7], 16), int(payload[-6:])
        value = digits / 100 if flags & 4 else (digits / 1000 + (0.0005 if flags & 2 else 0)) * 25.4
        return -value if flags & 1 else value
    raise ValueError("Unsupported scale type")


def parse_response(line: bytes | bytearray) -> Response:
    """Parse one complete reply, optionally ending in CR/LF, CR, or LF.

    Raises ValueError for malformed readings, non-printable/non-ASCII input,
    empty lines, and oversized lines. Unknown printable messages are retained
    because the manual does not specify the spelling of firmware warnings.
    """
    if not isinstance(line, (bytes, bytearray)):
        raise TypeError("Response must be bytes")
    for ending in (b"\r\n", b"\r", b"\n"):
        if line.endswith(ending):
            line = line[:-len(ending)]
            break
    if not line or len(line) > MAX_LINE_BYTES:
        raise ValueError(f"Response must contain 1–{MAX_LINE_BYTES} bytes")
    if any(byte < 32 or byte > 126 for byte in line):
        raise ValueError("Response must be one printable ASCII line")
    text = line.decode("ascii")

    match = _READING.fullmatch(text)
    if match:
        channel, scale_type, payload = match.groups()
        # The manual prints 8 B digits and 10 L digits; do not invent padding.
        if len(payload) > (8 if scale_type == "B" else 12):
            raise ValueError(f"Payload too wide for a {scale_type} scale")
        return Response("reading", text, int(channel), scale_type, payload)
    if text.startswith("DRO#"):
        raise ValueError("Malformed DRO reading (expected channel 0–3, B/L, and hex payload)")

    if text == "OK":
        return Response("ack", text)
    match = re.fullmatch(r"([0-9A-Fa-f]{8}) OK", text)
    if match:
        return Response("configuration", text, payload=match[1])
    if re.fullmatch(r"(?:YADRO-DROINT v|DRO:int4 V)[0-9][0-9A-Za-z.]*", text):
        return Response("version", text)
    if text == "*** Welcome to YADRO-land! ***":
        return Response("welcome", text)
    if re.fullmatch(r"\(c\) [0-9]{4} Nick Mueller // www\.yadro\.de", text):
        return Response("copyright", text)
    match = re.fullmatch(r"Checking device #([0-3])\.\.\.", text)
    if match:
        return Response("detect", text, channel=int(match[1]))
    match = re.fullmatch(r"(Clock|Data) *: *(Signal|stuck [HL])", text)
    if match:
        return Response(match[1].lower(), text)
    for name, scale_type in ((r"7BCDs", "B"), (r"2\*24 *bits", "L")):
        if re.fullmatch(rf"Device: *{name} *(?:'{scale_type}'|\('{scale_type}'\))", text):
            return Response("device", text, scale_type=scale_type)
    match = re.fullmatch(r"Device: unknown type\. Sending 0x([0-9A-Fa-f]{2})", text)
    if match:
        return Response("device", text, payload=match[1])
    return Response("text", text)


class ResponseParser:
    """Incrementally frame replies with at most MAX_LINE_BYTES pending bytes.

    Iterate feed() fully for each serial chunk. Invalid records become events,
    so one damaged line does not hide subsequent readings from the same chunk.
    Create a new parser on reconnect; unfinished old replies must not carry over.
    """

    def __init__(self) -> None:
        self._pending = bytearray()
        self._discarding = False

    def feed(self, chunk: bytes | bytearray) -> Iterator[Response]:
        if not isinstance(chunk, (bytes, bytearray)):
            raise TypeError("Serial chunk must be bytes")
        # ponytail: byte-wise framing is ample at 19200 baud. For much faster
        # links, use bounded delimiter scans while retaining the same line cap.
        for byte in chunk:
            # CR alone also appears in the manual; ignore empty CR/LF segments.
            if byte in (13, 10):
                self._discarding = False
                if self._pending:
                    line = bytes(self._pending)
                    self._pending.clear()
                    try:
                        yield parse_response(line)
                    except ValueError as exc:
                        yield Response("invalid", line.decode("ascii", "backslashreplace"),
                                       error=str(exc))
            elif not self._discarding:
                if len(self._pending) == MAX_LINE_BYTES:
                    prefix = self._pending.decode("ascii", "backslashreplace")
                    self._pending.clear()
                    self._discarding = True
                    yield Response("invalid", prefix,
                                   error=f"Response exceeds {MAX_LINE_BYTES} bytes; discarding through delimiter")
                else:
                    self._pending.append(byte)
