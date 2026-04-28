"""``batch`` verb — execute newline-delimited hue-cli invocations (FR-53/54).

Reads a stream of commands (one ``hue-cli`` invocation per line, minus the
leading ``hue-cli`` token) from a file (``--file <path>``) or stdin
(``--stdin``), parses each line, dispatches the parsed verb's existing
**async core** with ``parallel.run_with_concurrency``, and emits one result
record per attempted operation.

Lines starting with ``#`` are comments; blank lines are skipped. Empty
input exits 0 with stdout ``[]`` in JSON mode, no output otherwise
(FR-54b). Exit code follows :func:`parallel.aggregate_exit_code`:

* all-ok                       → 0
* mixed                        → 7  (:class:`PartialBatchError`, §11.1)
* uniform-failure (all 3, etc) → that single code
* multi-mode failure           → 7

SIGINT / SIGTERM during the dispatch loop drains in-flight tasks for ≤ 2 s,
emits a final JSONL summary line ``{"event":"interrupted",...}`` to stdout,
and exits 130 / 143 respectively. The signal plumbing lives in
:func:`hue_cli.cli._run_async_graceful` keyed off the
:class:`BatchSession` we hand it.

The verb does NOT subprocess-spawn or re-enter Click; it imports the
verb modules' ``_apply_*`` async functions directly. This keeps batch
throughput bound by aiohue / wrapper concurrency, not by Click overhead.
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from hue_cli.errors import (
    HueCliError,
    PartialBatchError,
    UsageError,
    emit_structured_error,
)
from hue_cli.output import OutputFormat, emit_batch_result, emit_json
from hue_cli.parallel import TaskResult, aggregate_exit_code, timed_run
from hue_cli.verbs.onoff_cmd import _apply_power, _apply_toggle
from hue_cli.verbs.scene_cmd import _apply_scene_apply
from hue_cli.verbs.set_cmd import (
    _apply_set,
    _check_mutex,
    _parse_hsv,
    _parse_xy,
)

if TYPE_CHECKING:
    from hue_cli._protocols import HueWrapperProto


# ---------------------------------------------------------------------------
# Parsed-line shape
# ---------------------------------------------------------------------------


@dataclass
class ParsedLine:
    """One parsed input line ready for dispatch.

    ``raw`` preserves the original line (for the ``line`` field of each
    result record); ``verb`` and ``target`` drive the dispatch table; ``args``
    holds the verb's already-parsed kwargs (the verb's ``_apply_*`` core
    takes them positionally / as kwargs).

    ``error`` is populated when the parser itself rejects the line (unknown
    verb, bad flag, missing arg). Such lines flow through the dispatch loop
    as pre-failed :class:`TaskResult`s — the operator sees one result line
    per input line, including parse failures, per FR-57b.
    """

    raw: str
    verb: str
    target: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    error: HueCliError | None = None


# ---------------------------------------------------------------------------
# BatchSession — the shared progress / drain coordination object
# ---------------------------------------------------------------------------


@dataclass
class BatchSession:
    """Mutable progress accumulator the signal handler reads on drain (FR-54c).

    The dispatcher loop bumps ``completed`` after each ``await`` returns and
    sets ``cancel_event`` on signal receipt to stop dispatching new ops.
    ``_run_async_graceful`` (in ``cli.py``) waits up to 2 s for the in-flight
    set to finish, then emits the final summary line keyed off our state.
    """

    fmt: OutputFormat
    total: int
    completed: int = 0
    pending: int = 0
    cancel_event: asyncio.Event | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "event": "interrupted",
            "completed": int(self.completed),
            "pending": int(self.pending),
        }


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


_VALID_EFFECTS = ("none", "colorloop")
_VALID_ALERTS = ("none", "select", "lselect")


def _parse_batch_line(raw_line: str) -> ParsedLine | None:
    """Parse one input line into a :class:`ParsedLine` or ``None`` to skip.

    Skip rules per FR-54b:
        * empty / whitespace-only → ``None``
        * leading ``#``           → ``None`` (comment)

    Only **leading** ``#`` is treated as a comment marker. An inline ``#``
    (e.g., ``on @kitchen # set after dinner``) is passed through to
    ``shlex.split`` and will be tokenised into the verb's arg list, which
    typically surfaces as a :class:`UsageError` (unknown verb / unknown
    flag). Operators wanting trailing notes should put them on their own
    ``#``-prefixed line.

    A line that fails parsing returns a :class:`ParsedLine` with ``error``
    set (a :class:`UsageError`) — the dispatch loop emits one failed-result
    record for it without ever opening a connection.
    """
    stripped = raw_line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    try:
        tokens = shlex.split(stripped)
    except ValueError as exc:
        return ParsedLine(
            raw=stripped,
            verb="",
            error=UsageError(f"could not tokenize line: {exc}"),
        )

    if not tokens:
        return None

    verb = tokens[0]
    rest = tokens[1:]

    try:
        if verb in ("on", "off"):
            return _parse_onoff_line(stripped, verb, rest)
        if verb == "toggle":
            return _parse_toggle_line(stripped, rest)
        if verb == "set":
            return _parse_set_line(stripped, rest)
        if verb == "scene":
            return _parse_scene_line(stripped, rest)
        return ParsedLine(
            raw=stripped,
            verb=verb,
            error=UsageError(f"unknown batch verb {verb!r}"),
        )
    except UsageError as exc:
        return ParsedLine(raw=stripped, verb=verb, error=exc)


def _parse_onoff_line(raw: str, verb: str, rest: list[str]) -> ParsedLine:
    if not rest:
        raise UsageError(f"{verb!r} requires a target")
    if len(rest) != 1:
        raise UsageError(f"{verb!r} takes exactly one positional target (got {rest!r})")
    return ParsedLine(raw=raw, verb=verb, target=rest[0])


def _parse_toggle_line(raw: str, rest: list[str]) -> ParsedLine:
    if not rest:
        raise UsageError("'toggle' requires a target")
    if len(rest) != 1:
        raise UsageError(f"'toggle' takes exactly one positional target (got {rest!r})")
    return ParsedLine(raw=raw, verb="toggle", target=rest[0])


def _parse_set_line(raw: str, rest: list[str]) -> ParsedLine:
    """Parse a ``set <target> [flags...]`` line into kwargs for ``_apply_set``.

    Flag grammar mirrors :func:`hue_cli.verbs.set_cmd.set_cmd` Click options:
    ``--brightness``, ``--kelvin``, ``--mireds``, ``--xy``, ``--hex``,
    ``--color``, ``--hsv``, ``--transition``, ``--effect``, ``--alert``.
    Mutex (FR-35) is enforced via :func:`_check_mutex` so a bad combo
    returns a parse-time :class:`UsageError`.
    """
    if not rest:
        raise UsageError("'set' requires a target")
    target = rest[0]
    flags = rest[1:]

    brightness: int | None = None
    kelvin: int | None = None
    mireds: int | None = None
    xy_raw: str | None = None
    hex_value: str | None = None
    color_name: str | None = None
    hsv_raw: str | None = None
    transition: int | None = None
    effect: str | None = None
    alert: str | None = None

    i = 0
    while i < len(flags):
        flag = flags[i]
        if not flag.startswith("--"):
            raise UsageError(f"expected --flag, got {flag!r}")
        if i + 1 >= len(flags):
            raise UsageError(f"flag {flag!r} requires a value")
        value = flags[i + 1]
        i += 2

        if flag == "--brightness":
            try:
                brightness = int(value)
            except ValueError as exc:
                raise UsageError(f"--brightness must be int, got {value!r}") from exc
            if not 0 <= brightness <= 100:
                raise UsageError(f"--brightness must be 0-100, got {brightness}")
        elif flag == "--kelvin":
            try:
                kelvin = int(value)
            except ValueError as exc:
                raise UsageError(f"--kelvin must be int, got {value!r}") from exc
        elif flag == "--mireds":
            try:
                mireds = int(value)
            except ValueError as exc:
                raise UsageError(f"--mireds must be int, got {value!r}") from exc
        elif flag == "--xy":
            xy_raw = value
        elif flag == "--hex":
            hex_value = value
        elif flag == "--color":
            color_name = value
        elif flag == "--hsv":
            hsv_raw = value
        elif flag == "--transition":
            try:
                transition = int(value)
            except ValueError as exc:
                raise UsageError(f"--transition must be int ms, got {value!r}") from exc
        elif flag == "--effect":
            if value not in _VALID_EFFECTS:
                raise UsageError(f"--effect must be one of {_VALID_EFFECTS}, got {value!r}")
            effect = value
        elif flag == "--alert":
            if value not in _VALID_ALERTS:
                raise UsageError(f"--alert must be one of {_VALID_ALERTS}, got {value!r}")
            alert = value
        else:
            raise UsageError(f"unknown 'set' flag {flag!r}")

    _check_mutex(
        kelvin=kelvin,
        mireds=mireds,
        xy=xy_raw,
        hex_=hex_value,
        color=color_name,
        hsv=hsv_raw,
    )
    if all(
        v is None
        for v in (
            brightness,
            kelvin,
            mireds,
            xy_raw,
            hex_value,
            color_name,
            hsv_raw,
            transition,
            effect,
            alert,
        )
    ):
        raise UsageError(
            "'set' requires at least one of --brightness/--kelvin/--mireds/"
            "--xy/--hex/--color/--hsv/--transition/--effect/--alert"
        )

    xy = _parse_xy(xy_raw) if xy_raw is not None else None
    hsv = _parse_hsv(hsv_raw) if hsv_raw is not None else None

    return ParsedLine(
        raw=raw,
        verb="set",
        target=target,
        args={
            "brightness": brightness,
            "kelvin": kelvin,
            "mireds": mireds,
            "xy": xy,
            "hex_": hex_value,
            "color_name": color_name,
            "hsv": hsv,
            "transition": transition,
            "effect": effect,
            "alert": alert,
        },
    )


def _parse_scene_line(raw: str, rest: list[str]) -> ParsedLine:
    """Parse ``scene apply <name> [--transition <ms>]`` lines.

    Only the ``apply`` sub-verb is dispatchable from batch — ``scene list``
    is a read-only verb that emits its own JSON shape and isn't sensible
    inside a per-line result envelope.
    """
    if not rest:
        raise UsageError("'scene' requires a sub-verb (e.g., 'scene apply <name>')")
    sub = rest[0]
    if sub != "apply":
        raise UsageError(f"only 'scene apply' is supported in batch mode, got {sub!r}")
    rest = rest[1:]
    if not rest:
        raise UsageError("'scene apply' requires a scene name or id")

    target = rest[0]
    flags = rest[1:]
    transition_ms: int | None = None

    i = 0
    while i < len(flags):
        flag = flags[i]
        if flag != "--transition":
            raise UsageError(f"unknown 'scene apply' flag {flag!r}")
        if i + 1 >= len(flags):
            raise UsageError(f"flag {flag!r} requires a value")
        try:
            transition_ms = int(flags[i + 1])
        except ValueError as exc:
            raise UsageError(f"--transition must be int ms, got {flags[i + 1]!r}") from exc
        i += 2

    return ParsedLine(
        raw=raw,
        verb="scene",
        target=target,
        args={"sub": "apply", "transition_ms": transition_ms},
    )


# ---------------------------------------------------------------------------
# Dispatch — turn a ParsedLine into a coroutine returning a result dict
# ---------------------------------------------------------------------------


async def _dispatch_parsed_line(
    wrapper: HueWrapperProto,
    parsed: ParsedLine,
) -> dict[str, Any]:
    """Run the parsed line's verb against ``wrapper`` and return the result dict.

    Caller wraps this in :func:`hue_cli.parallel.timed_run` so failures get
    converted to a :class:`TaskResult` with ``ok=False``. A pre-parsed-error
    line (``parsed.error is not None``) re-raises the parse error here so the
    same conversion path applies.
    """
    if parsed.error is not None:
        raise parsed.error

    if parsed.verb in ("on", "off"):
        assert parsed.target is not None
        return await _apply_power(wrapper, parsed.target, parsed.verb == "on")
    if parsed.verb == "toggle":
        assert parsed.target is not None
        return await _apply_toggle(wrapper, parsed.target)
    if parsed.verb == "set":
        assert parsed.target is not None
        return await _apply_set(wrapper, parsed.target, **parsed.args)
    if parsed.verb == "scene":
        assert parsed.target is not None
        return await _apply_scene_apply(
            wrapper,
            parsed.target,
            transition_ms=parsed.args.get("transition_ms"),
        )

    raise UsageError(f"unknown batch verb {parsed.verb!r}")


def _result_record(parsed: ParsedLine, task: TaskResult) -> dict[str, Any]:
    """Render one batch result dict per FR-54a."""
    record: dict[str, Any] = {
        "line": parsed.raw,
        "verb": parsed.verb,
        "target": parsed.target,
        "ok": task.ok,
        "duration_ms": round(task.duration_ms, 2),
        "error": None,
        "result": None,
    }
    if task.ok:
        record["result"] = task.value
    else:
        if task.error is not None:
            record["error"] = task.error.error
        else:
            record["error"] = "unknown_error"
    return record


# ---------------------------------------------------------------------------
# Runner — bounded-concurrency loop with cancel support
# ---------------------------------------------------------------------------


_DRAIN_TIMEOUT_SECONDS = 2.0


async def _wait_with_cancel(
    tasks: list[asyncio.Task[TaskResult]],
    cancel_event: asyncio.Event | None,
    *,
    on_done: Callable[[int, asyncio.Task[TaskResult]], None] | None = None,
    drain_timeout: float = _DRAIN_TIMEOUT_SECONDS,
) -> tuple[set[asyncio.Task[TaskResult]], set[asyncio.Task[TaskResult]]]:
    """Wait for all ``tasks`` while watching ``cancel_event``.

    Behaviour:

    * If ``cancel_event`` is ``None`` or never fires, this returns once
      every task is complete — equivalent to ``asyncio.gather`` semantics
      (without the per-task exception unwrapping).
    * If ``cancel_event`` fires while tasks are still running, in-flight
      tasks get up to ``drain_timeout`` seconds to finish naturally; any
      task still running after that is cancelled and reaped (so aiohttp's
      ``ClientSession`` ResourceWarning doesn't fire). FR-54c §5.11.

    ``on_done`` (when supplied) is invoked once per task as it completes,
    receiving its original index in ``tasks`` and the task itself. This is
    how the batch verb streams per-line JSONL records to stdout — so a
    SIGINT mid-batch leaves a faithful partial transcript on stdout, not
    just an "interrupted" summary line.

    Returns ``(done, pending)`` sets matching :func:`asyncio.wait`.
    """
    pending: set[asyncio.Task[TaskResult]] = set(tasks)
    done: set[asyncio.Task[TaskResult]] = set()
    task_to_index = {task: idx for idx, task in enumerate(tasks)}

    def _record_finished(finished: set[asyncio.Task[TaskResult]]) -> None:
        for task in finished:
            if task in pending:
                pending.discard(task)
                done.add(task)
                if on_done is not None:
                    on_done(task_to_index[task], task)

    while pending:
        if cancel_event is not None and cancel_event.is_set():
            break

        wait_targets: list[asyncio.Task[Any]] = list(pending)
        cancel_waiter: asyncio.Task[Any] | None = None
        if cancel_event is not None:
            cancel_waiter = asyncio.create_task(cancel_event.wait())
            wait_targets.append(cancel_waiter)

        try:
            finished, _ = await asyncio.wait(wait_targets, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if cancel_waiter is not None and not cancel_waiter.done():
                cancel_waiter.cancel()
                # Reap to suppress "Task was destroyed but it is pending!".
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_waiter

        # Pull out only the real-task completions; the cancel waiter (if it
        # fired) is intentionally NOT recorded in `done`.
        real_finished = {task for task in finished if task is not cancel_waiter and task in pending}
        _record_finished(real_finished)

    if cancel_event is not None and cancel_event.is_set() and pending:
        # FR-54c (2): give in-flight tasks up to drain_timeout seconds.
        drain_done, _drain_pending = await asyncio.wait(pending, timeout=drain_timeout)
        _record_finished(drain_done)

        # Anything still running after the drain budget gets force-cancelled.
        # Reap the cancellations so aiohttp's ClientSession doesn't emit a
        # ResourceWarning at GC time.
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    return done, pending


async def _run_batch(
    wrapper: HueWrapperProto,
    parsed_lines: list[ParsedLine],
    *,
    concurrency: int,
    session: BatchSession,
    on_result: Callable[[int, ParsedLine, TaskResult], None] | None = None,
) -> list[tuple[ParsedLine, TaskResult]]:
    """Dispatch all parsed lines under a semaphore, honouring ``session.cancel_event``.

    Returns the list of ``(parsed, TaskResult)`` pairs in **input order**,
    skipping any input line that never produced a result (a queued task
    cancelled by the drain budget, or a line never scheduled because the
    cancel landed before its `create_task`). The skipped count is
    reflected in ``session.pending``; ``session.completed`` is the length
    of the returned list.

    On a cancel-event trigger:

        1. The dispatcher stops scheduling new ops on the next yield.
        2. ``_wait_with_cancel`` gives in-flight tasks up to 2 s to finish
           (FR-54c §5.11). Anything still running is force-cancelled and
           reaped.
        3. If ``on_result`` is supplied, completed tasks have already been
           streamed to it as they finished — so on cancel the operator
           sees a faithful JSONL transcript of what landed before SIGINT,
           not just the summary line.

    The whole loop runs inside a single ``async with wrapper:`` so all
    per-line verbs share one TLS connection (FR-53 throughput). The
    wrapper is depth-counted so per-verb ``async with wrapper:`` blocks
    inside the dispatch path become true no-ops.
    """
    bound = max(1, concurrency)
    sem = asyncio.Semaphore(bound)

    async def _one(parsed: ParsedLine) -> TaskResult:
        async with sem:
            target_label = parsed.target or parsed.verb
            return await timed_run(target_label, _dispatch_parsed_line(wrapper, parsed))

    results: list[TaskResult | None] = [None] * len(parsed_lines)

    def _on_task_done(idx: int, task: asyncio.Task[TaskResult]) -> None:
        # Reap the result and stream it to the caller's per-line callback.
        # ``task`` is in ``done``, so ``.result()`` is safe.
        try:
            value = task.result()
        except (asyncio.CancelledError, Exception):  # pragma: no cover — defensive
            return
        results[idx] = value
        if on_result is not None:
            on_result(idx, parsed_lines[idx], value)

    async with wrapper:
        tasks: list[asyncio.Task[TaskResult]] = []
        for parsed in parsed_lines:
            if session.cancel_event is not None and session.cancel_event.is_set():
                break
            tasks.append(asyncio.create_task(_one(parsed)))
            # Yield to the event loop so the signal handler can land
            # ``cancel_event.set()`` mid-dispatch. Without this, a 1000-line
            # batch creates all 1000 tasks before the loop ever yields, and
            # the in-loop ``cancel_event.is_set()`` check above is dead code.
            await asyncio.sleep(0)

        if not tasks:
            session.completed = 0
            session.pending = len(parsed_lines)
            return []

        await _wait_with_cancel(
            tasks,
            session.cancel_event,
            on_done=_on_task_done,
        )

    pairs: list[tuple[ParsedLine, TaskResult]] = [
        (parsed_lines[idx], result) for idx, result in enumerate(results) if result is not None
    ]
    session.completed = len(pairs)
    session.pending = len(parsed_lines) - len(pairs)
    return pairs


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command(name="batch")
@click.option(
    "--file",
    "file_path",
    type=click.Path(exists=False, dir_okay=False, path_type=Path),
    default=None,
    help="Read newline-delimited commands from FILE (FR-53).",
)
@click.option(
    "--stdin",
    "from_stdin",
    is_flag=True,
    help="Read newline-delimited commands from stdin (FR-54).",
)
@click.pass_context
def batch_cmd(
    ctx: click.Context,
    file_path: Path | None,
    from_stdin: bool,
) -> None:
    """Execute a stream of hue-cli invocations (FR-53 / FR-54).

    Each non-blank, non-comment input line is parsed as a hue-cli command
    minus the leading ``hue-cli`` token (e.g., ``set kitchen --brightness
    30``), dispatched concurrently (bounded by ``--concurrency`` /
    ``[defaults] concurrency``), and emitted as one structured result per
    line.

    Exit code follows §11.1: ``0`` (all ok), ``7`` (mixed), or the uniform
    failure code (e.g., all-BridgeBusy → ``3``). On SIGINT / SIGTERM the
    dispatcher drains for ≤ 2 s and emits a final
    ``{"event":"interrupted","completed":N,"pending":M}`` JSONL summary
    line before exiting 130 / 143.
    """
    obj = ctx.obj or {}
    fmt = obj.get("format") if isinstance(obj, dict) else None
    if not isinstance(fmt, OutputFormat):
        fmt = OutputFormat.TEXT
    json_mode = fmt in (OutputFormat.JSON, OutputFormat.JSONL)

    err: HueCliError
    if file_path is None and not from_stdin:
        err = UsageError("'batch' requires --file <path> or --stdin")
        emit_structured_error(err, json_mode=json_mode)
        sys.exit(err.exit_code)
    if file_path is not None and from_stdin:
        err = UsageError("'batch' --file and --stdin are mutually exclusive")
        emit_structured_error(err, json_mode=json_mode)
        sys.exit(err.exit_code)

    # Read input.
    try:
        if from_stdin:
            raw = sys.stdin.read()
        else:
            assert file_path is not None
            raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        err = UsageError(f"could not read batch input: {exc}")
        emit_structured_error(err, json_mode=json_mode)
        sys.exit(err.exit_code)

    # Parse: skip blanks/comments, surface parse failures as pre-failed lines.
    parsed_lines: list[ParsedLine] = []
    for line in raw.splitlines():
        parsed = _parse_batch_line(line)
        if parsed is not None:
            parsed_lines.append(parsed)

    # FR-54b: empty input → exit 0; stdout `[]` in JSON, nothing otherwise.
    if not parsed_lines:
        if fmt is OutputFormat.JSON:
            click.echo("[]")
        sys.exit(0)

    wrapper = obj.get("wrapper") if isinstance(obj, dict) else None
    if wrapper is None:
        # No bridge wrapper available — every line would fail anyway with
        # an "unknown bridge" error. Fail fast with a single structured
        # error to stderr, exit 2 (auth/no-paired).
        from hue_cli.errors import NotPairedError

        err = NotPairedError(
            "No active bridge wrapper. Run `hue-cli bridge pair` first.",
            hint="Run: hue-cli bridge pair",
        )
        emit_structured_error(err, json_mode=json_mode)
        sys.exit(err.exit_code)

    # Concurrency: --concurrency overrides [defaults] concurrency (default 5).
    concurrency = _resolve_concurrency(obj)

    # Set up the BatchSession. The signal handler hangs off this object via
    # ``_run_async_graceful``; we hand it the format so the drain emit knows
    # whether to write JSONL or human-readable text.
    session = BatchSession(fmt=fmt, total=len(parsed_lines))

    from hue_cli.cli import _run_async_graceful

    # JSONL streaming: emit each record on stdout the instant its task
    # finishes (completion-order, not input-order). Streaming matters
    # because a SIGINT mid-batch causes ``_run_async_graceful`` to call
    # ``_emit_interrupted_summary`` and ``sys.exit`` — the verb's
    # post-await code never runs on cancel, so any records buffered
    # in-process would be lost. Records that landed before the interrupt
    # are already on stdout; the summary line follows them.
    #
    # TEXT and JSON modes batch-emit after the run completes: TEXT because
    # human readers expect a single coherent block, JSON because pretty-array
    # framing can't be streamed line-by-line. Both paths are unreachable on
    # cancel (sys.exit fires inside the graceful runner), which is the
    # documented limitation for those modes.
    on_result: Callable[[int, ParsedLine, TaskResult], None] | None = None
    if fmt is OutputFormat.JSONL:

        def _stream_jsonl(_idx: int, parsed: ParsedLine, task: TaskResult) -> None:
            record = _result_record(parsed, task)
            line = emit_batch_result(record, fmt)
            if line:
                click.echo(line)

        on_result = _stream_jsonl

    pairs = _run_async_graceful(
        _run_batch(
            wrapper,
            parsed_lines,
            concurrency=concurrency,
            session=session,
            on_result=on_result,
        ),
        session=session,
    )

    # Emit results for non-streaming formats. JSONL already streamed above.
    if fmt is not OutputFormat.JSONL:
        _emit_results(pairs, fmt)

    # Exit code follows §11.1.
    code = aggregate_exit_code([t for _, t in pairs])
    if code == 7:
        # Mixed batch — surface a structured summary error to stderr.
        err = PartialBatchError(
            f"batch had {sum(1 for _, t in pairs if t.ok)} ok, "
            f"{sum(1 for _, t in pairs if not t.ok)} fail"
        )
        emit_structured_error(err, json_mode=json_mode)
    sys.exit(code)


def _resolve_concurrency(obj: dict[str, Any]) -> int:
    """Resolve effective concurrency: ``--concurrency`` > [defaults] > built-in 5."""
    cli_value = obj.get("concurrency") if isinstance(obj, dict) else None
    if isinstance(cli_value, int) and cli_value >= 1:
        return cli_value

    # Best-effort config read; failures fall back to 5.
    try:
        from hue_cli.config import load_config

        cfg_path = obj.get("config_path") if isinstance(obj, dict) else None
        explicit = Path(str(cfg_path)).expanduser() if cfg_path else None
        cfg = load_config(explicit_path=explicit)
        return int(cfg.concurrency)
    except Exception:
        return 5


def _emit_results(
    pairs: list[tuple[ParsedLine, TaskResult]],
    fmt: OutputFormat,
) -> None:
    """Write one record per dispatched line, format-aware."""
    if fmt is OutputFormat.QUIET:
        return

    records = [_result_record(parsed, task) for parsed, task in pairs]

    if fmt is OutputFormat.JSON:
        click.echo(emit_json(records))
        return

    # TEXT and JSONL: one line per record via the shared helper.
    for record in records:
        line = emit_batch_result(record, fmt)
        if line:
            click.echo(line)
