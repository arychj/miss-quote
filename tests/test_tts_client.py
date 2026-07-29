"""Asking a Wyoming server to say something."""

from dataclasses import replace

import pytest
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.tts import Synthesize

from config import tts_cfg
from tts import client as client_module
from tts.client import SynthesisError, synthesize

PHRASE = "you are fined one credit"
VOICE = "some-voice"

RATE = 24_000
WIDTH = 2
MONO = 1
STEREO = 2

PCM = b"\x01\x02" * 100


class FakeClient:
    """A Wyoming server that replies with a scripted list of events."""

    written: list = []

    def __init__(self, events, host=None, port=None) -> None:
        self._events = list(events)
        FakeClient.written = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def write_event(self, event) -> None:
        FakeClient.written.append(event)

    async def read_event(self):
        return self._events.pop(0) if self._events else None


def _serve(monkeypatch, *events):
    """Point the client at a server that answers with these events."""
    scripted = [event.event() if hasattr(event, "event") else event for event in events]
    monkeypatch.setattr(
        client_module,
        "AsyncTcpClient",
        lambda host, port: FakeClient(scripted),
    )


def _start(rate: int = RATE, width: int = WIDTH, channels: int = MONO) -> AudioStart:
    return AudioStart(rate=rate, width=width, channels=channels)


def _chunk(pcm: bytes = PCM, rate: int = RATE) -> AudioChunk:
    return AudioChunk(rate=rate, width=WIDTH, channels=MONO, audio=pcm)


async def _collect(text: str = PHRASE) -> list:
    return [speech async for speech in synthesize(text)]


# ── the happy path ────────────────────────────────


async def test_a_phrase_comes_back_as_audio(monkeypatch):
    _serve(monkeypatch, _start(), _chunk(), AudioStop())

    spoken = await _collect()

    assert [speech.pcm for speech in spoken] == [PCM]
    assert spoken[0].rate == RATE


async def test_audio_is_yielded_as_it_arrives(monkeypatch):
    """Not collected and returned: the caller starts playing the first piece."""
    _serve(monkeypatch, _start(), _chunk(), _chunk(), _chunk(), AudioStop())

    assert len(await _collect()) == 3


async def test_the_phrase_is_what_gets_asked_for(monkeypatch):
    _serve(monkeypatch, _start(), _chunk(), AudioStop())

    await _collect()

    asked = Synthesize.from_event(FakeClient.written[0])
    assert asked.text == PHRASE


async def test_the_configured_voice_is_requested(monkeypatch):
    monkeypatch.setattr(client_module, "tts_cfg", replace(tts_cfg, voice=VOICE))
    _serve(monkeypatch, _start(), _chunk(), AudioStop())

    await _collect()

    assert Synthesize.from_event(FakeClient.written[0]).voice.name == VOICE


async def test_no_configured_voice_asks_for_no_voice(monkeypatch):
    """So a server with one voice loaded needs no setting at all."""
    monkeypatch.setattr(client_module, "tts_cfg", replace(tts_cfg, voice=""))
    _serve(monkeypatch, _start(), _chunk(), AudioStop())

    await _collect()

    assert Synthesize.from_event(FakeClient.written[0]).voice is None


async def test_an_empty_chunk_is_skipped(monkeypatch):
    _serve(monkeypatch, _start(), _chunk(pcm=b""), _chunk(), AudioStop())

    assert len(await _collect()) == 1


# ── failure ───────────────────────────────────────


async def test_hanging_up_before_the_end_is_an_error(monkeypatch):
    """A clip cut short must not be mistaken for a clip, or it gets cached."""
    _serve(monkeypatch, _start(), _chunk())

    with pytest.raises(SynthesisError, match="hung up"):
        await _collect()


async def test_answering_with_no_audio_is_an_error(monkeypatch):
    _serve(monkeypatch, _start(), AudioStop())

    with pytest.raises(SynthesisError, match="no audio"):
        await _collect()


async def test_stereo_is_refused_rather_than_played_at_half_speed(monkeypatch):
    _serve(monkeypatch, _start(channels=STEREO), _chunk(), AudioStop())

    with pytest.raises(SynthesisError, match="channel"):
        await _collect()


async def test_a_silent_server_is_given_up_on(monkeypatch):
    """The budget covers one wait, so a long phrase is not cut off for length."""
    monkeypatch.setattr(client_module, "tts_cfg", replace(tts_cfg, timeout_seconds=0.01))

    class Silent(FakeClient):
        async def read_event(self):
            import asyncio

            await asyncio.sleep(1)

    monkeypatch.setattr(client_module, "AsyncTcpClient", lambda host, port: Silent([]))

    with pytest.raises(SynthesisError, match="quiet"):
        await _collect()
