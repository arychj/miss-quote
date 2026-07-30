"""Encoding to what Discord sends, and the container it is kept in."""

import struct

import numpy as np
import pytest
from discord.oggparse import OggError

from miss_quote.audio import opus

RATE = opus.SAMPLE_RATE
CHANNELS = opus.CHANNELS

OGG_MAGIC = b"OggS"
HEADER_PAGES = 2

# Page flags, restated so a test asserting one is not reading the constant it
# is checking.
BEGINNING_OF_STREAM = 0x02
END_OF_STREAM = 0x04

FIRST = 0
NONE = 0

# Whatever a clip is, it should never come back bigger than a tenth of the
# samples it was made from. The real figure on speech is about a tenth; this
# only has to fail if a clip stopped being encoded at all.
LARGEST_WORTHWHILE_FRACTION = 4

# Opus codes stereo as mid and side, so a duplicated channel returns as very
# nearly itself. Against a source amplitude of 10,000 this is under 3%.
CHANNEL_TOLERANCE = 256


def _tone(seconds: float) -> bytes:
    """Playback-shaped PCM: 48 kHz, stereo, the same signal in both channels."""
    samples = int(seconds * RATE)
    t = np.arange(samples)
    mono = (np.sin(t * 2 * np.pi * 440 / RATE) * 10_000).astype(np.int16)

    return np.repeat(mono.reshape(-1, 1), CHANNELS, axis=1).tobytes()


def _pages(data: bytes) -> list[dict]:
    """Every Ogg page in a file, as the fields a test wants to assert on."""
    pages = []
    offset = 0

    while offset < len(data):
        magic, _, flags, granule, serial, sequence, _, count = struct.unpack(
            opus.HEADER_FORMAT, data[offset : offset + 27]
        )
        assert magic == OGG_MAGIC

        segments = data[offset + 27 : offset + 27 + count]
        body = sum(segments)
        pages.append(
            {"flags": flags, "granule": granule, "serial": serial, "sequence": sequence}
        )
        offset += 27 + count + body

    return pages


# ── encoding ──────────────────────────────────────


def test_one_packet_per_frame():
    packets = opus.encoded(_tone(1.0))

    assert len(packets) == 1000 // opus.FRAME_MILLISECONDS


