"""Output formatting and emission (FR-55..57b).

Four output modes (:class:`OutputFormat`):

* ``TEXT``  — human-readable column-aligned table on tty
* ``JSON``  — pretty multi-line JSON with ``indent=2``, sorted keys (FR-20)
* ``JSONL`` — one compact JSON object per line, sorted keys
* ``QUIET`` — suppress all stdout (FR-57a)

Detection (FR-55):
   tty + no flags    → TEXT
   pipe + no flags   → JSONL
   ``--json``         → JSON  (overrides)
   ``--jsonl``        → JSONL (overrides)
   ``--quiet``        → QUIET (highest precedence)

JSON-validity guard (FR-57b): the :func:`json_validity_guard` context manager
buffers stdout while a verb runs in JSON / JSONL mode. If the verb exits
cleanly the buffer is flushed verbatim. If the verb raises, the guard ensures
stdout is either the empty string or a valid JSON value — never partial /
malformed output. This is the contract that lets shell pipelines like
``hue-cli list lights --json | jq`` survive a mid-emit crash without garbage.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from collections.abc import Iterable, Iterator
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any, TextIO


class OutputFormat(Enum):
    """Output rendering mode for stdout."""

    TEXT = "text"
    JSON = "json"
    JSONL = "jsonl"
    QUIET = "quiet"


def detect(
    force_json: bool,
    force_jsonl: bool,
    quiet: bool,
    *,
    stdout_is_tty: bool,
) -> OutputFormat:
    """Resolve flags + tty state into a single :class:`OutputFormat` (FR-55).

    Precedence: ``--quiet`` > ``--json`` > ``--jsonl`` > tty/pipe heuristic.
    """
    if quiet:
        return OutputFormat.QUIET
    if force_json:
        return OutputFormat.JSON
    if force_jsonl:
        return OutputFormat.JSONL
    return OutputFormat.TEXT if stdout_is_tty else OutputFormat.JSONL


def _to_jsonable(value: Any) -> Any:
    """Convert a dataclass / mapping / scalar into a JSON-safe Python value."""
    if is_dataclass(value) and not isinstance(value, type):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def emit_json(value: Any) -> str:
    """Pretty-print JSON with 2-space indent and sorted keys (FR-20)."""
    payload = _to_jsonable(value)
    return json.dumps(payload, indent=2, sort_keys=True)


def emit_jsonl(records: Iterable[dict[str, Any]]) -> Iterator[str]:
    """Yield one compact, sorted-key JSON line per input record.

    Order of input is preserved. Each yielded string contains no trailing
    newline — the caller appends ``\\n`` when writing to a stream.
    """
    for record in records:
        payload = _to_jsonable(record)
        yield json.dumps(payload, separators=(",", ":"), sort_keys=True)


def emit_text(records: list[dict[str, Any]], columns: list[str]) -> str:
    """Render ``records`` as a column-aligned text table.

    The first row is a header derived from ``columns``. Each subsequent row
    is one record, with columns padded to the per-column max width across
    header + all rows. Returns the table as a single string with one row per
    line and no trailing newline.

    Empty ``records`` returns the header alone.
    """
    if not columns:
        return ""

    def cell(record: dict[str, Any], col: str) -> str:
        v = record.get(col)
        if v is None:
            return "-"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, list):
            return ",".join(str(x) for x in v) if v else "-"
        return str(v)

    rendered: list[list[str]] = [list(columns)]
    for r in records:
        rendered.append([cell(r, c) for c in columns])

    widths = [max(len(row[i]) for row in rendered) for i in range(len(columns))]

    def fmt_row(row: list[str]) -> str:
        return "  ".join(row[i].ljust(widths[i]) for i in range(len(columns))).rstrip()

    return "\n".join(fmt_row(row) for row in rendered)


@contextlib.contextmanager
def json_validity_guard(
    fmt: OutputFormat,
    *,
    stream: TextIO | None = None,
) -> Iterator[TextIO]:
    """Buffer stdout in JSON / JSONL modes; guarantee FR-57b on exception.

    Use as::

        with json_validity_guard(fmt) as out:
            out.write(emit_json(records))
            out.write("\\n")

    On clean exit the buffer is flushed verbatim to the underlying stream
    (or ``sys.stdout`` if ``stream`` is None).

    On exception:
      * ``JSON``  — an empty list ``[]`` followed by a newline is emitted to
        ensure ``jq`` sees parseable JSON.
      * ``JSONL`` — whatever complete JSON lines are already in the buffer
        are flushed; a trailing partial line (if any) is dropped.
      * ``TEXT`` / ``QUIET`` — no transformation; buffer is flushed as-is
        for ``TEXT`` and dropped for ``QUIET``.

    The exception is re-raised after the guard finishes so the caller's
    error-mapping path still runs.
    """
    target = stream if stream is not None else sys.stdout
    buf = io.StringIO()
    try:
        yield buf
    except BaseException:
        if fmt is OutputFormat.JSON:
            target.write("[]\n")
        elif fmt is OutputFormat.JSONL:
            text = buf.getvalue()
            if text:
                # Drop a trailing partial line (no newline at the end).
                if not text.endswith("\n"):
                    last_nl = text.rfind("\n")
                    text = text[: last_nl + 1] if last_nl >= 0 else ""
                target.write(text)
        elif fmt is OutputFormat.TEXT:
            target.write(buf.getvalue())
        # QUIET: write nothing.
        raise
    else:
        if fmt is OutputFormat.QUIET:
            return
        target.write(buf.getvalue())
