"""
Configuration for the Discord voice transcription bot.

Groups settings into logical dataclasses with environment variable loading and validation.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})

BYTES_PER_INT16_SAMPLE = 2
MILLISECONDS_PER_SECOND = 1000

# Playback at whatever loudness the audio was authored or synthesized at, and
# the quietest a scale can ask for. Below silence a factor inverts the waveform
# rather than lowering it, which is not what anybody setting a volume meant.
UNITY_VOLUME = 1.0
SILENT_VOLUME = 0.0

# A fraction is what the code scales audio by; a percentage is what somebody
# setting one in a deployment writes.
PERCENT = 100


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc


def _env_percent(name: str, default: float) -> float:
    """A percentage from the environment, as the fraction everything else uses."""
    return _env_float(name, default) / PERCENT


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default

    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be a boolean, got {value!r}")


# ──────────────────────────────────────────────
# Discord
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class DiscordConfig:
    token: str = field(default_factory=lambda: _env_str("DISCORD_TOKEN", ""))
    command_prefix: str = field(default_factory=lambda: _env_str("COMMAND_PREFIX", "!"))
    autojoin: bool = field(default_factory=lambda: _env_bool("AUTOJOIN", True))


# ──────────────────────────────────────────────
# Audio Pipeline
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class AudioConfig:
    """Audio format for the Discord → ASR pipeline, and back out again."""
    input_sample_rate: int = 48_000   # Discord Opus decoded PCM
    input_channels: int = 2           # Stereo
    output_sample_rate: int = 16_000  # Silero and Wyoming both expect this
    output_channels: int = 1          # Mono
    sample_width: int = BYTES_PER_INT16_SAMPLE

    # Discord's player reads one frame per tick and stops on anything short of a
    # full one, so playback is framed rather than streamed byte by byte.
    playback_frame_ms: int = 20

    # What a clip is scaled by on its way to the player, where 1.0 is however
    # loud the synthesizer rendered it: 0.8 is 20% quieter, 1.2 is 20% louder
    # and clipped rather than wrapped. Floored at silence, since a negative
    # factor inverts a waveform instead of quietening it.
    playback_volume: float = field(
        default_factory=lambda: max(
            SILENT_VOLUME, _env_float("PLAYBACK_VOLUME", UNITY_VOLUME)
        )
    )

    @property
    def playback_sample_rate(self) -> int:
        """Playing into Discord takes back exactly what the gateway delivers."""
        return self.input_sample_rate

    @property
    def playback_channels(self) -> int:
        return self.input_channels

    @property
    def playback_frame_bytes(self) -> int:
        return self.playback_bytes(self.playback_frame_ms)

    def playback_bytes(self, milliseconds: float) -> int:
        """How much playback PCM covers a span of time."""
        samples = int(
            self.playback_sample_rate * milliseconds // MILLISECONDS_PER_SECOND
        )
        return samples * self.playback_channels * self.sample_width


# ──────────────────────────────────────────────
# VAD  (Silero, via onnxruntime)
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class VADConfig:
    """
    Silero VAD is driven through onnxruntime directly against a vendored model
    file; the `silero-vad` package declares torch even in ONNX mode.
    """
    model_path: Path = Path(__file__).parent / "stt" / "models" / "silero_vad.onnx"

    # Silero v5 requires exactly 512 samples @ 16 kHz = 32 ms
    frame_samples: int = 512
    frame_duration_ms: int = 32
    ring_buffer_frames: int = 10  # ~320 ms pre-speech context

    # The v5 graph expects the tail of the previous frame prepended to each
    # input. Feed it a bare frame and it returns near-zero on obvious speech.
    context_samples: int = 64

    # VADIterator hysteresis: speech onset trips at `threshold`, release at the
    # lower `threshold - negative_threshold_delta`, then only after the release
    # has held for `min_silence_duration_ms`.
    threshold: float = 0.5
    negative_threshold_delta: float = 0.15
    min_silence_duration_ms: int = 100
    speech_pad_ms: int = 30

    # A tiny model on a busy event loop is slower with a thread pool than without.
    onnx_intra_op_threads: int = 1

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * BYTES_PER_INT16_SAMPLE

    @property
    def negative_threshold(self) -> float:
        return self.threshold - self.negative_threshold_delta


# ──────────────────────────────────────────────
# STT  (Wyoming)
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class STTConfig:
    host: str = field(default_factory=lambda: _env_str("WYOMING_HOST", "localhost"))
    port: int = field(default_factory=lambda: _env_int("WYOMING_PORT", 10300))
    language: str = field(default_factory=lambda: _env_str("STT_LANGUAGE", "en"))
    max_concurrent: int = field(
        default_factory=lambda: _env_int("MAX_CONCURRENT_TRANSCRIPTIONS", 4)
    )

    # Utterances below this are silence slivers the VAD released early; a round
    # trip would cost more than the transcript is worth.
    min_audio_bytes: int = 3200  # 0.1 s @ 16 kHz int16

    # Bytes of PCM per Wyoming AudioChunk event.
    chunk_bytes: int = 4096

    # A hung ASR must not pin a semaphore slot forever.
    timeout_seconds: float = 30.0


# ──────────────────────────────────────────────
# TTS  (Wyoming)
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class TTSConfig:
    """
    Speech synthesis, for tools that answer out loud.

    A separate host and port from `STTConfig`: recognition and synthesis are
    both Wyoming, but they are two servers and only one of them wants a GPU.
    """

    host: str = field(default_factory=lambda: _env_str("TTS_HOST", "localhost"))
    port: int = field(default_factory=lambda: _env_int("TTS_PORT", 10200))

    # Empty asks the synthesizer for whatever it considers its default, so a
    # deployment with one voice loaded needs no setting at all.
    voice: str = field(default_factory=lambda: _env_str("TTS_VOICE", ""))

    # Budget for a single wait on the synthesizer, not for the whole clip: a
    # server that streams slowly but steadily is healthy, one that goes quiet
    # for this long is not.
    timeout_seconds: float = field(
        default_factory=lambda: _env_float("TTS_TIMEOUT_SECONDS", 30.0)
    )

    # How long the player waits for the next piece of a clip before ending it.
    # Playback begins on the first chunk, so a synthesizer that stalls mid-word
    # leaves a thread holding the channel open until this expires.
    stall_seconds: float = field(
        default_factory=lambda: _env_float("TTS_STALL_SECONDS", 10.0)
    )

    # How much of a phrase to have in hand before a clip starts playing. A
    # synthesizer that renders a phrase whole before sending any of it makes the
    # first chunk the slow one and every chunk after it instant, which is silence
    # in the middle of a clip that opens with a chime. Waiting for this much
    # moves that wait to before the chime, where nobody hears it. Zero plays on
    # the first chunk, as a synthesizer that streams as it renders wants.
    lead_ms: float = field(default_factory=lambda: _env_float("TTS_LEAD_MS", 500.0))

    # Rendered speech, kept so a phrase is only ever synthesized once. An
    # unwritable or unset directory costs the persistence, not the feature.
    cache_directory: Path = field(
        default_factory=lambda: Path(_env_str("TTS_CACHE_DIR", "/cache/tts"))
    )

    # Clips held in memory. The bound exists because what gets synthesized can
    # include a speaker's Discord display name, and those are not a closed set.
    cache_entries: int = field(default_factory=lambda: _env_int("TTS_CACHE_ENTRIES", 256))

    # How long a rendered clip survives on disk without being played. Aged by
    # mtime, which the cache refreshes on every hit, so a phrase still in use
    # stays whatever its age. Any value below 1 disables the reaper. Clips left
    # in the directory by hand are never reaped, whatever this says.
    cache_retention_days: int = field(
        default_factory=lambda: _env_int("TTS_CACHE_RETENTION_DAYS", 90)
    )

    @property
    def caching_enabled(self) -> bool:
        return self.cache_entries >= 1

    @property
    def lead_bytes(self) -> int:
        return audio_cfg.playback_bytes(self.lead_ms)


# ──────────────────────────────────────────────
# Transcripts
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class TranscriptConfig:
    directory: Path = field(
        default_factory=lambda: Path(_env_str("TRANSCRIPT_DIR", "/transcripts"))
    )
    timezone: str = field(default_factory=lambda: _env_str("TZ", "America/Los_Angeles"))

    # Days of transcripts to keep. Any value below 1 disables pruning entirely,
    # so a mis-set variable cannot destroy the archive.
    retention_days: int = field(default_factory=lambda: _env_int("RETENTION_DAYS", -1))

    # How long a channel may sit empty before its transcript is sealed. A
    # channel that refills inside the window is one conversation with a gap in
    # it, not two. Zero seals on disconnect.
    resume_window_seconds: float = field(
        default_factory=lambda: _env_float("SESSION_RESUME_SECONDS", 5.0)
    )

    # One file per connection, named for the moment the bot joined. Colons are
    # legal in the name on POSIX but travel badly, so the time is dash-separated.
    filename_timestamp_format: str = "%Y-%m-%dT%H-%M-%S"
    filename_suffix: str = ".jsonl"

    # Retention needs only the day, and reads it off the front of the name.
    filename_date_format: str = "%Y-%m-%d"
    filename_date_length: int = len("YYYY-MM-DD")

    @property
    def retention_enabled(self) -> bool:
        return self.retention_days >= 1

    @property
    def resume_enabled(self) -> bool:
        return self.resume_window_seconds > 0


# ──────────────────────────────────────────────
# Processing
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class ProcessConfig:
    user_timeout_seconds: int = field(
        default_factory=lambda: _env_int("USER_TIMEOUT_SECONDS", 60)
    )
    speech_flush_timeout_seconds: float = field(
        default_factory=lambda: _env_float("SPEECH_FLUSH_TIMEOUT_SECONDS", 2.0)
    )

    # How often the maintenance task checks for stalled speech and idle users.
    maintenance_interval_seconds: float = 0.5


# ──────────────────────────────────────────────
# Verbal morality
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class MoralityConfig:
    """
    The standing tally of fines, and how quiet a repeat offender gets.

    Where the rest of the tool's settings are per server and live in the mounted
    file, these are per deployment: where the tally is kept, what it is counted
    in, how often it is published, and how the backoff behaves, none of which one
    server should be able to set differently from another.
    """

    # The tally, as JSON, kept across restarts. Mount a volume here; an
    # unwritable path costs the persistence, not the counting.
    credits_file: Path = field(
        default_factory=lambda: Path(_env_str("CREDITS_FILE", "/credits/credits.json"))
    )

    # What a fine is denominated in, in the singular. The plural is grown from
    # it by the same spelling rules the word list uses, so a deployment that
    # fines in something other than credits sets one variable rather than
    # rewriting every server's announcement.
    currency: str = field(default_factory=lambda: _env_str("CREDIT_CURRENCY", "credit"))

    # How often a changed tally is written to disk, and how often the loop that
    # does it wakes at all. Any value at or below zero stops the loop, leaving
    # the tally in memory until shutdown, which still saves it.
    save_interval_seconds: float = field(
        default_factory=lambda: _env_float("CREDITS_SAVE_SECONDS", 5.0)
    )

    # How often a changed tally is published to the voice channel topic — the
    # line the client shows under the channel's name, which `bot.scoreboard` sets
    # as the channel status because a voice channel has no topic. Discord's
    # bucket for it is roughly six a second, so this is a question of how often a
    # tally is worth reading rather than of what the API will tolerate. Any value
    # at or below zero keeps the tally off the channel, and still saves it.
    topic_interval_seconds: float = field(
        default_factory=lambda: _env_float("CREDITS_TOPIC_SECONDS", 10.0)
    )

    # How soon after being fined a speaker is announced as being fined *again*,
    # which is a second wording rather than a second announcement. Short, and
    # deliberately much shorter than the backoff window: it is for the flurry
    # where somebody is still mid-sentence, not for the argument they had five
    # minutes ago. 0 means nothing is ever a repeat.
    repeat_seconds: float = field(
        default_factory=lambda: _env_float("REPEAT_FINE_SECONDS", 5.0)
    )

    # How long a violation counts against how loudly the next one is announced.
    # A sliding window, so a speaker is back to full volume this long after
    # their last one rather than at the top of some fixed period.
    backoff_seconds: float = field(
        default_factory=lambda: _env_float("VOLUME_BACKOFF_DURATION", 300.0)
    )

    # How much of an announcement each violation inside that window takes off.
    # At the default, fifteen of them reach a floor of a quarter. 0 turns the
    # backoff off, there being nothing to take off; anything above 100% would
    # make one violation enough to reach the floor, and anything below 0 would
    # make a repeat offender louder rather than quieter.
    backoff_step: float = field(
        default_factory=lambda: min(
            UNITY_VOLUME,
            max(SILENT_VOLUME, _env_percent("VOLUME_BACKOFF_PERCENT", 5.0)),
        )
    )

    # The quietest an announcement gets, as a fraction of PLAYBACK_VOLUME, once
    # a speaker has earned enough of a backoff to reach it. 0 silences them
    # entirely; 1 turns the backoff off, since there is nowhere to back off to.
    volume_floor: float = field(
        default_factory=lambda: min(
            UNITY_VOLUME,
            max(SILENT_VOLUME, _env_float("VIOLATION_VOLUME_FLOOR", 0.25)),
        )
    )

    @property
    def counting_enabled(self) -> bool:
        """Whether anything happens between startup and shutdown."""
        return self.save_interval_seconds > 0

    @property
    def publishing_enabled(self) -> bool:
        return self.topic_interval_seconds > 0


# ──────────────────────────────────────────────
# Quotes
# ──────────────────────────────────────────────

# The list the image ships with, found relative to this file so a checkout and a
# container agree without either of them being told where they are.
BUNDLED_QUOTES = Path(__file__).resolve().parent / "resources" / "quotes.csv"


@dataclass(frozen=True)
class QuotesConfig:
    """
    Where the `quotes` tool reads its triggers and lines from.

    Per deployment rather than per server, unlike the words a server objects to:
    a film everybody in one channel has seen is one everybody in the next has
    too, and a list per server is a second file to keep current.
    """

    # A CSV of `movie,trigger,quote`. Mount one over this path, or point the
    # variable at it, to say something the shipped list does not.
    file: Path = field(
        default_factory=lambda: Path(_env_str("QUOTES_FILE", str(BUNDLED_QUOTES)))
    )

    # How long a trigger stays spent after it fires. The joke is the
    # recognition, and a channel that keeps saying the same word does not want
    # the same line back each time. Any value at or below zero answers every
    # trigger every time, which is a deployment's own business to want.
    backoff_seconds: float = field(
        default_factory=lambda: _env_float("QUOTE_BACKOFF_SECONDS", 300.0)
    )


# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class LogConfig:
    level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO"))
    format: str = "%(asctime)s │ %(name)-18s │ %(levelname)-7s │ %(message)s"
    date_format: str = "%H:%M:%S"


# ──────────────────────────────────────────────
# Mounted file
# ──────────────────────────────────────────────
CONFIG_FILE_ENV = "CONFIG_FILE"
DEFAULT_CONFIG_FILE = "/config/config.yaml"

SERVERS_KEY = "servers"
ALIAS_KEY = "alias"
USERS_KEY = "users"
TOOLS_KEY = "tools"
TOOL_ENABLED_KEY = "enabled"
TOOL_CONFIG_KEY = "config"

# A tool listed without saying so is off. Enabling one is a decision, and it
# should have to be written down.
TOOL_ENABLED_BY_DEFAULT = False


@dataclass(frozen=True)
class ToolSettings:
    """One server's election into one tool."""

    enabled: bool
    config: Mapping[str, Any]


