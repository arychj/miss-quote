"""
Runtime patches to discord-ext-voice-recv.

Discord has required DAVE, its MLS-based end-to-end encryption, on every
non-stage voice call since 2026-03-02; a client advertising no support is
rejected at the handshake with close code 4017. discord.py negotiates the MLS
session and holds it on the connection state, but `discord-ext-voice-recv` never
learned to use it, so it feeds still-encrypted payloads to the Opus decoder.

The extension is pinned to a SHA on a repository whose last commit is
2025-06-18, so these are patched here rather than waited on. See todo.md.
"""

from __future__ import annotations

from typing import Any, Optional

from utils.logging import get_logger

logger = get_logger(__name__)

DECODE_FAILURE_LOG_INTERVAL = 100
DECRYPT_FAILURE_LOG_INTERVAL = 100

# Seconds a passthrough allowance survives, matching what discord.py uses when
# it is told to expect unencrypted frames during an epoch transition.
PASSTHROUGH_EXPIRY_SECONDS = 10


def _dave_session(decoder: Any) -> Optional[Any]:
    """The MLS session discord.py negotiated for this decoder's connection."""
    connection = getattr(decoder.sink.voice_client, "_connection", None)
    return getattr(connection, "dave_session", None)


def _is_encrypted_audio(packet: Any, member: Any, session: Any) -> bool:
    """
    Whether this packet carries an MLS-encrypted Opus frame we can decrypt.

    Fake packets carry no payload and exist only to drive loss concealment,
    silence packets are a fixed sentinel that is never encrypted, and decryption
    is keyed by speaker, so an unresolved member means there is nothing to try.
    """
    return (
        bool(packet)
        and member is not None
        and session is not None
        and session.ready
        and packet.decrypted_data is not None
        and not packet.is_silence()
    )


def _allow_passthrough(decoder: Any, session: Any) -> None:
    """
    Let unencrypted frames through, once per decoder.

    Frames arrive unencrypted while the group is moving between MLS epochs. The
    call is FFI into davey, so it is made once rather than per packet.
    """
    if getattr(decoder, "_passthrough_allowed", False):
        return

    session.set_passthrough_mode(True, PASSTHROUGH_EXPIRY_SECONDS)
    decoder._passthrough_allowed = True


def enable_dave_decryption() -> None:
    """
    Decrypt DAVE frames before they reach the Opus decoder.

    Upstream decodes first and resolves the speaker afterwards. Decryption is
    keyed by speaker, so the order is reversed here; everything else matches
    `PacketDecoder._process_packet` as pinned.
    """
    from discord.ext.voice_recv.opus import PacketDecoder, VoiceData

    try:
        from davey import MediaType
    except ImportError:
        logger.error("davey is not installed; DAVE frames cannot be decrypted.")
        return

    if getattr(PacketDecoder._process_packet, "_decrypts_dave", False):
        return

    failures = 0

    def _process_packet(self, packet: Any) -> Any:
        nonlocal failures

        member = self._get_cached_member()
        if member is None:
            self._cached_id = self.sink.voice_client._get_id_from_ssrc(self.ssrc)
            member = self._get_cached_member()

        session = _dave_session(self)

        if _is_encrypted_audio(packet, member, session):
            _allow_passthrough(self, session)

            try:
                packet.decrypted_data = session.decrypt(
                    member.id, MediaType.audio, bytes(packet.decrypted_data)
                )
            except Exception as exc:
                failures += 1
                if failures % DECRYPT_FAILURE_LOG_INTERVAL == 1:
                    logger.warning(
                        "Could not decrypt a DAVE frame from %s (%d so far): %s",
                        member,
                        failures,
                        exc,
                    )

                self._last_seq = packet.sequence
                self._last_ts = packet.timestamp
                return VoiceData(packet, member, pcm=b"")

        pcm = None
        if not self.sink.wants_opus():
            packet, pcm = self._decode_packet(packet)

        data = VoiceData(packet, member, pcm=pcm)
        self._last_seq = packet.sequence
        self._last_ts = packet.timestamp

        return data

    _process_packet._decrypts_dave = True  # type: ignore[attr-defined]
    PacketDecoder._process_packet = _process_packet  # type: ignore[method-assign]
    logger.info("DAVE decryption enabled for incoming voice.")


def guard_packet_decoding() -> None:
    """
    Drop packets the decoder rejects instead of killing voice receive.

    `PacketRouter.run` wraps its whole loop in one try/except and calls
    `stop_listening()` from the `finally`, so a single bad packet ends voice
    receive for the connection and nothing re-arms it. Returning None keeps the
    router thread alive; the loop already skips None.
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
    enable_dave_decryption()
    guard_packet_decoding()
