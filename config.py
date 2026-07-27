"""
Configuration for the Discord voice transcription bot.

Groups settings into logical dataclasses with environment variable loading and validation.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

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

    filename_date_format: str = "%Y-%m-%d"
    filename_suffix: str = ".jsonl"

    @property
    def retention_enabled(self) -> bool:
        return self.retention_days >= 1


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
# Singleton instances (import these directly)
# ──────────────────────────────────────────────
discord_cfg = DiscordConfig()
audio_cfg = AudioConfig()
vad_cfg = VADConfig()
stt_cfg = STTConfig()
transcript_cfg = TranscriptConfig()
process_cfg = ProcessConfig()
log_cfg = LogConfig()
