"""Tests for :mod:`hue_cli.parallel` — Phase 3 additions.

Phase 1 already exercises :func:`run_with_concurrency` and :func:`timed_run`
in ``test_part_b.py``. This file pins the Phase 3 :func:`aggregate_exit_code`
helper, the pure-function spec for §11.1's batch-collapse rule (FR-54a):

* empty results              → 0
* all-ok                     → 0
* mixed ok and fail          → 7  (PartialBatchError)
* uniform-failure (all 3)    → that single code (e.g. 3 for BridgeBusy)
* multi-mode failure         → 7

These cases are independent of the batch verb's CLI integration — keeping
them in their own module makes the contract auditable in isolation.
"""

from __future__ import annotations

from hue_cli.errors import (
    BridgeBusyError,
    NetworkError,
    NotFoundError,
)
from hue_cli.parallel import TaskResult, aggregate_exit_code


def _ok(target: str = "x") -> TaskResult:
    return TaskResult(target=target, ok=True, value={"ok": True}, error=None, duration_ms=1.0)


def _fail(err: BaseException, target: str = "x") -> TaskResult:
    # ``aggregate_exit_code`` only inspects ``error`` (HueCliError-shaped); the
    # ``value`` stays None on failure.
    return TaskResult(
        target=target,
        ok=False,
        value=None,
        error=err,  # type: ignore[arg-type]
        duration_ms=1.0,
    )


class TestAggregateExitCode:
    def test_empty_returns_zero(self) -> None:
        assert aggregate_exit_code([]) == 0

    def test_all_ok_returns_zero(self) -> None:
        assert aggregate_exit_code([_ok(), _ok(), _ok()]) == 0

    def test_mixed_returns_seven(self) -> None:
        results = [_ok(), _fail(BridgeBusyError("busy")), _fail(NetworkError("net"))]
        assert aggregate_exit_code(results) == 7

    def test_one_ok_one_fail_returns_seven(self) -> None:
        # The minimum mixed case.
        results = [_ok(), _fail(BridgeBusyError("busy"))]
        assert aggregate_exit_code(results) == 7

    def test_uniform_failure_returns_that_code(self) -> None:
        # All three failed for the same reason (BridgeBusy → 3) → exit 3.
        results = [
            _fail(BridgeBusyError("busy")),
            _fail(BridgeBusyError("busy")),
            _fail(BridgeBusyError("busy")),
        ]
        assert aggregate_exit_code(results) == 3

    def test_multi_mode_failure_returns_seven(self) -> None:
        # Three failures: BridgeBusy (3), NetworkError (3), NotFound (4).
        # First two share code 3 but the third is 4 → multi-mode → 7.
        results = [
            _fail(BridgeBusyError("busy")),
            _fail(NetworkError("net")),
            _fail(NotFoundError("missing")),
        ]
        assert aggregate_exit_code(results) == 7

    def test_uniform_failure_two_distinct_classes_same_code(self) -> None:
        # BridgeBusy and NetworkError both map to exit code 3 — uniform code,
        # different classes. Aggregate keys off the int code, not the class.
        results = [
            _fail(BridgeBusyError("busy")),
            _fail(NetworkError("net")),
        ]
        assert aggregate_exit_code(results) == 3

    def test_failure_without_error_object_treated_as_one(self) -> None:
        # Defensive: a TaskResult with ok=False but error=None (an unexpected
        # non-HueCliError exception path) should fall through to exit 1.
        results = [_fail(BridgeBusyError("busy"))]
        results.append(TaskResult(target="x", ok=False, value=None, error=None, duration_ms=1.0))
        # First one is code 3, second is 1 → multi-mode → 7.
        assert aggregate_exit_code(results) == 7
