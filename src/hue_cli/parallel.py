"""Async dispatch helpers.

Phase 1 ships the minimum set the CLI needs: a semaphore-bounded gather and a
:class:`TaskResult` envelope. Phase 3 expands this for full ``batch`` glue
(``aggregate_exit_code``, partial-result shaping on SIGINT, etc.) per FR-54a /
FR-54c.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    # Engineer A's errors module supplies the canonical hue-cli exception
    # base. Importing under TYPE_CHECKING keeps mypy strict-clean during
    # parallel development when ``errors.py`` may not yet exist on disk.
    from hue_cli.errors import HueCliError


T = TypeVar("T")


@dataclass
class TaskResult:
    """Envelope for a single sub-operation's outcome.

    ``ok=True`` means the sub-op completed without raising; ``value`` holds
    its return. ``ok=False`` means it raised and ``error`` holds the mapped
    :class:`HueCliError`. ``duration_ms`` is the wall-clock duration of the
    awaited coroutine in milliseconds.
    """

    target: str
    ok: bool
    value: Any
    error: HueCliError | None
    duration_ms: float


async def run_with_concurrency(
    coros: list[Awaitable[T]],
    limit: int,
) -> list[T]:
    """Run a list of coroutines with at most ``limit`` in flight at a time.

    Order of returned values matches the order of ``coros``. Exceptions from
    individual coroutines propagate via :func:`asyncio.gather` (they are NOT
    wrapped here — :class:`TaskResult` wrapping is the caller's job when it
    wants a structured-result shape).

    ``limit`` SHALL be ≥ 1; values < 1 are clamped to 1.
    """
    bound = max(1, limit)
    sem = asyncio.Semaphore(bound)

    async def _run(awaitable: Awaitable[T]) -> T:
        async with sem:
            return await awaitable

    return await asyncio.gather(*(_run(c) for c in coros))


async def timed_run(target: str, coro: Awaitable[T]) -> TaskResult:
    """Wrap a coroutine into a :class:`TaskResult` with timing.

    Catches the project's ``HueCliError`` subclasses (resolved lazily so
    Engineer A's ``errors`` module need not exist at import time) and any
    other ``Exception`` — both flow into ``TaskResult.error`` for the failing
    case, with ``ok=False``.
    """
    start = time.perf_counter()
    try:
        value = await coro
    except Exception as exc:
        # Late-bind the exception type so this module imports cleanly even
        # before Engineer A's errors.py lands on disk.
        err_cls: type[BaseException] | None
        try:
            from hue_cli.errors import HueCliError as _Err  # local import

            err_cls = _Err
        except ImportError:  # pragma: no cover — exercised only pre-merge
            err_cls = None
        elapsed = (time.perf_counter() - start) * 1000.0
        if err_cls is not None and isinstance(exc, err_cls):
            return TaskResult(target=target, ok=False, value=None, error=exc, duration_ms=elapsed)
        return TaskResult(target=target, ok=False, value=None, error=None, duration_ms=elapsed)
    elapsed = (time.perf_counter() - start) * 1000.0
    return TaskResult(target=target, ok=True, value=value, error=None, duration_ms=elapsed)
