"""Verbose-flag and file-logging plumbing per SRD §7.3.

Behaviour:

* ``-v`` (verbose=1) sets the ``hue_cli`` logger to ``INFO``.
* ``-vv`` (verbose=2) sets it to ``DEBUG``.
* ``[logging] file = "<path>"`` in the loaded config (FR-7.3) attaches a
  file handler that emits the same single-line JSON log records the stderr
  handler emits — this is the file is **additive** to stderr; both fire.
* No file rotation in v1 — ``logrotate`` is the answer per §7.3.

The setup is idempotent: a sentinel attribute on the root ``hue_cli`` logger
stops second invocations from double-attaching handlers (which would happen
when pytest imports the cli module repeatedly across tests, or when batch
mode re-enters the top-level callback).
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

_SENTINEL_ATTR = "_hue_cli_logging_configured"
_LOGGER_NAME = "hue_cli"


class JsonLineFormatter(logging.Formatter):
    """Format a log record as a single-line JSON object (§7.3)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


def _verbose_to_level(verbose: int) -> int:
    """Translate ``-v`` count to a stdlib log level."""
    if verbose >= 2:
        return logging.DEBUG
    if verbose == 1:
        return logging.INFO
    return logging.WARNING


def setup_logging(
    verbose: int,
    file_path: str | None,
) -> None:
    """Configure the ``hue_cli`` logger for ``-v``/``-vv`` and optional file output.

    Idempotent: a second call with different parameters re-applies the level
    on the existing logger and adds a file handler only if one for the new
    path isn't already attached. Does not detach previous handlers — pytest
    captures stderr and the file path is stable within a test.

    ``file_path`` may contain a leading ``~`` which is expanded; missing
    parent directories cause :class:`OSError` to bubble up to the caller —
    invalid log paths are an operator-visible config problem, not a silent
    fallback. The cli wrapper traps this and emits a clear error.
    """

    logger = logging.getLogger(_LOGGER_NAME)
    level = _verbose_to_level(verbose)
    logger.setLevel(level)

    # Stderr handler (always present). On first call, attach a single fresh
    # one so verbose flags actually reach the operator. Subsequent calls just
    # update the level on the existing handler — no duplicates.
    if not getattr(logger, _SENTINEL_ATTR, False):
        stderr_handler = logging.StreamHandler(stream=sys.stderr)
        stderr_handler.setFormatter(JsonLineFormatter())
        stderr_handler.setLevel(level)
        logger.addHandler(stderr_handler)
        logger.propagate = False
        setattr(logger, _SENTINEL_ATTR, True)
    else:
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(level)

    # Optional file handler: attach at most once per resolved path.
    if file_path:
        resolved = Path(file_path).expanduser()
        already_attached = any(
            isinstance(h, logging.FileHandler) and Path(h.baseFilename) == resolved
            for h in logger.handlers
        )
        if not already_attached:
            file_handler = logging.FileHandler(resolved, mode="a", encoding="utf-8")
            file_handler.setFormatter(JsonLineFormatter())
            file_handler.setLevel(level)
            logger.addHandler(file_handler)
        else:
            for handler in logger.handlers:
                if (
                    isinstance(handler, logging.FileHandler)
                    and Path(handler.baseFilename) == resolved
                ):
                    handler.setLevel(level)


def reset_for_tests() -> None:
    """Tear down hue_cli-logger state between tests.

    Removes every attached handler (closing file handles cleanly) and clears
    the sentinel so the next ``setup_logging`` call re-attaches a fresh
    stderr handler. Test-only: production code runs setup once per CLI
    invocation and inherits one stable configuration.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()
    if hasattr(logger, _SENTINEL_ATTR):
        delattr(logger, _SENTINEL_ATTR)
    logger.setLevel(logging.WARNING)
