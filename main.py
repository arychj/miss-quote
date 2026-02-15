"""
Discord Realtime STT Bot — Entry Point

Orchestrates:
    1. IPC queue creation
    2. STT process spawn (isolated for GPU inference)
    3. Discord bot startup
    4. Graceful shutdown on SIGINT / Ctrl+C
"""

from __future__ import annotations

import multiprocessing
import signal
import sys

from utils.logging import get_logger

logger = get_logger(__name__)


def main() -> None:
    multiprocessing.freeze_support()  # Required on Windows

    # ── IPC Queues ───────────────────────────────
    audio_queue = multiprocessing.Queue()
    result_queue = multiprocessing.Queue()
    command_queue = multiprocessing.Queue()

    # ── Spawn STT worker ─────────────────────────
    from stt.processor import run_stt_process

    stt_proc = multiprocessing.Process(
        target=run_stt_process,
        args=(audio_queue, result_queue, command_queue),
        name="STT-Worker",
        daemon=True,
    )
    stt_proc.start()
    logger.info("STT process spawned (PID %d).", stt_proc.pid)

    # ── Graceful shutdown ────────────────────────
    def _shutdown(signum: int, _frame) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s — shutting down...", sig_name)
        # Ask the STT process to exit cleanly
        try:
            command_queue.put(("SHUTDOWN", None))
        except Exception:
            pass
        stt_proc.terminate()
        stt_proc.join(timeout=5)
        if stt_proc.is_alive():
            logger.warning("STT process did not exit in time — killing.")
            stt_proc.kill()
        logger.info("Cleanup complete. Goodbye.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── Start Discord bot (blocking) ─────────────
    from bot.client import STTBot

    bot = STTBot(audio_queue, result_queue, command_queue)

    try:
        bot.run()
    except KeyboardInterrupt:
        _shutdown(signal.SIGINT, None)
    finally:
        if stt_proc.is_alive():
            logger.info("Terminating STT process...")
            stt_proc.terminate()
            stt_proc.join(timeout=5)


if __name__ == "__main__":
    main()
