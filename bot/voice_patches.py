"""
Runtime patches to discord.py and discord-ext-voice-recv.

Both gaps are in dependencies rather than in this bot, and both make voice
receive fail silently, so they are patched at startup rather than worked around
at the call site. `discord-ext-voice-recv` is pinned to a SHA on a repository
whose last commit is 2025-06-18, so there is no release to wait for.
"""

from __future__ import annotations

from typing import Optional

from utils.logging import get_logger

logger = get_logger(__name__)

DECODE_FAILURE_LOG_INTERVAL = 100


def disable_dave() -> None:
    """
    Stop advertising DAVE, Discord's MLS end-to-end encryption for voice.

    discord.py 2.7.1 ships `davey` and advertises DAVE protocol version 1 in the
    voice IDENTIFY. `discord-ext-voice-recv` has no DAVE support at all, so it
    hands still-encrypted payloads to the Opus decoder and every packet fails as
    `corrupted stream`. Advertising version 0 keeps the media path on the
    transport encryption the extension understands.

    Only `voice_state.has_dave` is touched. `voice_client.has_dave` is a
    separate binding, and `VoiceClient.__init__` raises RuntimeError when it is
    false, so clearing that one would stop voice working entirely.
    """
    import discord.voice_state

    if not discord.voice_state.has_dave:
        return

    discord.voice_state.has_dave = False
    logger.info("DAVE disabled; voice media uses transport encryption only.")


def guard_packet_decoding() -> None:
    """
    Drop packets the Opus decoder rejects instead of killing voice receive.

    `PacketRouter.run` wraps its whole loop in one try/except and calls
    `stop_listening()` from the `finally`, so a single undecodable packet ends
    voice receive for the connection and nothing re-arms it. Returning None for
    a bad packet keeps the router thread alive; the loop already skips None.
    """
    from discord.ext.voice_recv.opus import PacketDecoder, VoiceData

    if getattr(PacketDecoder.pop_data, "_is_guarded", False):
        return

    original = PacketDecoder.pop_data
    failures = 0

    def pop_data(self, *, timeout: float = 0) -> Optional[VoiceData]:
        nonlocal failures

        try:
            return original(self, timeout=timeout)
        except Exception as exc:
            failures += 1
            if failures % DECODE_FAILURE_LOG_INTERVAL == 1:
                logger.warning(
                    "Dropped an undecodable voice packet (%d so far): %s", failures, exc
                )
            return None

    pop_data._is_guarded = True  # type: ignore[attr-defined]
    PacketDecoder.pop_data = pop_data  # type: ignore[method-assign]


def apply() -> None:
    """Apply every patch. Called once, before the bot connects."""
    disable_dave()
    guard_packet_decoding()
