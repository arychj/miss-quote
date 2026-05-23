"""
STT Process — the isolated heavy-compute worker.

Runs in its own OS process (via multiprocessing) so the
Discord bot's event loop never blocks on inference.

Pipeline per frame (32 ms):
    IPC Queue → UserStateManager → Silero VAD → Accumulator → Faster-Whisper → Result Queue
"""

from __future__ import annotations

import os
import queue
from multiprocessing import Queue as MPQueue

from config import vad_cfg
from utils.logging import get_logger

logger = get_logger(__name__)


class STTProcessor:
    """Orchestrates VAD + transcription for all active users."""

    def __init__(
        self,
        audio_queue: MPQueue,
        result_queue: MPQueue,
        command_queue: MPQueue,
    ) -> None:
        self._audio_q = audio_queue
        self._result_q = result_queue
        self._cmd_q = command_queue

        # Heavy imports kept local to this process
        from stt.vad import VADWrapper
        from stt.transcriber import Transcriber
        from stt.user_state import UserStateManager

        self._vad = VADWrapper()
        self._transcriber = Transcriber()
        self._users = UserStateManager(vad_iterator_factory=self._vad.create_iterator)

    # ── main loop ─────────────────────────────────

    def run(self) -> None:
        """Block forever, processing audio from the IPC queue."""
        logger.info("STT Processor ready (PID %d).", os.getpid())

        while True:
            self._handle_commands()
            self._process_audio()
            self._flush_stale_speech()
            self._cleanup_inactive_users()

    # ── internals ─────────────────────────────────

    def _handle_commands(self) -> None:
        """Drain the command queue (non-blocking)."""
        while True:
            try:
                cmd, data = self._cmd_q.get_nowait()
            except queue.Empty:
                return
            except Exception as exc:
                logger.error("Command queue error: %s", exc)
                return

            if cmd == "LEAVE":
                state = self._users.remove(data)
                if state:
                    self._flush_state(state, "leave")
            elif cmd == "FLUSH_ALL":
                self._flush_all("flush-all")
            elif cmd == "SHUTDOWN":
                logger.info("Received SHUTDOWN command.")
                self._flush_all("shutdown")
                raise SystemExit(0)

    def _process_audio(self) -> None:
        """Pull one chunk from the audio queue and feed it through VAD."""
        try:
            user_id, pcm_data = self._audio_q.get(timeout=0.1)
        except queue.Empty:
            return
        except Exception as exc:
            logger.error("Audio queue error: %s", exc)
            return

        state = self._users.get_or_create(user_id)
        state.raw_buffer.extend(pcm_data)

        frame_bytes = vad_cfg.frame_bytes

        while len(state.raw_buffer) >= frame_bytes:
            # Slice one frame
            frame = bytes(state.raw_buffer[:frame_bytes])
            del state.raw_buffer[:frame_bytes]

            # VAD inference
            tensor = self._vad.frame_to_tensor(frame)
            state.vad_iterator(tensor, return_seconds=True)
            is_speech = state.vad_iterator.triggered

            if is_speech:
                # On speech onset, prepend the ring-buffer context
                if not state.is_speaking:
                    for prev_frame in state.ring_buffer.drain():
                        state.speech_buffer.extend(prev_frame)
                state.speech_buffer.extend(frame)
            else:
                if state.is_speaking:
                    # End-of-speech → transcribe
                    audio_data = state.reset_speech()
                    result = self._transcriber.transcribe(user_id, bytes(audio_data))
                    if result:
                        self._put_result(result)
                # Keep recent silence in ring buffer
                state.ring_buffer.append(frame)

    def _flush_state(self, state, reason: str) -> None:
        """Transcribe any buffered speech before discarding a user state."""
        if not state.is_speaking:
            return

        audio_data = state.reset_speech()
        result = self._transcriber.transcribe(state.user_id, bytes(audio_data))
        if result:
            logger.info("Flushed user %s speech due to %s.", state.user_id, reason)
            self._put_result(result)

    def _flush_all(self, reason: str) -> None:
        for state in self._users.remove_all():
            self._flush_state(state, reason)

    def _flush_stale_speech(self) -> None:
        for state in self._users.stale_speech_states():
            self._flush_state(state, "speech inactivity")

    def _cleanup_inactive_users(self) -> None:
        for state in self._users.cleanup_inactive():
            self._flush_state(state, "user timeout")

    def _put_result(self, result: dict) -> None:
        try:
            self._result_q.put_nowait(result)
        except queue.Full:
            logger.warning(
                "Result queue full; dropped transcription for user %s.",
                result.get("user_id"),
            )
        except Exception as exc:
            logger.error("Result queue error: %s", exc)


# ── Process entry point ─────────────────────────

def run_stt_process(
    audio_queue: MPQueue,
    result_queue: MPQueue,
    command_queue: MPQueue,
) -> None:
    """
    Top-level function spawned by multiprocessing.Process.

    All heavy model loading happens here (inside the child process)
    to avoid CUDA context issues.
    """
    try:
        processor = STTProcessor(audio_queue, result_queue, command_queue)
        processor.run()
    except SystemExit:
        logger.info("STT process exiting gracefully.")
    except KeyboardInterrupt:
        logger.info("STT process interrupted.")
    except Exception as exc:
        logger.critical("STT process crashed: %s", exc, exc_info=True)
        raise
