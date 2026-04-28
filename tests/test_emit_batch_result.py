"""Tests for :func:`hue_cli.output.emit_batch_result`.

The helper renders one batch-line result per call. It's the seam Engineer A's
``batch`` verb (FR-53 / FR-54) writes through; :mod:`hue_cli.output` does not
import the batch module — the dependency points one way only.

Format coverage:

* QUIET → empty string (caller skips the write)
* JSON  → pretty multi-line, sorted keys (matches :func:`emit_json`)
* JSONL → single compact line, sorted keys, no trailing newline
* TEXT  → single human-readable summary line with status / duration / line
"""

from __future__ import annotations

import json

from hue_cli.output import OutputFormat, emit_batch_result


class TestEmitBatchResultJson:
    def test_pretty_json_for_success(self) -> None:
        rec = {
            "line": "set kitchen --brightness 30",
            "ok": True,
            "duration_ms": 12.5,
            "target": "kitchen",
        }
        out = emit_batch_result(rec, OutputFormat.JSON)
        # Pretty-printed → multiline.
        assert "\n" in out
        # Round-trips to the same dict.
        parsed = json.loads(out)
        assert parsed["line"] == rec["line"]
        assert parsed["ok"] is True
        assert parsed["duration_ms"] == 12.5

    def test_pretty_json_for_failure_includes_error(self) -> None:
        rec = {
            "line": "on @nonsense",
            "ok": False,
            "error": "not_found",
            "duration_ms": 8.0,
        }
        parsed = json.loads(emit_batch_result(rec, OutputFormat.JSON))
        assert parsed["ok"] is False
        assert parsed["error"] == "not_found"


class TestEmitBatchResultJsonl:
    def test_compact_single_line(self) -> None:
        rec = {"line": "off plug-1", "ok": True, "duration_ms": 5.0}
        out = emit_batch_result(rec, OutputFormat.JSONL)
        # No trailing newline; caller appends \n when streaming.
        assert "\n" not in out
        assert json.loads(out)["line"] == "off plug-1"

    def test_jsonl_keys_sorted(self) -> None:
        """Sorted keys match :func:`emit_jsonl` so JSONL streams diff cleanly."""
        rec = {"z": 1, "a": 2, "m": 3}
        out = emit_batch_result(rec, OutputFormat.JSONL)
        assert out.index('"a"') < out.index('"m"') < out.index('"z"')


class TestEmitBatchResultText:
    def test_success_status_ok_with_duration(self) -> None:
        rec = {"line": "on @kitchen", "ok": True, "duration_ms": 12.0}
        out = emit_batch_result(rec, OutputFormat.TEXT)
        # Operators eyeball ``ok`` then duration then the original line.
        assert out.startswith("ok")
        assert "12ms" in out
        assert "on @kitchen" in out

    def test_failure_uses_error_string_when_present(self) -> None:
        rec = {
            "line": "on @nonsense",
            "ok": False,
            "error": "not_found",
            "duration_ms": 4.0,
        }
        out = emit_batch_result(rec, OutputFormat.TEXT)
        assert "not_found" in out
        assert "on @nonsense" in out

    def test_failure_without_error_falls_back_to_fail(self) -> None:
        rec = {"line": "on @x", "ok": False, "duration_ms": 1.0}
        out = emit_batch_result(rec, OutputFormat.TEXT)
        assert out.startswith("fail")

    def test_missing_duration_renders_zero_ms(self) -> None:
        """``duration_ms`` is optional — absence renders as ``0ms``, not a crash."""
        rec = {"line": "on @x", "ok": True}
        out = emit_batch_result(rec, OutputFormat.TEXT)
        assert "0ms" in out

    def test_non_numeric_duration_does_not_raise(self) -> None:
        """Defensive: a malformed duration value falls back to 0ms cleanly."""
        rec = {"line": "on @x", "ok": True, "duration_ms": "not-a-number"}
        out = emit_batch_result(rec, OutputFormat.TEXT)
        assert "0ms" in out


class TestEmitBatchResultQuiet:
    def test_returns_empty_string(self) -> None:
        rec = {"line": "on @kitchen", "ok": True}
        assert emit_batch_result(rec, OutputFormat.QUIET) == ""