def test_a_part_frame_is_padded_rather_than_dropped():
    """The end of a word rather than a clean cut at a frame boundary."""
    whole = opus.encoded(_tone(0.10))
    ragged = opus.encoded(_tone(0.10) + b"\x00" * (opus.FRAME_BYTES // 2))

    assert len(ragged) == len(whole) + 1


def test_encoding_streams_rather_than_waiting_for_the_whole_clip():
    """A cache miss plays while it is still being synthesized."""
    encoder = opus.Encoder()
    pcm = _tone(1.0)
    half = len(pcm) // 2

    assert encoder.feed(pcm[:half])


def test_a_chunk_short_of_a_frame_yields_nothing_yet():
    assert opus.Encoder().feed(b"\x00" * (opus.FRAME_BYTES - 2)) == []


def test_flushing_an_empty_encoder_yields_nothing():
    assert opus.Encoder().flush() == []


def test_fed_in_pieces_is_the_same_as_fed_whole():
    """The encoder carries state, so the seams must not be where chunks landed."""
    pcm = _tone(1.0)
    encoder = opus.Encoder()

    piecemeal = []
    for offset in range(0, len(pcm), 997):
        piecemeal += encoder.feed(pcm[offset : offset + 997])
    piecemeal += encoder.flush()

    assert piecemeal == opus.encoded(pcm)


def test_a_clip_is_a_fraction_of_the_samples():
    pcm = _tone(3.0)
    packets = opus.encoded(pcm)

    assert sum(map(len, packets)) < len(pcm) // LARGEST_WORTHWHILE_FRACTION


# ── decoding ──────────────────────────────────────


def test_a_clip_decodes_to_about_what_went_in():
    pcm = _tone(1.0)
    back = opus.decoded(opus.encoded(pcm))

    assert abs(len(back) - len(pcm)) <= opus.FRAME_BYTES


def test_a_decoded_clip_is_still_the_same_in_both_channels():
    samples = np.frombuffer(opus.decoded(opus.encoded(_tone(1.0))), dtype=np.int16)
    difference = np.abs(
        samples[0::2].astype(np.int32) - samples[1::2].astype(np.int32)
    ).max()

    assert difference <= CHANNEL_TOLERANCE


def test_decoding_nothing_gives_nothing():
    assert opus.decoded([]) == b""


# ── the container ─────────────────────────────────


def test_a_written_clip_reads_back_packet_for_packet(tmp_path):
    packets = opus.encoded(_tone(3.0))
    path = tmp_path / "clip.opus"

    opus.write(path, packets)

    assert opus.read(path) == packets


def test_a_written_clip_is_ogg(tmp_path):
    path = tmp_path / "clip.opus"
    opus.write(path, opus.encoded(_tone(1.0)))

    assert path.read_bytes().startswith(OGG_MAGIC)


def test_the_headers_are_the_first_two_pages(tmp_path):
    path = tmp_path / "clip.opus"
    opus.write(path, opus.encoded(_tone(1.0)))

    pages = _pages(path.read_bytes())

    assert pages[0]["flags"] == BEGINNING_OF_STREAM
    assert pages[1]["flags"] == NONE


def test_the_last_page_ends_the_stream(tmp_path):
    path = tmp_path / "clip.opus"
    opus.write(path, opus.encoded(_tone(1.0)))

    pages = _pages(path.read_bytes())

    assert pages[-1]["flags"] == END_OF_STREAM
    assert all(page["flags"] != END_OF_STREAM for page in pages[:-1])


def test_pages_are_numbered_in_order(tmp_path):
    path = tmp_path / "clip.opus"
    opus.write(path, opus.encoded(_tone(6.0)))

    pages = _pages(path.read_bytes())

    assert [page["sequence"] for page in pages] == list(range(len(pages)))
    assert len({page["serial"] for page in pages}) == 1


def test_the_final_granule_is_the_length_of_the_clip(tmp_path):
    """Which is what a player reads the duration off, plus the encoder's lead-in."""
    packets = opus.encoded(_tone(3.0))
    path = tmp_path / "clip.opus"
    opus.write(path, packets)

    granule = _pages(path.read_bytes())[-1]["granule"]

    assert granule == opus.PRE_SKIP + len(packets) * opus.SAMPLES_PER_FRAME


@pytest.mark.parametrize("seconds", (0.04, 0.2, 1.0, 3.0, 12.0))
def test_a_clip_of_any_length_spans_at_least_two_pages(tmp_path, seconds):
    """
    A single-page stream is legal and libsndfile will not open one.

    The cache directory is meant to be something you can listen to, so a clip
    that only this module can read is a clip that has lost half its point.
    """
    path = tmp_path / "clip.opus"
    opus.write(path, opus.encoded(_tone(seconds)))

    assert len(_pages(path.read_bytes())) >= HEADER_PAGES + 2


def test_writing_is_reproducible(tmp_path):
    """Same clip, same bytes, so nothing churns a volume on every restart."""
    packets = opus.encoded(_tone(1.0))
    first = tmp_path / "first.opus"
    second = tmp_path / "second.opus"

    opus.write(first, packets)
    opus.write(second, packets)

    assert first.read_bytes() == second.read_bytes()


def test_a_truncated_file_is_refused_rather_than_half_read(tmp_path):
    path = tmp_path / "clip.opus"
    opus.write(path, opus.encoded(_tone(3.0)))
    path.write_bytes(path.read_bytes()[:200])

    with pytest.raises((OggError, struct.error, ValueError)):
        opus.read(path)


def test_something_that_is_not_ogg_is_refused(tmp_path):
    path = tmp_path / "clip.opus"
    path.write_bytes(b"this is not a container")

    with pytest.raises((OggError, struct.error, ValueError)):
        opus.read(path)


def test_seconds_reports_the_length(tmp_path):
    assert opus.seconds(opus.encoded(_tone(3.0))) == pytest.approx(3.0, abs=0.02)


# ── decoding as it arrives ────────────────────────


def test_decoding_streams_rather_than_waiting_for_the_whole_clip():
    """A clip played quieter must not wait for its own last packet."""
    packets = opus.encoded(_tone(3.0))

    assert opus.Decoder().feed(packets[0])


def test_fed_packet_by_packet_is_the_same_as_fed_whole():
    packets = opus.encoded(_tone(1.0))
    decoder = opus.Decoder()

    assert b"".join(decoder.feed(packet) for packet in packets) == opus.decoded(packets)


def test_the_lead_in_comes_off_the_front_of_the_clip_only():
    """Not off each packet, which would punch a hole every 20 ms."""
    packets = opus.encoded(_tone(1.0))
    decoder = opus.Decoder()

    first = decoder.feed(packets[0])
    rest = [decoder.feed(packet) for packet in packets[1:]]

    assert len(first) < opus.FRAME_BYTES
    assert all(len(pcm) == opus.FRAME_BYTES for pcm in rest)
