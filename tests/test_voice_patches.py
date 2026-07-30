import pytest
from discord.ext.voice_recv.opus import PacketDecoder, VoiceData
from discord.opus import OpusError

from miss_quote.bot import voice_patches

CORRUPTED_STREAM = -4
SPEAKER_ID = 4242
ENCRYPTED = b"encrypted-opus-frame"
PLAINTEXT = b"plaintext-opus-frame"
OPUS_SILENCE = b"\xf8\xff\xfe"


def _opus_error() -> OpusError:
    """
    Build an OpusError without going through its constructor.

    OpusError.__init__ calls opus_strerror through ctypes, which needs libopus
    loaded. The container has it; a bare test environment does not, and the
    patches catch the exception rather than inspecting it.
    """
    error = OpusError.__new__(OpusError)
    error.code = CORRUPTED_STREAM
    return error


class FakeSession:
    """Stands in for davey's DaveSession."""

    def __init__(self, ready: bool = True, error: Exception | None = None) -> None:
        self.ready = ready
        self.error = error
        self.decrypted: list[tuple[int, bytes]] = []
        self.passthrough_calls = 0

    def set_passthrough_mode(self, enabled: bool, expiry: int | None = None) -> None:
        self.passthrough_calls += 1

    def decrypt(self, user_id: int, media_type, packet: bytes) -> bytes:
        if self.error is not None:
            raise self.error
        self.decrypted.append((user_id, packet))
        return PLAINTEXT


class FakePacket:
    """A real-looking RTP packet whose payload the test controls."""

    def __init__(self, data: bytes | None = ENCRYPTED, real: bool = True) -> None:
        self.decrypted_data = data
        self.sequence = 7
        self.timestamp = 99
        self._real = real

    def __bool__(self) -> bool:
        return self._real

    def is_silence(self) -> bool:
        return self.decrypted_data == OPUS_SILENCE


class FakeMember:
    id = SPEAKER_ID

    def __repr__(self) -> str:
        return "speaker"


class FakeDecoder:
    """Enough of PacketDecoder for the patched _process_packet to run."""

    def __init__(self, session: FakeSession | None, member=FakeMember()) -> None:
        self._session = session
        self._member = member
        self._cached_id = None
        self._last_seq = -1
        self._last_ts = -1
        self.ssrc = 1
        self.decoded: list[bytes | None] = []

        self.sink = type("Sink", (), {"wants_opus": lambda self: False})()
        self.sink.voice_client = type(
            "VC",
            (),
            {
                "_connection": type("C", (), {})(),
                "_get_id_from_ssrc": lambda self, ssrc: None,
            },
        )()
        self.sink.voice_client._connection.dave_session = session

    def _get_cached_member(self):
        return self._member

    def _decode_packet(self, packet):
        self.decoded.append(packet.decrypted_data)
        return packet, b"pcm"


@pytest.fixture
def process(monkeypatch):
    """Apply the DAVE patch to a stand-in decoder rather than the real one."""
    monkeypatch.setattr(
        PacketDecoder, "_process_packet", lambda self, packet: None, raising=False
    )
    voice_patches.enable_dave_decryption()
    return PacketDecoder._process_packet


def test_encrypted_frame_is_decrypted_before_decoding(process):
    session = FakeSession()
    decoder = FakeDecoder(session)
    packet = FakePacket()

    data = process(decoder, packet)

    assert session.decrypted == [(SPEAKER_ID, ENCRYPTED)]
    assert decoder.decoded == [PLAINTEXT], "Opus must see the decrypted payload"
    assert data.pcm == b"pcm"


def test_passthrough_is_enabled_once_per_decoder(process):
    session = FakeSession()
    decoder = FakeDecoder(session)

    for _ in range(5):
        process(decoder, FakePacket())

    assert session.passthrough_calls == 1


def test_failed_decryption_drops_the_frame_without_raising(process):
    session = FakeSession(error=RuntimeError("bad epoch"))
    decoder = FakeDecoder(session)
    packet = FakePacket()

    data = process(decoder, packet)

    assert data.pcm == b""
    assert decoder.decoded == [], "a frame that failed to decrypt must not be decoded"


def test_silence_packets_are_passed_through_untouched(process):
    session = FakeSession()
    decoder = FakeDecoder(session)
    packet = FakePacket(data=OPUS_SILENCE)

    process(decoder, packet)

    assert session.decrypted == []
    assert decoder.decoded == [OPUS_SILENCE]


def test_fake_packets_are_not_decrypted(process):
    """Loss-concealment packets carry no payload to decrypt."""
    session = FakeSession()
    decoder = FakeDecoder(session)

    process(decoder, FakePacket(data=b"", real=False))

    assert session.decrypted == []


def test_frames_pass_through_until_the_session_is_ready(process):
    session = FakeSession(ready=False)
    decoder = FakeDecoder(session)

    process(decoder, FakePacket())

    assert session.decrypted == []
    assert decoder.decoded == [ENCRYPTED]


def test_missing_session_does_not_raise(process):
    decoder = FakeDecoder(None)

    data = process(decoder, FakePacket())

    assert data.pcm == b"pcm"


def test_unresolved_member_is_not_decrypted(process):
    """Decryption is keyed by speaker, so an unknown speaker has nothing to try."""
    session = FakeSession()
    decoder = FakeDecoder(session, member=None)

    process(decoder, FakePacket())

    assert session.decrypted == []


def test_dave_patch_is_not_applied_twice(monkeypatch):
    monkeypatch.setattr(
        PacketDecoder, "_process_packet", lambda self, packet: None, raising=False
    )

    voice_patches.enable_dave_decryption()
    once = PacketDecoder._process_packet

    voice_patches.enable_dave_decryption()

    assert PacketDecoder._process_packet is once


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
