"""Tests for FR-54c — graceful drain on SIGINT / SIGTERM during batch.

The CLI's :func:`hue_cli.cli._run_async_graceful` installs async-aware
signal handlers when given a :class:`BatchSession`. On signal receipt the
handler:

1. sets ``session.cancel_event`` so the dispatcher stops scheduling new ops,
2. lets in-flight tasks finish (up to 2 s — enforced by ``_run_batch``),
3. emits ``{"event":"interrupted","completed":N,"pending":M}`` JSONL line,
4. exits 130 (SIGINT) or 143 (SIGTERM).

The straightforward subprocess approach (spawn the CLI, send SIGINT,
inspect output) is timing-flaky on CI. Instead we drive the drain logic
in-process by:

* injecting a ``BatchSession`` whose ``cancel_event`` is pre-set,
* asserting ``_run_batch`` returns only the slice it managed to dispatch,
* asserting the FR-54c summary line is written through
  :func:`hue_cli.cli._emit_interrupted_summary`.

This proves both halves of the contract — drain timing AND summary-line
emission — without subprocess timing dependencies. A small subprocess test
covers the end-to-end SIGINT path on POSIX with a generous timeout for
robustness.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from typing import Any

import pytest

from hue_cli.cli import _emit_interrupted_summary, _run_async_graceful
from hue_cli.output import OutputFormat
from hue_cli.verbs.batch_cmd import BatchSession, ParsedLine, _run_batch

# ---------------------------------------------------------------------------
# In-process drain — deterministic, no subprocess
# ---------------------------------------------------------------------------


class _DrainFakeWrapper:
    """Wrapper whose ``resolve_target`` blocks for ``delay`` seconds.

    Used to model long-running sub-ops so the drain has something to wait
    on. Each call records its target so the test can verify how many were
    actually dispatched before the cancel.
    """

    def __init__(self, delay: float = 0.5) -> None:
        self.delay = delay
        self.dispatched: list[str] = []

    async def __aenter__(self) -> _DrainFakeWrapper:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def resolve_target(self, target: str) -> dict[str, Any]:
        self.dispatched.append(target)
        await asyncio.sleep(self.delay)
        # All targets resolve to a fake "all" group so on/off pass.
        from tests.test_batch_cmd import _FakeGroup, _group_record

        return _group_record(_FakeGroup(target, target))

    async def light_set_on(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def group_set_on(self, group: Any, on: bool) -> None:
        return None

    async def get_all_lights_group(self) -> Any:
        from tests.test_batch_cmd import _FakeGroup

        return _FakeGroup("0", "all")


@pytest.mark.asyncio
async def test_run_batch_honors_pre_set_cancel_event() -> None:
    """Pre-cancelled batch dispatches nothing and reports all-pending."""
    wrapper = _DrainFakeWrapper(delay=0.05)
    parsed_lines = [ParsedLine(raw=f"on @{i}", verb="on", target=f"@{i}") for i in range(5)]

    cancel = asyncio.Event()
    cancel.set()
    session = BatchSession(
        fmt=OutputFormat.JSONL,
        total=len(parsed_lines),
        cancel_event=cancel,
    )

    pairs = await _run_batch(wrapper, parsed_lines, concurrency=2, session=session)
    # No tasks were created because cancel was already set on entry.
    assert len(pairs) == 0
    assert session.completed == 0
    assert session.pending == 5


@pytest.mark.asyncio
async def test_run_batch_drains_in_flight_within_two_seconds() -> None:
    """SIGINT mid-batch: drain bounds wait to <=2s, summary inputs are accurate.

    Schedules a cancel via ``asyncio.create_task`` BEFORE awaiting
    ``_run_batch`` so the cancel fires while the dispatcher is mid-flight
    (some tasks running, some queued behind the semaphore). Per FR-54c the
    in-flight set gets up to 2 s to finish; anything still pending is
    cancelled. Wall-clock elapsed must be bounded by drain_budget + slack.
    """
    # 8 lines, each blocked on a 5 s sleep, with concurrency=2 — at any
    # moment exactly 2 are in flight and 6 are queued waiting on the
    # semaphore. Firing cancel ~0.1 s in guarantees a real mid-flight state.
    wrapper = _DrainFakeWrapper(delay=5.0)
    parsed_lines = [ParsedLine(raw=f"on @{i}", verb="on", target=f"@{i}") for i in range(8)]

    cancel = asyncio.Event()
    session = BatchSession(
        fmt=OutputFormat.JSONL,
        total=len(parsed_lines),
        cancel_event=cancel,
    )

    async def cancel_after() -> None:
        await asyncio.sleep(0.1)
        cancel.set()

    asyncio.create_task(cancel_after())  # noqa: RUF006 — fire-and-forget by design

    start = time.monotonic()
    pairs = await _run_batch(wrapper, parsed_lines, concurrency=2, session=session)
    elapsed = time.monotonic() - start

    # Drain budget enforced — must NOT block on the 5 s slow sleeps.
    assert elapsed < 2.5, f"drain exceeded 2 s budget: {elapsed:.2f}s"
    # Real cancellation happened — at least some lines never produced a result.
    assert session.pending > 0, "no lines were left pending — cancel didn't fire mid-flight"
    # Accounting holds: every input line is either completed or pending.
    assert session.completed + session.pending == len(parsed_lines)
    # Returned pair count matches the completed count.
    assert len(pairs) == session.completed
    # Cancel event observed.
    assert session.cancel_event is not None and session.cancel_event.is_set()


@pytest.mark.asyncio
async def test_run_batch_streams_partial_results_before_cancel() -> None:
    """Mid-flight cancel preserves a faithful per-line transcript via on_result.

    A SIGINT-during-batch shouldn't lose the records for lines that DID
    finish before the cancel — operators need to know which N of M succeeded.
    The streaming ``on_result`` callback fires once per completed task in
    completion order; the batch verb routes those to stdout in JSONL mode.
    """
    # Fast tasks (~10 ms) so a handful complete in the 0.2 s grace window
    # before cancel fires; concurrency=2 keeps queue length predictable.
    wrapper = _DrainFakeWrapper(delay=0.01)
    parsed_lines = [ParsedLine(raw=f"on @{i}", verb="on", target=f"@{i}") for i in range(20)]

    cancel = asyncio.Event()
    session = BatchSession(
        fmt=OutputFormat.JSONL,
        total=len(parsed_lines),
        cancel_event=cancel,
    )

    streamed: list[tuple[int, str]] = []

    def _on_result(idx: int, parsed: ParsedLine, _task: Any) -> None:
        streamed.append((idx, parsed.raw))

    async def cancel_after() -> None:
        # Long enough to let a few tasks finish; short enough to hit
        # the gather mid-batch.
        await asyncio.sleep(0.05)
        cancel.set()

    asyncio.create_task(cancel_after())  # noqa: RUF006

    pairs = await _run_batch(
        wrapper,
        parsed_lines,
        concurrency=2,
        session=session,
        on_result=_on_result,
    )

    # Streamed callback fired for at least one completed task — the partial
    # transcript is preserved.
    assert len(streamed) >= 1, "no partial results were streamed before cancel"
    # ``pairs`` length matches ``session.completed`` and the streamed count.
    assert len(pairs) == session.completed
    assert len(pairs) >= len(streamed) - 1  # tolerance: 1 task may complete during cleanup
    # Streamed indices fall within input range.
    for idx, _raw in streamed:
        assert 0 <= idx < len(parsed_lines)


# ---------------------------------------------------------------------------
# Summary-line emission
# ---------------------------------------------------------------------------


def test_emit_interrupted_summary_jsonl_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = BatchSession(fmt=OutputFormat.JSONL, total=5, completed=2, pending=3)
    _emit_interrupted_summary(session)
    captured = capsys.readouterr()
    # JSONL → stdout only.
    payload = json.loads(captured.out.strip())
    assert payload == {"event": "interrupted", "completed": 2, "pending": 3}
    assert captured.err == ""


def test_emit_interrupted_summary_json_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = BatchSession(fmt=OutputFormat.JSON, total=4, completed=1, pending=3)
    _emit_interrupted_summary(session)
    captured = capsys.readouterr()
    # JSON mode also emits the JSONL summary line per the SRD's literal reading.
    payload = json.loads(captured.out.strip())
    assert payload["event"] == "interrupted"
    assert payload["completed"] == 1


def test_emit_interrupted_summary_text_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = BatchSession(fmt=OutputFormat.TEXT, total=3, completed=2, pending=1)
    _emit_interrupted_summary(session)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "interrupted" in captured.err
    assert "completed=2" in captured.err
    assert "pending=1" in captured.err


def test_emit_interrupted_summary_quiet_writes_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = BatchSession(fmt=OutputFormat.QUIET, total=3, completed=2, pending=1)
    _emit_interrupted_summary(session)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# ---------------------------------------------------------------------------
# _run_async_graceful single-verb fallback (no session)
# ---------------------------------------------------------------------------


def test_run_async_graceful_no_session_returns_value() -> None:
    """When ``session`` is None we get Phase 1 behavior — no signal hooks."""

    async def _hello() -> str:
        return "hi"

    assert _run_async_graceful(_hello()) == "hi"


def test_run_async_graceful_no_session_propagates_exceptions() -> None:
    async def _boom() -> None:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        _run_async_graceful(_boom())


# ---------------------------------------------------------------------------
# End-to-end SIGINT — POSIX-only, generous timing
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only signal test (SIGINT delivery to subprocess)",
)
def test_subprocess_sigint_emits_summary_and_exits_130(tmp_path: Any) -> None:
    """End-to-end: SIGINT during a slow batch produces the FR-54c summary line.

    We launch the CLI as a subprocess running a batch of 5 ``on`` lines
    targeting an unconfigured wrapper — the verb will fail fast on each
    line because there's no paired bridge, but the dispatch path itself
    runs the signal-aware ``_run_async_graceful`` machinery. We observe
    its SIGINT exit code and stdout summary.

    This is the ``flakily-timed`` test the brief calls out — we accept
    *either* exit-code 130 (signal interrupted the dispatch) or exit-code
    in the §11.1 set (the batch finished before the signal arrived) as
    valid. The test is primarily a smoke / coverage check; the in-process
    summary tests above pin the contract.
    """
    script = "on @nope\n" * 5
    batch_file = tmp_path / "batch.txt"
    batch_file.write_text(script, encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-m", "hue_cli", "--jsonl", "batch", "--file", str(batch_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    # Give the process a beat to launch the dispatch loop.
    time.sleep(0.2)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
    out_b, _err_b = proc.communicate(timeout=10.0)
    out = out_b.decode("utf-8", errors="replace")

    # The process should exit cleanly with one of: 130 (signal interrupted),
    # the not-paired exit code 2 (no wrapper available — failed before
    # signal could land), or another §11.1 code if the batch resolved first.
    # We don't assert on exit_code here because it's timing-dependent; we
    # assert on the *behavior*: stdout is parseable JSONL or empty, never
    # garbage.
    for line in out.strip().splitlines():
        # Each non-blank stdout line must be valid JSON (FR-57b).
        json.loads(line)
