"""
Faster-Whisper transcription wrapper.

Handles model loading (with GPU→CPU fallback) and provides
a clean transcription API that returns structured results.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from config import stt_cfg
from utils.logging import get_logger

logger = get_logger(__name__)


class Transcriber:
    """Loads Faster-Whisper and transcribes int16 PCM audio to text."""

    def __init__(self) -> None:
        self._model = self._load_model()

    # ── model loading ─────────────────────────────

    @staticmethod
    def _load_model():
        from faster_whisper import WhisperModel

        try:
            logger.info(
                "Loading Faster-Whisper model '%s' on %s (%s)...",
                stt_cfg.model_id, stt_cfg.device, stt_cfg.compute_type,
            )
            model = WhisperModel(
                stt_cfg.model_id,
                device=stt_cfg.device,
                compute_type=stt_cfg.compute_type,
            )
            logger.info("Faster-Whisper loaded on %s.", stt_cfg.device.upper())
            return model
        except Exception as exc:
            logger.warning(
                "GPU load failed (%s). Falling back to CPU '%s'.",
                exc, stt_cfg.fallback_model_id,
            )
            return WhisperModel(
                stt_cfg.fallback_model_id,
                device=stt_cfg.fallback_device,
                compute_type=stt_cfg.fallback_compute_type,
            )

    # ── public API ────────────────────────────────

    def transcribe(self, user_id: int, audio_bytes: bytes) -> dict[str, Any] | None:
        """
        Transcribe PCM int16 audio to text.

        Parameters
        ----------
        user_id : int
            Discord user ID (included in the result for routing).
        audio_bytes : bytes
            Raw PCM int16 @ 16 kHz mono.

        Returns
        -------
        dict | None
            ``{"user_id": ..., "text": ..., "latency": ...}``
            or *None* if the audio is too short or empty.
        """
        if len(audio_bytes) < stt_cfg.min_audio_bytes:
            return None

        # int16 → float32 normalised
        pcm_i16 = np.frombuffer(audio_bytes, dtype=np.int16)
        pcm_f32 = pcm_i16.astype(np.float32) / 32768.0

        t0 = time.perf_counter()
        try:
            segments, _info = self._model.transcribe(
                pcm_f32,
                language=stt_cfg.language,
                beam_size=stt_cfg.beam_size,
            )
            text = "".join(seg.text for seg in segments).strip()
            elapsed = time.perf_counter() - t0

            if not text:
                return None

            logger.debug(
                "User %s transcribed in %.3fs: %s", user_id, elapsed, text,
            )
            return {
                "user_id": user_id,
                "text": text,
                "latency": f"{elapsed:.3f}s",
            }
        except Exception as exc:
            logger.error("Transcription error for user %s: %s", user_id, exc)
            return None
