"""Feeding Discord's player thread from the event loop."""

import asyncio
import threading
from dataclasses import replace

import numpy as np
import pytest

import miss_quote.bot.speaker as speaker_module
from miss_quote.audio.gain import scaled
from miss_quote.bot.speaker import DiscordSpeaker, OpusStream, PCMStream
from miss_quote.config import audio_cfg
from miss_quote.transcript.writer import Source

GUILD_ID = 1
CHANNEL_ID = 2
OTHER_CHANNEL_ID = 3

SOURCE = Source(
    guild_id=GUILD_ID, guild_alias="first-server", channel_id=CHANNEL_ID, channel="general-voice"
)

FRAME_BYTES = audio_cfg.playback_frame_bytes
LOUD = b"\x11" * FRAME_BYTES
STALL_SECONDS = 0.2
PATIENCE_SECONDS = 2.0
HALF_VOLUME = 0.5
SAMPLE_DTYPE = np.int16

# What a fake clip reports having been asked for, so a test can name the path
# the speaker took rather than infer it.
ENCODED = "packets"
SAMPLES = "pcm"

# Stands in for one 20 ms Opus packet, which is opaque to everything here.
OPUS_PACKET = b"\x78\x9a\xbc"


class FakeVoiceClient:
    """A voice client that drains a source on a thread, as discord.py does."""

    def __init__(self, channel_id: int = CHANNEL_ID, connected: bool = True) -> None:
        self.channel = type("Channel", (), {"id": channel_id, "name": "general-voice"})()
        self.frames: list[bytes] = []
        self.sources: list = []
        self._connected = connected
        self._playing = False
        self._thread: threading.Thread | None = None

    def is_connected(self) -> bool:
        return self._connected

    def is_playing(self) -> bool:
        return self._playing

    def play(self, source, after) -> None:
        self._playing = True
        self.sources.append(source)

        def drain() -> None:
            while (frame := source.read()):
                self.frames.append(frame)
            self._playing = False
            after(None)

        self._thread = threading.Thread(target=drain, daemon=True)
        self._thread.start()


class FakeGuild:
    def __init__(self, voice_client) -> None:
        self.voice_client = voice_client


async def _audio(*chunks: bytes, delay: float = 0):
    for chunk in chunks:
        if delay:
            await asyncio.sleep(delay)
        yield chunk


def _speaker(voice_client) -> DiscordSpeaker:
    return DiscordSpeaker(lambda guild_id: FakeGuild(voice_client))


# ── the stream ────────────────────────────────────


def test_a_whole_frame_is_handed_over():
    stream = PCMStream(STALL_SECONDS)
    stream.feed(LOUD)
    stream.finish()

    assert stream.read() == LOUD
    assert stream.read() == b""


