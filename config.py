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
    """Audio format constants for the Discord → ASR pipeline."""
    input_sample_rate: int = 48_000   # Discord Opus decoded PCM
    input_channels: int = 2           # Stereo
    output_sample_rate: int = 16_000  # Silero and Wyoming both expect this
    output_channels: int = 1          # Mono
    sample_width: int = BYTES_PER_INT16_SAMPLE

    # Discord's player reads one frame per tick and stops on anything short of a
    # full one, so playback is framed rather than streamed byte by byte.
    playback_frame_ms: int = 20

    @property
    def playback_sample_rate(self) -> int:
        """Playing into Discord takes back exactly what the gateway delivers."""
        return self.input_sample_rate

    @property
    def playback_channels(self) -> int:
        return self.input_channels

    @property
    def playback_frame_bytes(self) -> int:
        samples = (
            self.playback_sample_rate * self.playback_frame_ms // MILLISECONDS_PER_SECOND
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

    # Rendered speech, kept so a phrase is only ever synthesized once. An
    # unwritable or unset directory costs the persistence, not the feature.
    cache_directory: Path = field(
        default_factory=lambda: Path(_env_str("TTS_CACHE_DIR", "/cache/tts"))
    )

    # Clips held in memory. The bound exists because what gets synthesized can
    # include a speaker's Discord display name, and those are not a closed set.
    cache_entries: int = field(default_factory=lambda: _env_int("TTS_CACHE_ENTRIES", 256))

    @property
    def caching_enabled(self) -> bool:
        return self.cache_entries >= 1


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
log_cfg = LogConfig()
file_cfg = FileConfig.load()
