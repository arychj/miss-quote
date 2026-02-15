"""
Generic ring buffer backed by collections.deque.

Used to capture pre-speech audio context so the first syllable
is never clipped when VAD detects speech onset.
"""

from __future__ import annotations

import collections
from typing import Generic, TypeVar

T = TypeVar("T")


class RingBuffer(Generic[T]):
    """Fixed-capacity FIFO that silently drops the oldest item on overflow."""

    __slots__ = ("_buf",)

    def __init__(self, capacity: int) -> None:
        self._buf: collections.deque[T] = collections.deque(maxlen=capacity)

    # ── mutators ──────────────────────────────────

    def append(self, item: T) -> None:
        """Push *item*; oldest is discarded if full."""
        self._buf.append(item)

    def clear(self) -> None:
        self._buf.clear()

    # ── accessors ─────────────────────────────────

    def drain(self) -> list[T]:
        """Pop **all** items (oldest-first) and return as a list."""
        items = list(self._buf)
        self._buf.clear()
        return items

    def __len__(self) -> int:
        return len(self._buf)

    def __bool__(self) -> bool:
        return bool(self._buf)

    def __repr__(self) -> str:
        return f"RingBuffer(len={len(self._buf)}, cap={self._buf.maxlen})"
