"""Schema-free protobuf wire-format walker (ported from the parent project).

betPawa publishes no .proto. Wire type 2 (length-delimited) is ambiguous —
UTF-8 string or nested message — so the walker returns raw bytes and the
caller decides per known field via as_str/as_msg/as_f64, never guessing.
Odds and implied probabilities are little-endian IEEE-754 doubles (wire 1).
"""

import struct

Field = tuple[int, int, int | bytes]  # (field_no, wire_type, value)


class DecodeError(ValueError):
    pass


def read_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if i >= len(buf):
            raise DecodeError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise DecodeError("varint too long")


def walk(buf: bytes) -> list[Field]:
    """Decode one message level into (field_no, wire_type, value) triples."""
    out: list[Field] = []
    i = 0
    while i < len(buf):
        key, i = read_varint(buf, i)
        field_no, wire = key >> 3, key & 7
        if field_no == 0:
            raise DecodeError(f"field 0 at byte {i}")
        if wire == 0:
            v, i = read_varint(buf, i)
        elif wire == 1:
            v, i = buf[i:i + 8], i + 8
        elif wire == 2:
            ln, i = read_varint(buf, i)
            if i + ln > len(buf):
                raise DecodeError(f"length {ln} overruns buffer at byte {i}")
            v, i = buf[i:i + ln], i + ln
        elif wire == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            raise DecodeError(f"unsupported wire type {wire} at byte {i}")
        out.append((field_no, wire, v))
    return out


def fields(msg: list[Field], n: int) -> list[int | bytes]:
    return [v for f, _, v in msg if f == n]


def field(msg: list[Field], n: int) -> int | bytes | None:
    xs = fields(msg, n)
    return xs[0] if xs else None


def as_str(v: int | bytes | None) -> str | None:
    return v.decode("utf-8") if isinstance(v, bytes) else None


def as_msg(v: int | bytes | None) -> list[Field]:
    return walk(v) if isinstance(v, bytes) else []


def as_f64(v: int | bytes | None) -> float | None:
    return struct.unpack("<d", v)[0] if isinstance(v, bytes) and len(v) == 8 else None
