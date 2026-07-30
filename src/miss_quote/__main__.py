"""
Discord voice transcription bot — entry point.

Transcription is a network call to a Wyoming ASR server, so it is ordinary
async I/O: one process, one event loop, no worker to supervise.
"""

from __future__ import annotations

import sys

from miss_quote.config import discord_cfg
from miss_quote.utils.logging import get_logger

logger = get_logger(__name__)


def main() -> None:
    if not discord_cfg.token:
        logger.critical("DISCORD_TOKEN is not set. Aborting.")
        sys.exit(1)

    from miss_quote.bot import voice_patches
    from miss_quote.bot.client import STTBot

    voice_patches.apply()

    bot = STTBot()

    try:
        bot.run()
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting down.")


if __name__ == "__main__":
    main()
