"""
Per-user audio state management for the STT process.

Each user who speaks in the voice channel gets their own UserState,
which encapsulates all buffers and the VAD iterator.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from audio.ring_buffer import RingBuffer
from config import vad_cfg, process_cfg
from utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class UserState:
    """Mutable bundle of per-user audio processing state."""
    user_id: int
    raw_buffer: bytearray = field(default_factory=bytearray)
    speech_buffer: bytearray = field(default_factory=bytearray)
    ring_buffer: RingBuffer = field(default=None)       # type: ignore[assignment]
    vad_iterator: object = field(default=None)            # Silero VADIterator
    last_activity: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.ring_buffer is None:
            self.ring_buffer = RingBuffer[bytes](vad_cfg.ring_buffer_frames)

    @property
    def is_speaking(self) -> bool:
        return len(self.speech_buffer) > 0

    def reset_speech(self) -> bytearray:
        """Return accumulated speech and clear the buffer."""
        data = self.speech_buffer[:]
        self.speech_buffer = bytearray()
        return data

    def touch(self) -> None:
        """Update the last-activity timestamp."""
        self.last_activity = time.time()


class UserStateManager:
    """Registry of active users, with automatic timeout cleanup."""

    def __init__(self, vad_iterator_factory) -> None:
        """
        Parameters
        ----------
        vad_iterator_factory : callable
            A zero-arg callable that returns a fresh Silero VADIterator.
        """
        self._users: dict[int, UserState] = {}
        self._vad_factory = vad_iterator_factory

    # ── access ────────────────────────────────────

    def get_or_create(self, user_id: int) -> UserState:
        """Return existing state or create a new one for *user_id*."""
        if user_id not in self._users:
            state = UserState(user_id=user_id)
            state.vad_iterator = self._vad_factory()
            self._users[user_id] = state
            logger.info("New user registered: %s", user_id)
        state = self._users[user_id]
        state.touch()
        return state

    # ── cleanup ───────────────────────────────────

    def remove(self, user_id: int) -> UserState | None:
        if user_id in self._users:
            state = self._users.pop(user_id)
            logger.info("User removed: %s", user_id)
            return state
        return None

    def remove_all(self) -> list[UserState]:
        """Remove and return every active user state."""
        states = list(self._users.values())
        self._users.clear()
        if states:
            logger.info("Removed all user states (%d).", len(states))
        return states

    def stale_speech_states(self) -> list[UserState]:
        """Return speaking users with no fresh audio past the flush timeout."""
        now = time.time()
        return [
            state for state in self._users.values()
            if state.is_speaking
            and now - state.last_activity > process_cfg.speech_flush_timeout_seconds
        ]

    def cleanup_inactive(self) -> list[UserState]:
        """Remove users exceeding the timeout. Returns removed states."""
        now = time.time()
        expired = [
            uid for uid, s in self._users.items()
            if now - s.last_activity > process_cfg.user_timeout_seconds
        ]
        states: list[UserState] = []
        for uid in expired:
            state = self.remove(uid)
            if state:
                states.append(state)
                logger.info("User %s timed out; cleaned up.", uid)
        return states

    @property
    def active_count(self) -> int:
        return len(self._users)
