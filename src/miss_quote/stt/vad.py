"""
Silero VAD driven directly through onnxruntime.

The upstream `silero-vad` package declares torch even for its ONNX path, so the
model file is vendored and its `VADIterator` hysteresis reimplemented here.

One `SileroVAD` owns the shared inference session; each speaker gets its own
`VADIterator`, because the model is recurrent and its state is per-stream.
"""

from __future__ import annotations

import numpy as np
import onnxruntime as ort

from miss_quote.config import audio_cfg, vad_cfg, MILLISECONDS_PER_SECOND
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)

INT16_FULL_SCALE = 32768.0

# Shapes and dtypes fixed by the Silero v5 ONNX graph.
STATE_SHAPE = (2, 1, 128)
STATE_DTYPE = np.float32
BATCH_AXIS = 0
FIRST_ELEMENT = 0

INPUT_AUDIO = "input"
INPUT_STATE = "state"
INPUT_SAMPLE_RATE = "sr"


class VADIterator:
    """
    Per-speaker speech/silence state machine.

    Onset trips as soon as speech probability reaches the threshold. Release is
    deliberately harder: probability must fall below the lower negative
    threshold *and* stay there for `min_silence_duration_ms`, so a pause between
    words does not chop one utterance into several.

    `triggered` is the public signal; the consumer reads it after each frame.
    """

    def __init__(self, session: ort.InferenceSession) -> None:
        self._session = session
        self._sample_rate = np.array(audio_cfg.output_sample_rate, dtype=np.int64)

        self._min_silence_samples = int(
            audio_cfg.output_sample_rate
            * vad_cfg.min_silence_duration_ms
            / MILLISECONDS_PER_SECOND
        )
        self._speech_pad_samples = int(
            audio_cfg.output_sample_rate
            * vad_cfg.speech_pad_ms
            / MILLISECONDS_PER_SECOND
        )

        self.reset_states()

    def reset_states(self) -> None:
        self._state = np.zeros(STATE_SHAPE, dtype=STATE_DTYPE)
        self._context = np.zeros((1, vad_cfg.context_samples), dtype=STATE_DTYPE)
        self.triggered = False
        self._temp_end = 0
        self._current_sample = 0

    def __call__(self, frame: np.ndarray, return_seconds: bool = False) -> dict | None:
        """
        Feed one frame of normalised float32 audio.

        Returns a `{"start": ...}` or `{"end": ...}` marker on a transition and
        None otherwise, mirroring Silero's own iterator.
        """
        window_size = frame.shape[-1]
        self._current_sample += window_size

        speech_probability = self._infer(frame)

        if speech_probability >= vad_cfg.threshold and self._temp_end:
            self._temp_end = 0

        if speech_probability >= vad_cfg.threshold and not self.triggered:
            self.triggered = True
            speech_start = (
                self._current_sample - self._speech_pad_samples - window_size
            )
            return {"start": self._format_position(speech_start, return_seconds)}

        if speech_probability < vad_cfg.negative_threshold and self.triggered:
            if not self._temp_end:
                self._temp_end = self._current_sample

            if self._current_sample - self._temp_end < self._min_silence_samples:
                return None

            speech_end = self._temp_end + self._speech_pad_samples - window_size
            self._temp_end = 0
            self.triggered = False
            return {"end": self._format_position(speech_end, return_seconds)}

        return None

    def _infer(self, frame: np.ndarray) -> float:
        # The graph scores `context + frame` together; the context is the tail of
        # the previous frame, so consecutive calls see a continuous signal.
        windowed = np.concatenate([self._context, frame.reshape(1, -1)], axis=1)

        probabilities, self._state = self._session.run(
            None,
            {
                INPUT_AUDIO: windowed,
                INPUT_STATE: self._state,
                INPUT_SAMPLE_RATE: self._sample_rate,
            },
        )
        self._context = windowed[:, -vad_cfg.context_samples :]

        return float(probabilities[BATCH_AXIS][FIRST_ELEMENT])

    @staticmethod
    def _format_position(sample: int, return_seconds: bool) -> float | int:
        if not return_seconds:
            return sample
        return round(sample / audio_cfg.output_sample_rate, 1)


class SileroVAD:
    """Owns the shared ONNX session and hands out per-speaker iterators."""

    def __init__(self) -> None:
        logger.info("Loading Silero VAD from '%s'...", vad_cfg.model_path)

        options = ort.SessionOptions()
        options.intra_op_num_threads = vad_cfg.onnx_intra_op_threads
        options.inter_op_num_threads = vad_cfg.onnx_intra_op_threads

        self._session = ort.InferenceSession(
            str(vad_cfg.model_path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        logger.info("Silero VAD loaded successfully.")

    def create_iterator(self) -> VADIterator:
        """Return a fresh iterator with its own recurrent state."""
        return VADIterator(self._session)

    @staticmethod
    def frame_to_array(frame_bytes: bytes) -> np.ndarray:
        """Convert an int16 PCM frame to normalised float32."""
        samples = np.frombuffer(frame_bytes, dtype=np.int16)
        return samples.astype(np.float32) / INT16_FULL_SCALE
