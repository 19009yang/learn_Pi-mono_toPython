"""UUID version 7 generation for session and entry identifiers."""

from __future__ import annotations

import secrets
import threading
import time
import uuid

_lock = threading.Lock()
_last_timestamp_ms = -1
_sequence = 0


def uuidv7() -> str:
    """Return a monotonic UUIDv7 string.

    UUIDv7 puts the Unix millisecond timestamp in the most significant bits.
    A 12-bit sequence makes calls made in the same millisecond ordered, while
    the remaining 62 random bits retain collision resistance.
    """

    global _last_timestamp_ms, _sequence

    with _lock:
        timestamp_ms = time.time_ns() // 1_000_000
        if timestamp_ms > _last_timestamp_ms:
            _last_timestamp_ms = timestamp_ms
            _sequence = secrets.randbits(12)
        else:
            timestamp_ms = _last_timestamp_ms
            _sequence = (_sequence + 1) & 0xFFF
            if _sequence == 0:
                # More than 4096 IDs in one millisecond. Advance the logical
                # clock instead of blocking or producing out-of-order values.
                _last_timestamp_ms += 1
                timestamp_ms = _last_timestamp_ms

        random_tail = secrets.randbits(62)
        value = (
            (timestamp_ms & ((1 << 48) - 1)) << 80
            | 0x7 << 76
            | _sequence << 64
            | 0b10 << 62
            | random_tail
        )

    return str(uuid.UUID(int=value))
