"""
Configuration module for Discord Realtime STT Bot.
Groups settings into logical dataclasses with environment variable loading and validation.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


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


# ──────────────────────────────────────────────
# Discord
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class DiscordConfig:
    token: str = field(default_factory=lambda: _env_str("DISCORD_TOKEN", ""))
    command_prefix: str = field(default_factory=lambda: _env_str("COMMAND_PREFIX", "!"))


# ──────────────────────────────────────────────
# Audio Pipeline
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class AudioConfig:
    """Audio format constants for the Discord → STT pipeline."""
    input_sample_rate: int = 48_000   # Discord Opus decoded PCM
    input_channels: int = 2           # Stereo
    output_sample_rate: int = 16_000  # Whisper expected rate
    output_channels: int = 1          # Mono
    sample_width: int = 2             # int16 = 2 bytes per sample


# ──────────────────────────────────────────────
# VAD  (Silero)
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class VADConfig:
    repo_or_dir: str = "snakers4/silero-vad"
    model_name: str = "silero_vad"
    # Silero requires 512 samples @ 16 kHz = 32 ms
    frame_samples: int = 512
    frame_duration_ms: int = 32
    ring_buffer_frames: int = 10  # ~320 ms pre-speech context

    @property
    def frame_bytes(self) -> int:
        """Bytes per VAD frame (512 samples × 2 bytes/sample)."""
        return self.frame_samples * 2


# ──────────────────────────────────────────────
# STT  (Faster-Whisper)
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class STTConfig:
    model_id: str = field(
        default_factory=lambda: _env_str(
            "STT_MODEL_ID",
            "deepdml/faster-whisper-large-v3-turbo-ct2",
        )
    )
    device: str = field(default_factory=lambda: _env_str("STT_DEVICE", "cuda"))
    compute_type: str = field(
        default_factory=lambda: _env_str("STT_COMPUTE_TYPE", "float16")
    )
    language: str = field(default_factory=lambda: _env_str("STT_LANGUAGE", "ko"))
    beam_size: int = field(default_factory=lambda: _env_int("STT_BEAM_SIZE", 1))
    min_audio_bytes: int = field(
        default_factory=lambda: _env_int("STT_MIN_AUDIO_BYTES", 3200)
    )  # Ignore audio < 0.1 s (1600 samples × 2 bytes)
    fallback_model_id: str = field(
        default_factory=lambda: _env_str("STT_FALLBACK_MODEL_ID", "base")
    )
    fallback_device: str = field(
        default_factory=lambda: _env_str("STT_FALLBACK_DEVICE", "cpu")
    )
    fallback_compute_type: str = field(
        default_factory=lambda: _env_str("STT_FALLBACK_COMPUTE_TYPE", "int8")
    )


# ──────────────────────────────────────────────
# Process Management
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class ProcessConfig:
    user_timeout_seconds: int = field(
        default_factory=lambda: _env_int("USER_TIMEOUT_SECONDS", 60)
    )
    speech_flush_timeout_seconds: float = field(
        default_factory=lambda: _env_float("SPEECH_FLUSH_TIMEOUT_SECONDS", 2.0)
    )
    result_poll_interval: float = field(
        default_factory=lambda: _env_float("RESULT_POLL_INTERVAL", 0.05)
    )  # 50 ms async poll
    audio_queue_maxsize: int = field(
        default_factory=lambda: _env_int("AUDIO_QUEUE_MAXSIZE", 512)
    )
    result_queue_maxsize: int = field(
        default_factory=lambda: _env_int("RESULT_QUEUE_MAXSIZE", 128)
    )
    command_queue_maxsize: int = field(
        default_factory=lambda: _env_int("COMMAND_QUEUE_MAXSIZE", 128)
    )
    stt_health_check_interval: float = field(
        default_factory=lambda: _env_float("STT_HEALTH_CHECK_INTERVAL", 2.0)
    )
    shutdown_timeout_seconds: float = field(
        default_factory=lambda: _env_float("SHUTDOWN_TIMEOUT_SECONDS", 10.0)
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
# Singleton instances (import these directly)
# ──────────────────────────────────────────────
discord_cfg = DiscordConfig()
audio_cfg = AudioConfig()
vad_cfg = VADConfig()
stt_cfg = STTConfig()
process_cfg = ProcessConfig()
log_cfg = LogConfig()
