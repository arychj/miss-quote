"""
Configuration module for Discord Realtime STT Bot.
Groups settings into logical dataclasses with environment variable loading and validation.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


# ──────────────────────────────────────────────
# Discord
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class DiscordConfig:
    token: str = field(default_factory=lambda: os.getenv("DISCORD_TOKEN", ""))
    command_prefix: str = "!"


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
    model_id: str = "deepdml/faster-whisper-large-v3-turbo-ct2"
    device: str = "cuda"
    compute_type: str = "float16"
    language: str = "ko"
    beam_size: int = 1
    min_audio_bytes: int = 3200  # Ignore audio < 0.1 s (1600 samples × 2 bytes)
    fallback_model_id: str = "base"
    fallback_device: str = "cpu"
    fallback_compute_type: str = "int8"


# ──────────────────────────────────────────────
# Process Management
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class ProcessConfig:
    user_timeout_seconds: int = 60
    result_poll_interval: float = 0.05  # 50 ms async poll


# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class LogConfig:
    level: str = "INFO"
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
