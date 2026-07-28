import discord.voice_client
import discord.voice_state
import pytest
from discord.ext.voice_recv.opus import PacketDecoder
from discord.opus import OpusError

from bot import voice_patches

CORRUPTED_STREAM = -4


def _opus_error() -> OpusError:
    """
    Build an OpusError without going through its constructor.

    OpusError.__init__ calls opus_strerror through ctypes, which needs libopus
    loaded. The container has it; a bare test environment does not, and the
    guard catches the exception rather than inspecting it.
    """
    error = OpusError.__new__(OpusError)
    error.code = CORRUPTED_STREAM
    return error


@pytest.fixture(autouse=True)
def restore_dave(monkeypatch):
    """Patches mutate imported modules; keep them out of other tests."""
    monkeypatch.setattr(discord.voice_state, "has_dave", discord.voice_state.has_dave)
    monkeypatch.setattr(discord.voice_client, "has_dave", discord.voice_client.has_dave)


def test_disable_dave_stops_advertising_e2ee():
    monkeypatched = discord.voice_state
    monkeypatched.has_dave = True

    voice_patches.disable_dave()

    assert monkeypatched.has_dave is False


def test_disable_dave_leaves_voice_client_alone():
    """
    VoiceClient.__init__ raises RuntimeError when its own has_dave is false.

    The two modules hold separate bindings, so clearing both would stop voice
    connecting at all rather than just downgrading the encryption.
    """
    discord.voice_state.has_dave = True
    discord.voice_client.has_dave = True

    voice_patches.disable_dave()

    assert discord.voice_client.has_dave is True


def test_disable_dave_is_idempotent():
    discord.voice_state.has_dave = False

    voice_patches.disable_dave()

    assert discord.voice_state.has_dave is False


class _Decoder:
    """Stands in for a PacketDecoder whose packets fail to decode."""

    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.calls = 0

    def pop_data(self, *, timeout: float = 0):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return "voice-data"


@pytest.fixture
def guarded(monkeypatch):
    """Apply the decode guard to a stand-in decoder rather than the real one."""
    monkeypatch.setattr(PacketDecoder, "pop_data", _Decoder.pop_data, raising=False)
    voice_patches.guard_packet_decoding()
    return PacketDecoder.pop_data


def test_undecodable_packet_returns_none_instead_of_raising(guarded):
    decoder = _Decoder(_opus_error())

    assert guarded(decoder) is None
    assert decoder.calls == 1


def test_good_packet_passes_through(guarded):
    decoder = _Decoder(None)

    assert guarded(decoder) == "voice-data"


def test_guard_survives_repeated_failures(guarded):
    """One bad packet must not end voice receive; many must not either."""
    decoder = _Decoder(_opus_error())

    results = [guarded(decoder) for _ in range(250)]

    assert results == [None] * 250
    assert decoder.calls == 250


def test_guard_is_not_applied_twice(monkeypatch):
    monkeypatch.setattr(PacketDecoder, "pop_data", _Decoder.pop_data, raising=False)

    voice_patches.guard_packet_decoding()
    once = PacketDecoder.pop_data

    voice_patches.guard_packet_decoding()

    assert PacketDecoder.pop_data is once