@dataclass(frozen=True)
class ServerConfig:
    """Everything configured about one server, under its ID."""

    alias: str
    users: Mapping[int, str]
    tools: Mapping[str, ToolSettings]


def _parse_users(
    server_id: int, raw: Any, problems: list[str]
) -> Mapping[int, str]:
    if not raw:
        return {}

    if not isinstance(raw, Mapping):
        problems.append(f"Server {server_id}: '{USERS_KEY}' is not a mapping; ignoring it.")
        return {}

    users: dict[int, str] = {}
    for user, name in raw.items():
        try:
            users[int(user)] = str(name)
        except (TypeError, ValueError):
            problems.append(
                f"Server {server_id}: '{user}' is not a user ID; ignoring that name."
            )

    return users


def _parse_tools(
    server_id: int, raw: Any, problems: list[str]
) -> Mapping[str, ToolSettings]:
    if not raw:
        return {}

    if not isinstance(raw, Mapping):
        problems.append(f"Server {server_id}: '{TOOLS_KEY}' is not a mapping; ignoring it.")
        return {}

    tools: dict[str, ToolSettings] = {}
    for name, settings in raw.items():
        if settings is None:
            settings = {}

        if not isinstance(settings, Mapping):
            problems.append(
                f"Server {server_id}: tool '{name}' is not a mapping; ignoring it."
            )
            continue

        config = settings.get(TOOL_CONFIG_KEY) or {}
        if not isinstance(config, Mapping):
            problems.append(
                f"Server {server_id}: tool '{name}' has a '{TOOL_CONFIG_KEY}' that is "
                "not a mapping; treating it as empty."
            )
            config = {}

        tools[str(name)] = ToolSettings(
            enabled=bool(settings.get(TOOL_ENABLED_KEY, TOOL_ENABLED_BY_DEFAULT)),
            config=dict(config),
        )

    return tools


