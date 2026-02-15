"""
Silero VAD wrapper.

Encapsulates model loading and provides a simple API
for frame-level speech detection.
"""

from __future__ import annotations

import torch
import numpy as np

from config import vad_cfg
from utils.logging import get_logger

logger = get_logger(__name__)


class VADWrapper:
    """Loads Silero VAD and exposes a factory for per-user VADIterators."""

    def __init__(self) -> None:
        logger.info("Loading Silero VAD from '%s'...", vad_cfg.repo_or_dir)
        self._model, self._utils = torch.hub.load(
            repo_or_dir=vad_cfg.repo_or_dir,
            model=vad_cfg.model_name,
            force_reload=False,
            trust_repo=True,
        )
        self._VADIterator = self._utils[3]   # (get_speech_ts, save, read, VADIterator, collect)
        logger.info("Silero VAD loaded successfully.")

    # ── public API ────────────────────────────────

    def create_iterator(self):
        """Return a fresh VADIterator bound to the loaded model."""
        return self._VADIterator(self._model)

    @staticmethod
    def frame_to_tensor(frame_bytes: bytes) -> torch.Tensor:
        """Convert an int16 PCM frame to a float32 torch tensor (normalised)."""
        arr = np.frombuffer(frame_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        return torch.from_numpy(arr)
