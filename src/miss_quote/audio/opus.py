"""
Opus, in the two shapes this bot needs it, and the container it is kept in.

Discord speaks Opus. Everything upstream of playback — a synthesizer's output, a
WAV somebody put in the cache directory — is PCM, so something has to encode it,
and until now that was discord.py: it builds an encoder per `play` and converts
every 20 ms frame on the way out. Encoding the same cached phrase on every play
is the waste this module exists to stop. A clip encoded once, kept encoded, and
handed to the player already encoded costs nothing at all to play, because
`AudioSource.is_opus` is how a source says the frames are ready to send.

The frame is fixed by Discord and taken from discord.py rather than restated
here: 20 ms of 48 kHz stereo, which is 960 samples and 3840 bytes. A packet has
to be exactly one of those, because the player asks for one frame per tick and
sends whatever it gets.

Clips are kept as Ogg Opus rather than bare packets. The container costs a few
bytes per page and buys a file that opens in anything — the same reason the WAVs
it replaces were WAVs. `discord.oggparse` reads it; the writer is here because
discord.py never needs one.

Bitrate is `BITRATE_KBPS` and is not the 128 discord.py defaults to. What is
stored is what Discord receives, so this is the quality of the announcement
rather than an archival copy of it, and 32 kbps of VoIP-mode Opus is transparent
on one synthesized voice in a way it would not be on music.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Sequence
from pathlib import Path

import discord.opus as opus
from discord.oggparse import OggError, OggStream

from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

# Discord's frame, taken from discord.py so that a change there cannot leave
# this module encoding packets the player will not send.
SAMPLES_PER_FRAME = opus.Encoder.SAMPLES_PER_FRAME
FRAME_BYTES = opus.Encoder.FRAME_SIZE
FRAME_MILLISECONDS = opus.Encoder.FRAME_LENGTH
CHANNELS = opus.Encoder.CHANNELS
SAMPLE_RATE = opus.Encoder.SAMPLING_RATE
SAMPLE_WIDTH = FRAME_BYTES // (SAMPLES_PER_FRAME * CHANNELS)

# How a clip is encoded. 'voip' rather than discord.py's 'audio' because every
# clip is one synthesized voice, which is what the mode is tuned for, and
# because it is what makes 32 kbps sound like 64.
#
# FEC is on and the loss estimate is discord.py's, since these packets go over
# the same lossy link its own would have.
APPLICATION = "voip"
BITRATE_KBPS = 32
FORWARD_ERROR_CORRECTION = True
EXPECTED_PACKET_LOSS = 0.15
BANDWIDTH = "full"

SILENCE = b"\x00"
NOTHING = b""

# ── Ogg ───────────────────────────────────────────

OGG_MAGIC = b"OggS"
OGG_VERSION = 0

# Page flags, per RFC 3533.
CONTINUED = 0x01
BEGINNING_OF_STREAM = 0x02
END_OF_STREAM = 0x04
NO_FLAGS = 0x00

# A lacing value is one byte, so a packet is written as however many 255s it
# takes plus a remainder. A remainder is always written, including a zero, which
# is what tells a reader the packet ended rather than continuing onto the next
# page.
MAXIMUM_LACING = 255
MAXIMUM_SEGMENTS = 255

# How many packets a page is aimed at, which is about a second of audio and
# roughly what libsndfile writes. Well under what a page could hold, and
# deliberately: a stream whose audio all fits in one page is read happily by
# this module and by CoreAudio, and rejected as malformed by libsndfile. Two
# pages are accepted everywhere tried, so a clip is always written as at least
# two — see `_grouped`.
PACKETS_PER_PAGE = 50
LEAST_PAGES = 2

# One logical stream per file and nothing multiplexing them, so the serial only
# has to be consistent within the file. Fixed rather than random so that
# encoding the same clip twice gives the same bytes.
STREAM_SERIAL = 0x6D695155  # "miQU"

FIRST_PAGE = 0

OPUS_HEAD_MAGIC = b"OpusHead"
OPUS_TAGS_MAGIC = b"OpusTags"
OPUS_HEAD_VERSION = 1
CHANNEL_MAPPING_FAMILY = 0
NO_OUTPUT_GAIN = 0
NO_COMMENTS = 0
VENDOR = b"miss-quote"

# The samples libopus needs before its output is aligned with its input. A
# decoder is meant to drop this many, and every player except this one does.
# Fixed rather than queried because the CTL that reports it is not on discord.py's
# encoder, and being a few milliseconds generous costs a leading hush nobody can
# hear where being short would clip the first consonant.
PRE_SKIP = 312

# Magic, version, flags, granule position (signed), serial, sequence, checksum,
# segment count. The granule is the only signed field of the three 32-bit-and-up
# ones, which is what an 'i' in the wrong place here costs an afternoon over.
HEADER_FORMAT = "<4sBBqIIIB"
HEADER_BYTES = struct.calcsize(HEADER_FORMAT)
GRANULE_AT_HEADER = 0

READ = "rb"

OPUS_HEAD_FORMAT = "<8sBBHIhB"
LENGTH_FORMAT = "<I"

# Ogg's CRC is its own: the usual polynomial, but neither the input nor the
# output is reflected and there is no final inversion, so `zlib.crc32` cannot be
# used however tempting it looks.
CRC_POLYNOMIAL = 0x04C11DB7
CRC_WIDTH = 32
CRC_TOP_BIT = 1 << (CRC_WIDTH - 1)
CRC_MASK = (1 << CRC_WIDTH) - 1
BYTE_VALUES = 256
BITS_PER_BYTE = 8
CRC_INITIAL = 0
CRC_ZERO = 0


def _crc_table() -> tuple[int, ...]:
    table = []

    for value in range(BYTE_VALUES):
        remainder = value << (CRC_WIDTH - BITS_PER_BYTE)

        for _ in range(BITS_PER_BYTE):
            if remainder & CRC_TOP_BIT:
                remainder = ((remainder << 1) ^ CRC_POLYNOMIAL) & CRC_MASK
            else:
                remainder = (remainder << 1) & CRC_MASK

        table.append(remainder)

    return tuple(table)


CRC_TABLE = _crc_table()


def _crc32(data: bytes) -> int:
    crc = CRC_INITIAL

    for byte in data:
        index = ((crc >> (CRC_WIDTH - BITS_PER_BYTE)) ^ byte) & (BYTE_VALUES - 1)
        crc = ((crc << BITS_PER_BYTE) ^ CRC_TABLE[index]) & CRC_MASK

    return crc


# ── encoding ──────────────────────────────────────


class Encoder:
    """
    Playback PCM in, Opus packets out, a chunk at a time.

    Stateful and single-use, like `PlaybackResampler` and for the same reasons.
    libopus carries state between frames, so an encoder belongs to one clip and
    is discarded with it; and PCM does not arrive in 20 ms multiples, so
    whatever is short of a whole frame is held until the rest of it turns up.

    Streaming rather than batch because the first clip of a phrase is playing
    while it is still being synthesized. An encoder that needed the whole clip
    before it produced anything would move that wait to the front of every cache
    miss.
    """

    __slots__ = ("_encoder", "_remainder")

    def __init__(self) -> None:
        self._encoder = opus.Encoder(
            application=APPLICATION,
            bitrate=BITRATE_KBPS,
            fec=FORWARD_ERROR_CORRECTION,
            expected_packet_loss=EXPECTED_PACKET_LOSS,
            bandwidth=BANDWIDTH,
        )
        self._remainder = bytearray()

    def feed(self, pcm: bytes) -> list[bytes]:
        """Encode whatever whole frames this chunk completes."""
        self._remainder.extend(pcm)
        packets = []

        while len(self._remainder) >= FRAME_BYTES:
            frame = bytes(self._remainder[:FRAME_BYTES])
            del self._remainder[:FRAME_BYTES]
            packets.append(self._encoder.encode(frame, SAMPLES_PER_FRAME))

        return packets

    def flush(self) -> list[bytes]:
        """
        Encode the part-frame left at the end, padded out with silence.

        The player sends one packet per tick whatever is in it, so a short final
        frame would be sent short. Padding keeps the last few milliseconds —
        usually the end of a word — where dropping to a frame boundary would
        lose them.
        """
        if not self._remainder:
            return []

        frame = bytes(self._remainder).ljust(FRAME_BYTES, SILENCE)
        self._remainder.clear()

        return [self._encoder.encode(frame, SAMPLES_PER_FRAME)]


def encoded(pcm: bytes) -> list[bytes]:
    """One Opus packet per 20 ms, for a clip that is already whole."""
    encoder = Encoder()

    return encoder.feed(pcm) + encoder.flush()


class Decoder:
    """
    Opus packets in, playback PCM out, a packet at a time.

    Needed wherever the audio has to be touched before it goes out — which here
    means anything played at less than full volume, since a gain is a
    multiplication and there is nothing to multiply in an encoded packet.

    Streaming for the same reason the encoder is. A clip being played quieter is
    just as likely to be one still coming off the synthesizer, and a decoder
    that wanted the whole clip first would make every such announcement wait for
    the last packet before the first one could be played.

    Stateful and single-use, like everything else on this path: libopus carries
    state between packets, and the encoder's lead-in has to be dropped from the
    front of the clip rather than from the front of each packet.
    """

    __slots__ = ("_decoder", "_skip")

    def __init__(self) -> None:
        self._decoder = opus.Decoder()
        self._skip = PRE_SKIP * CHANNELS * SAMPLE_WIDTH

    def feed(self, packet: bytes) -> bytes:
        """
        Decode one packet, less whatever is left of the lead-in.

        The lead-in is the encoder's, so it sits at the start of the clip and is
        taken off the first packet or two rather than from each of them. What
        comes back then lines up with what went in instead of opening on a few
        milliseconds of hush.
        """
        pcm = self._decoder.decode(packet, fec=False)

        if not self._skip:
            return pcm

        dropped = min(self._skip, len(pcm))
        self._skip -= dropped

        return pcm[dropped:]

    def decode(self, packets: Sequence[bytes]) -> bytes:
        """
        Several packets at once, for a caller decoding off the event loop.

        A packet takes microseconds, so handing each one to a thread separately
        would cost more in scheduling than the decode. This is what a caller
        hands to `asyncio.to_thread`.
        """
        return b"".join(self.feed(packet) for packet in packets)


def decoded(packets: Iterable[bytes]) -> bytes:
    """Playback PCM for a clip that is already whole."""
    decoder = Decoder()

    return b"".join(decoder.feed(packet) for packet in packets)


# ── the container ─────────────────────────────────


def _page(flags: int, sequence: int, granule: int, segments: Sequence[int], body: bytes) -> bytes:
    """
    One Ogg page, with the checksum of the page it is part of.

    The CRC is computed over the whole page with its own field zeroed, which is
    why the header is built twice rather than patched: the second build is the
    one that carries the answer.
    """
    def built(crc: int) -> bytes:
        return (
            struct.pack(
                HEADER_FORMAT,
                OGG_MAGIC,
                OGG_VERSION,
                flags,
                granule,
                STREAM_SERIAL,
                sequence,
                crc,
                len(segments),
            )
            + bytes(segments)
            + body
        )

    return built(_crc32(built(CRC_ZERO)))


def _laced(packet: bytes) -> list[int]:
    """
    A packet's lacing values.

    The remainder is always written, a zero included: a packet whose length is a
    multiple of 255 ends on a full segment, and without the zero a reader would
    take it as continuing onto the next page.
    """
    return [MAXIMUM_LACING] * (len(packet) // MAXIMUM_LACING) + [
        len(packet) % MAXIMUM_LACING
    ]


def _opus_head() -> bytes:
    return struct.pack(
        OPUS_HEAD_FORMAT,
        OPUS_HEAD_MAGIC,
        OPUS_HEAD_VERSION,
        CHANNELS,
        PRE_SKIP,
        SAMPLE_RATE,
        NO_OUTPUT_GAIN,
        CHANNEL_MAPPING_FAMILY,
    )


def _opus_tags() -> bytes:
    return (
        OPUS_TAGS_MAGIC
        + struct.pack(LENGTH_FORMAT, len(VENDOR))
        + VENDOR
        + struct.pack(LENGTH_FORMAT, NO_COMMENTS)
    )


def _grouped(packets: Sequence[bytes]) -> Iterator[list[bytes]]:
    """
    Packets in page-sized runs.

    Bounded twice over. A page cannot carry more than 255 lacing values, which
    is the format's limit and the one that would bite on a long clip; and a clip
    is split across at least two pages however short it is, which is not the
    format's rule but is what readers agree on. A single-page stream is legal as
    far as the specification goes, and libsndfile will not open one.

    A packet never straddles a page. It could — that is what the continued flag
    is for — but at this bitrate a 20 ms packet is a single segment, so the case
    does not arise and handling it would be untested code.
    """
    # Half the clip, so that even a two-packet clip is two pages, and never more
    # than a second of audio in one.
    per_page = min(PACKETS_PER_PAGE, -(-len(packets) // LEAST_PAGES)) or len(packets)

    page: list[bytes] = []
    segments = 0

    for packet in packets:
        needed = len(_laced(packet))
        full = len(page) >= per_page or segments + needed > MAXIMUM_SEGMENTS

        if page and full:
            yield page
            page, segments = [], 0

        page.append(packet)
        segments += needed

    if page:
        yield page


def write(path: Path, packets: Sequence[bytes]) -> None:
    """
    Ogg Opus for a clip, whole or not at all.

    Written to one side and moved into place by the caller; nothing here knows
    about that, but the file this produces is only ever complete.
    """
    head = _opus_head()
    tags = _opus_tags()

    pages = [
        _page(BEGINNING_OF_STREAM, FIRST_PAGE, GRANULE_AT_HEADER, _laced(head), head),
        _page(NO_FLAGS, FIRST_PAGE + 1, GRANULE_AT_HEADER, _laced(tags), tags),
    ]

    granule = PRE_SKIP
    grouped = list(_grouped(packets))

    for index, group in enumerate(grouped):
        granule += len(group) * SAMPLES_PER_FRAME
        last = index == len(grouped) - 1

        pages.append(
            _page(
                END_OF_STREAM if last else NO_FLAGS,
                len(pages),
                granule,
                [lacing for packet in group for lacing in _laced(packet)],
                b"".join(group),
            )
        )

    path.write_bytes(b"".join(pages))


def read(path: Path) -> list[bytes]:
    """
    The audio packets in an Ogg Opus file, without its two headers.

    `OggStream` hands back every packet in the stream, and the first two of them
    describe the rest rather than being any of it.

    The file is checked for its end marker first. A page whose body is short is
    read as a short page rather than refused, so a file truncated by a torn
    write or a half-restored volume would otherwise come back as a clip that
    simply stops — and then be cached and played that way. The end-of-stream
    flag is what says the last page is the last page.
    """
    data = path.read_bytes()

    if not _ends_cleanly(data):
        raise OggError(f"{path} has no end-of-stream page; it is incomplete")

    with path.open(READ) as handle:
        packets = list(OggStream(handle).iter_packets())

    return [
        packet
        for packet in packets
        if not packet.startswith((OPUS_HEAD_MAGIC, OPUS_TAGS_MAGIC))
    ]


def _ends_cleanly(data: bytes) -> bool:
    """
    Whether the last page of a file is a whole page that says it is the last.

    Walked here rather than left to `OggStream`, which reports what it found and
    has no opinion about whether that was all of it.
    """
    offset = 0
    flags = NO_FLAGS

    while offset < len(data):
        if len(data) - offset < HEADER_BYTES:
            return False

        magic, _, flags, _, _, _, _, count = struct.unpack(
            HEADER_FORMAT, data[offset : offset + HEADER_BYTES]
        )
        if magic != OGG_MAGIC:
            return False

        table = data[offset + HEADER_BYTES : offset + HEADER_BYTES + count]
        if len(table) < count:
            return False

        offset += HEADER_BYTES + count + sum(table)

    return offset == len(data) and bool(flags & END_OF_STREAM)


def seconds(packets: Sequence[bytes]) -> float:
    """How long a clip runs, for a log line that would otherwise say bytes."""
    return len(packets) * FRAME_MILLISECONDS / 1000