def _parse_server(
    key: Any, settings: Any, problems: list[str]
) -> tuple[int, ServerConfig] | None:
    """
    Read one server's block, or reject it.

    A malformed entry is dropped rather than raised on: the bot then joins one
    fewer server, which is visible in the startup report and recoverable. The
    alternative is a crash-looping pod over a typo.
    """
    try:
        server_id = int(key)
    except (TypeError, ValueError):
        problems.append(f"'{key}' is not a server ID; ignoring that entry.")
        return None

    if not isinstance(settings, Mapping):
        problems.append(
            f"Server {server_id}: expected a mapping with an '{ALIAS_KEY}'; not joining it."
        )
        return None

    alias = settings.get(ALIAS_KEY)
    if not isinstance(alias, str) or not alias.strip():
        problems.append(f"Server {server_id}: no '{ALIAS_KEY}'; not joining it.")
        return None

    return server_id, ServerConfig(
        alias=alias.strip(),
        users=_parse_users(server_id, settings.get(USERS_KEY), problems),
        tools=_parse_tools(server_id, settings.get(TOOLS_KEY), problems),
    )


@dataclass(frozen=True)
class FileConfig:
    """
    Settings that come from a mounted file rather than the environment.

    These are mappings, which do not survive being flattened into environment
    variables. Read once at startup, so changing the file means restarting the
    pod.

    Servers are identified by ID once, as the key in `servers`, and by a stable
    alias everywhere else. The alias is what transcript paths are named for, so
    renaming a server on Discord changes nothing here.

    Parsing reports rather than raises: `utils.logging` imports this module, so
    nothing here can log. Complaints accumulate in `problems` for the bot to
    report once it has a logger.
    """

    path: Path
    servers: Mapping[int, ServerConfig]
    problems: tuple[str, ...]
    found: bool

    @classmethod
    def load(cls) -> "FileConfig":
        path = Path(_env_str(CONFIG_FILE_ENV, DEFAULT_CONFIG_FILE))

        if not path.is_file():
            return cls(path=path, servers={}, problems=(), found=False)

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        servers: dict[int, ServerConfig] = {}
        problems: list[str] = []

        for key, settings in (raw.get(SERVERS_KEY) or {}).items():
            parsed = _parse_server(key, settings, problems)
            if parsed is not None:
                server_id, server = parsed
                servers[server_id] = server

        return cls(
            path=path,
            servers=servers,
            problems=tuple(problems),
            found=True,
        )

    def knows(self, server_id: int) -> bool:
        """
        Whether the bot may join a server.

        A server absent from `servers` is never joined, so an empty or missing
        file means the bot joins nothing. Recording the wrong server is not
        recoverable; joining none is.
        """
        return server_id in self.servers

    def alias_for(self, server_id: int) -> str | None:
        """The configured alias for a server, or None if it is not known."""
        server = self.servers.get(server_id)
        return None if server is None else server.alias

    def id_for(self, alias: str) -> int | None:
        """
        The server an alias names, for the things that only know the alias.

        Tools are handed the alias rather than the ID, so anything of theirs that
        has to reach Discord — a tally published to a channel topic — has to come
        back the other way. An alias two servers share is already reported as an
        error at startup; here the first entry wins.
        """
        for server_id, server in self.servers.items():
            if server.alias == alias:
                return server_id

        return None

    def name_for(self, server_id: int, user_id: int, reported: str) -> str:
        """
        The configured name for a speaker, or what Discord reported.

        Names are per server: the same person can be known differently in two
        places, and one server's roster should not label another's.
        """
        server = self.servers.get(server_id)
        if server is None:
            return reported

        return server.users.get(user_id, reported)

    def tools_for(self, server_id: int) -> Mapping[str, ToolSettings]:
        """Every tool named for a server, enabled or not."""
        server = self.servers.get(server_id)
        return {} if server is None else server.tools


# ──────────────────────────────────────────────
# Singleton instances (import these directly)
# ──────────────────────────────────────────────
discord_cfg = DiscordConfig()
audio_cfg = AudioConfig()
vad_cfg = VADConfig()
stt_cfg = STTConfig()
tts_cfg = TTSConfig()
transcript_cfg = TranscriptConfig()
process_cfg = ProcessConfig()
morality_cfg = MoralityConfig()
quotes_cfg = QuotesConfig()
log_cfg = LogConfig()
file_cfg = FileConfig.load()