def test_the_tail_is_padded_rather_than_dropped():
    """A short read ends the clip, so the last few milliseconds need a frame."""
    stream = PCMStream(STALL_SECONDS)
    partial = b"\x22" * (FRAME_BYTES // 2)
    stream.feed(partial)
    stream.finish()

    frame = stream.read()

    assert len(frame) == FRAME_BYTES
    assert frame.startswith(partial)
    assert frame[len(partial) :] == b"\x00" * (FRAME_BYTES - len(partial))


def test_the_player_waits_for_audio_that_has_not_arrived():
    """The whole point: playback starts before synthesis has finished."""
    stream = PCMStream(PATIENCE_SECONDS)
    frames: list[bytes] = []

    reader = threading.Thread(target=lambda: frames.append(stream.read()), daemon=True)
    reader.start()

    stream.feed(LOUD)
    reader.join(timeout=PATIENCE_SECONDS)

    assert frames == [LOUD]


def test_a_stalled_synthesizer_ends_the_clip(caplog):
    """Bounded, so a thread and a voice connection are not held indefinitely."""
    stream = PCMStream(STALL_SECONDS)

    with caplog.at_level("WARNING"):
        assert stream.read() == b""

    assert any("ending the clip" in record.message for record in caplog.records)


def test_a_clip_is_turned_down_on_the_way_in():
    """Fed rather than read, so every part of a clip is scaled once."""
    stream = PCMStream(STALL_SECONDS, HALF_VOLUME)
    stream.feed(LOUD)
    stream.finish()

    frame = np.frombuffer(stream.read(), dtype=SAMPLE_DTYPE)

    assert list(frame) == list(np.frombuffer(LOUD, dtype=SAMPLE_DTYPE) // 2)


def test_audio_fed_in_pieces_is_reframed():
    """A synthesizer's chunks have nothing to do with 20 ms frames."""
    stream = PCMStream(STALL_SECONDS)
    piece = FRAME_BYTES // 3

    for offset in range(0, FRAME_BYTES * 2, piece):
        stream.feed(b"\x33" * piece)
    stream.finish()

    frames = []
    while (frame := stream.read()):
        frames.append(frame)

    assert all(len(frame) == FRAME_BYTES for frame in frames)


# ── the speaker ───────────────────────────────────


async def test_a_clip_reaches_the_voice_client():
    voice_client = FakeVoiceClient()

    await _speaker(voice_client).play(SOURCE, _audio(LOUD))

    assert voice_client.frames == [LOUD]


async def test_play_returns_only_once_the_clip_has_finished():
    """So two announcements queue rather than land on top of each other."""
    voice_client = FakeVoiceClient()

    await _speaker(voice_client).play(SOURCE, _audio(LOUD, LOUD))

    assert not voice_client.is_playing()
    assert len(voice_client.frames) == 2


async def test_a_clip_streamed_slowly_still_plays_whole():
    voice_client = FakeVoiceClient()

    await _speaker(voice_client).play(SOURCE, _audio(LOUD, LOUD, LOUD, delay=0.05))

    assert len(voice_client.frames) == 3


async def test_nothing_is_played_when_the_bot_has_left():
    voice_client = FakeVoiceClient(connected=False)

    await _speaker(voice_client).play(SOURCE, _audio(LOUD))

    assert voice_client.frames == []


async def test_nothing_is_played_when_the_bot_has_moved_on():
    """A clip is synthesized after the fact; by then the channel may be stale."""
    voice_client = FakeVoiceClient(channel_id=OTHER_CHANNEL_ID)

    await _speaker(voice_client).play(SOURCE, _audio(LOUD))

    assert voice_client.frames == []


async def test_a_scale_is_applied_on_top_of_the_deployment_volume(monkeypatch):
    """A tool says how much quieter than usual, not how loud."""
    monkeypatch.setattr(
        speaker_module, "audio_cfg", replace(audio_cfg, playback_volume=HALF_VOLUME)
    )
    voice_client = FakeVoiceClient()

    await _speaker(voice_client).play(SOURCE, _audio(LOUD), HALF_VOLUME)

    frame = np.frombuffer(voice_client.frames[0], dtype=SAMPLE_DTYPE)
    assert list(frame) == list(np.frombuffer(LOUD, dtype=SAMPLE_DTYPE) // 4)


async def test_a_clip_with_no_scale_is_played_at_the_deployment_volume():
    voice_client = FakeVoiceClient()

    await _speaker(voice_client).play(SOURCE, _audio(LOUD))

    assert voice_client.frames == [scaled(LOUD, audio_cfg.playback_volume)]


async def test_nothing_is_played_without_a_voice_client():
    speaker = DiscordSpeaker(lambda guild_id: None)

    await speaker.play(SOURCE, _audio(LOUD))


async def test_two_clips_at_once_are_played_in_turn():
    """Two people swearing together are fined one after the other."""
    voice_client = FakeVoiceClient()
    speaker = _speaker(voice_client)

    await asyncio.gather(
        speaker.play(SOURCE, _audio(LOUD, delay=0.05)),
        speaker.play(SOURCE, _audio(LOUD, delay=0.05)),
    )

    assert len(voice_client.frames) == 2


async def test_a_failing_stream_does_not_strand_the_player():
    """Without the unconditional finish, the player thread waits forever."""
    voice_client = FakeVoiceClient()

    async def exploding():
        yield LOUD
        raise RuntimeError("the synthesizer fell over")

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(
            _speaker(voice_client).play(SOURCE, exploding()), timeout=PATIENCE_SECONDS
        )

    assert voice_client.frames == [LOUD]


# ── which form a clip goes out in ─────────────────


class FakePhrase:
    """
    A clip the speaker can take either way, as the cache hands one over.

    The two forms are told apart by what they contain rather than by type, so a
    test can say which one the speaker reached for.
    """

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def packets(self):
        self.asked.append(ENCODED)
        yield OPUS_PACKET

    async def pcm(self):
        self.asked.append(SAMPLES)
        yield LOUD


async def test_a_clip_at_full_volume_is_sent_as_it_was_stored():
    """The point of storing it encoded: discord.py builds no encoder at all."""
    voice_client = FakeVoiceClient()
    phrase = FakePhrase()

    await _speaker(voice_client).play(SOURCE, phrase)

    assert phrase.asked == [ENCODED]
    assert voice_client.frames == [OPUS_PACKET]


async def test_a_clip_below_full_volume_is_decoded_first():
    """A gain is a multiplication, and there is nothing to multiply in a packet."""
    voice_client = FakeVoiceClient()
    phrase = FakePhrase()

    await _speaker(voice_client).play(SOURCE, phrase, HALF_VOLUME)

    assert phrase.asked == [SAMPLES]


async def test_the_deployment_volume_alone_can_force_the_decode(monkeypatch):
    """A channel that turned everything down never gets the encoded path."""
    monkeypatch.setattr(
        speaker_module, "audio_cfg", replace(audio_cfg, playback_volume=HALF_VOLUME)
    )
    phrase = FakePhrase()

    await _speaker(FakeVoiceClient()).play(SOURCE, phrase)

    assert phrase.asked == [SAMPLES]


async def test_an_encoded_clip_says_so_to_the_player():
    """`is_opus` is the whole mechanism; without it the packets are re-encoded."""
    voice_client = FakeVoiceClient()

    await _speaker(voice_client).play(SOURCE, FakePhrase())

    assert voice_client.sources[-1].is_opus() is True


async def test_a_scaled_clip_does_not_say_so():
    voice_client = FakeVoiceClient()

    await _speaker(voice_client).play(SOURCE, FakePhrase(), HALF_VOLUME)

    assert voice_client.sources[-1].is_opus() is False


async def test_a_plain_stream_of_samples_is_still_accepted():
    """What a tool assembling a clip of its own hands over."""
    voice_client = FakeVoiceClient()

    await _speaker(voice_client).play(SOURCE, _audio(LOUD))

    assert voice_client.frames == [LOUD]


def test_a_packet_is_handed_over_whole():
    """One packet is one frame; half of one is not audio."""
    stream = OpusStream(STALL_SECONDS)
    stream.feed(OPUS_PACKET)
    stream.finish()

    assert stream.read() == OPUS_PACKET
    assert stream.read() == b""


def test_packets_keep_their_order():
    stream = OpusStream(STALL_SECONDS)
    for packet in (b"one", b"two", b"three"):
        stream.feed(packet)
    stream.finish()

    assert [stream.read() for _ in range(3)] == [b"one", b"two", b"three"]


def test_a_stalled_encoder_ends_the_encoded_clip(caplog):
    stream = OpusStream(STALL_SECONDS)

    with caplog.at_level("WARNING"):
        assert stream.read() == b""

    assert "No audio" in caplog.text
