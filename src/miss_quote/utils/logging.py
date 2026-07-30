"""
Structured logging configuration.

Usage:
    from miss_quote.utils.logging import get_logger
    logger = get_logger(__name__)
    logger.info("Hello %s", "world")
"""

import logging
import sys
from miss_quote.config import log_cfg

_configured = False


def _configure_root() -> None:
    """Apply the global log format once."""
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_cfg.level.upper(), logging.INFO))

    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(fmt=log_cfg.format, datefmt=log_cfg.date_format)
        )
        root.addHandler(handler)

    # Suppress noisy third-party loggers. The two voice_recv loggers report
    # traffic they went on to handle correctly — an RTCP sender report, which is
    # what a receiver gets, and the voice gateway's `seq` field, which discord.py
    # consumes for resume — at INFO, once a second between them. Their real
    # failures are logged at WARNING and above and still come through.
    for noisy in (
        "urllib3",
        "websockets",
        "discord.gateway",
        "discord.client",
        "discord.ext.voice_recv.reader",
        "discord.ext.voice_recv.gateway",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a module-level logger with the shared config."""
    _configure_root()
    return logging.getLogger(name)
