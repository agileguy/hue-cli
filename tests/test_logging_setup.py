"""Tests for `hue_cli.logging_setup` per SRD §7.3.

Covers:

* ``-v`` / ``-vv`` translation to log levels.
* ``[logging] file = "..."`` writes JSON-formatted log lines to the file.
* Without ``[logging] file``, no file is created and no errors raised.
* File handler is append-mode (subsequent runs preserve prior content).
* The setup is idempotent — repeated calls don't double-attach handlers.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from hue_cli import logging_setup


@pytest.fixture(autouse=True)
def _reset_logger() -> None:
    """Clean the hue_cli logger before AND after each test."""
    logging_setup.reset_for_tests()
    yield
    logging_setup.reset_for_tests()


# --- Verbose level translation ----------------------------------------------


class TestVerboseLevel:
    def test_v_zero_is_warning(self) -> None:
        logging_setup.setup_logging(verbose=0, file_path=None)
        assert logging.getLogger("hue_cli").level == logging.WARNING

    def test_v_one_is_info(self) -> None:
        logging_setup.setup_logging(verbose=1, file_path=None)
        assert logging.getLogger("hue_cli").level == logging.INFO

    def test_vv_is_debug(self) -> None:
        logging_setup.setup_logging(verbose=2, file_path=None)
        assert logging.getLogger("hue_cli").level == logging.DEBUG

    def test_more_than_two_caps_at_debug(self) -> None:
        logging_setup.setup_logging(verbose=5, file_path=None)
        assert logging.getLogger("hue_cli").level == logging.DEBUG


# --- File logging -----------------------------------------------------------


class TestFileLogging:
    def test_file_path_unset_creates_no_file(self, tmp_path: Path) -> None:
        # Sanity: nothing in the dir to start.
        assert list(tmp_path.iterdir()) == []
        logging_setup.setup_logging(verbose=1, file_path=None)
        logging.getLogger("hue_cli").info("anything")
        # Still nothing in the directory.
        assert list(tmp_path.iterdir()) == []

    def test_file_path_set_writes_json_line_with_message(self, tmp_path: Path) -> None:
        log_file = tmp_path / "hue.log"
        logging_setup.setup_logging(verbose=1, file_path=str(log_file))
        logging.getLogger("hue_cli").info("hello")

        # Flush handlers so the message lands on disk before we read.
        for handler in logging.getLogger("hue_cli").handlers:
            handler.flush()

        contents = log_file.read_text(encoding="utf-8").splitlines()
        assert len(contents) >= 1
        last = json.loads(contents[-1])
        assert last["message"] == "hello"
        assert last["level"] == "INFO"
        assert last["logger"] == "hue_cli"
        # Single-line JSON invariant: no internal newlines per record.
        assert "\n" not in contents[-1]

    def test_file_logging_appends_across_calls(self, tmp_path: Path) -> None:
        log_file = tmp_path / "hue.log"

        logging_setup.setup_logging(verbose=1, file_path=str(log_file))
        logging.getLogger("hue_cli").info("first")
        for handler in logging.getLogger("hue_cli").handlers:
            handler.flush()
        first_contents = log_file.read_text(encoding="utf-8")
        assert "first" in first_contents

        # Reset state to simulate a fresh CLI invocation; the file path is
        # the same so the second invocation should append, not truncate.
        logging_setup.reset_for_tests()

        logging_setup.setup_logging(verbose=1, file_path=str(log_file))
        logging.getLogger("hue_cli").info("second")
        for handler in logging.getLogger("hue_cli").handlers:
            handler.flush()
        full_contents = log_file.read_text(encoding="utf-8")
        # Append mode: prior line still there, new line added.
        assert "first" in full_contents
        assert "second" in full_contents

    def test_file_logging_below_level_is_filtered(self, tmp_path: Path) -> None:
        # At verbose=0 (WARNING) an INFO record SHALL not appear in the file.
        log_file = tmp_path / "hue.log"
        logging_setup.setup_logging(verbose=0, file_path=str(log_file))
        logging.getLogger("hue_cli").info("filtered")
        for handler in logging.getLogger("hue_cli").handlers:
            handler.flush()
        if log_file.exists():
            assert "filtered" not in log_file.read_text(encoding="utf-8")
        # If file doesn't exist that's also acceptable — INFO never fired.

    def test_setup_is_idempotent_no_double_handlers(self, tmp_path: Path) -> None:
        log_file = tmp_path / "hue.log"
        logging_setup.setup_logging(verbose=1, file_path=str(log_file))
        first_handler_count = len(logging.getLogger("hue_cli").handlers)
        # Re-invoke with the same params; handler count must not grow.
        logging_setup.setup_logging(verbose=1, file_path=str(log_file))
        logging_setup.setup_logging(verbose=1, file_path=str(log_file))
        assert len(logging.getLogger("hue_cli").handlers) == first_handler_count


# --- JSON formatter ---------------------------------------------------------


class TestJsonFormatter:
    def test_emits_required_keys(self) -> None:
        formatter = logging_setup.JsonLineFormatter()
        record = logging.LogRecord(
            name="hue_cli",
            level=logging.INFO,
            pathname=__file__,
            lineno=42,
            msg="msg %s",
            args=("arg",),
            exc_info=None,
        )
        rendered = formatter.format(record)
        # Single line, parseable JSON.
        parsed = json.loads(rendered)
        for required in ("ts", "level", "logger", "message"):
            assert required in parsed
        # Message interpolation worked.
        assert parsed["message"] == "msg arg"
