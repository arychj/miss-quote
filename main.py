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
import os
import queue
import signal
import sys
import threading

from config import discord_cfg, process_cfg
from utils.logging import get_logger

logger = get_logger(__name__)


def main() -> None:
    multiprocessing.freeze_support()  # Required on Windows

    if not discord_cfg.token:
        logger.critical("DISCORD_TOKEN is not set. Aborting before STT startup.")
        sys.exit(1)

    # ── IPC Queues ───────────────────────────────
    audio_queue = multiprocessing.Queue(maxsize=process_cfg.audio_queue_maxsize)
    result_queue = multiprocessing.Queue(maxsize=process_cfg.result_queue_maxsize)
    command_queue = multiprocessing.Queue(maxsize=process_cfg.command_queue_maxsize)

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

    shutdown_requested = threading.Event()

    # ── Graceful shutdown ────────────────────────
    def _request_stt_shutdown() -> None:
        try:
            command_queue.put_nowait(("SHUTDOWN", None))
        except queue.Full:
            logger.warning("Command queue is full; terminating STT process.")
        except Exception as exc:
            logger.warning("Could not send STT shutdown command: %s", exc)

        stt_proc.join(timeout=process_cfg.shutdown_timeout_seconds)
        if stt_proc.is_alive():
            logger.warning("STT process did not exit in time; terminating.")
            stt_proc.terminate()
            stt_proc.join(timeout=5)
        if stt_proc.is_alive():
            logger.warning("STT process ignored terminate; killing.")
            stt_proc.kill()
            stt_proc.join(timeout=5)

    def _shutdown(signum: int, _frame) -> None:
        sig_name = signal.Signals(signum).name
        if shutdown_requested.is_set():
            logger.warning("Received %s during shutdown; forcing exit.", sig_name)
            os._exit(1)

        shutdown_requested.set()
        logger.info("Received %s; shutting down...", sig_name)
        _request_stt_shutdown()
        logger.info("Cleanup complete. Goodbye.")
        sys.exit(0)

    def _monitor_stt_process() -> None:
        while not shutdown_requested.wait(process_cfg.stt_health_check_interval):
            if stt_proc.is_alive():
                continue
            logger.critical(
                "STT process exited unexpectedly with code %s; stopping bot.",
                stt_proc.exitcode,
            )
            os.kill(os.getpid(), signal.SIGTERM)
            return

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    threading.Thread(
        target=_monitor_stt_process,
        name="STT-HealthMonitor",
        daemon=True,
    ).start()

    # ── Start Discord bot (blocking) ─────────────
    from bot.client import STTBot

    bot = STTBot(audio_queue, result_queue, command_queue)

    try:
        bot.run()
    except KeyboardInterrupt:
        _shutdown(signal.SIGINT, None)
    finally:
        if not shutdown_requested.is_set():
            shutdown_requested.set()
            if stt_proc.is_alive():
                logger.info("Stopping STT process...")
                _request_stt_shutdown()


if __name__ == "__main__":
    main()
