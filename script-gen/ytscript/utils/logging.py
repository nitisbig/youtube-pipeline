"""Logging setup.

Rule of the tool: **stdout is data, stderr is chatter.** That keeps the CLI
pipe-friendly (`ytscript ... --json | jq .`).
"""

from __future__ import annotations

import logging
import sys

LOGGER_NAME = "ytscript"


def setup_logging(verbosity: int = 0, quiet: bool = False) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.propagate = False

    if quiet:
        level = logging.ERROR
    elif verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("ytscript: %(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
