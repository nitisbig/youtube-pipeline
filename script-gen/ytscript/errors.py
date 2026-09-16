"""Typed errors with POSIX-ish exit codes (see sysexits.h).

Every error raised inside the package should subclass :class:`YtScriptError`
so the CLI can map it to a stable exit code instead of dumping a traceback.
"""

from __future__ import annotations


class YtScriptError(Exception):
    """Base class for all ytscript errors."""

    exit_code: int = 1


class UsageError(YtScriptError):
    """Bad command line usage. EX_USAGE."""

    exit_code = 64


class ValidationError(YtScriptError):
    """Model output or user data failed validation. EX_DATAERR."""

    exit_code = 65


class ProviderError(YtScriptError):
    """A provider (LLM / research backend) failed. EX_UNAVAILABLE."""

    exit_code = 69


class ConfigError(YtScriptError):
    """Missing or invalid configuration. EX_CONFIG."""

    exit_code = 78


class OutputError(YtScriptError):
    """Could not write output files. EX_CANTCREAT."""

    exit_code = 73
