"""Top-level Click CLI surface (§8).

Phase 1 wires all Part B verbs (list, info, on, off, toggle, config) plus
Engineer A's bridge / auth groups into a single ``main`` group. Common flags
(§8.3) live on the top group so every sub-verb sees them via ``ctx.obj``.

Async coroutines are run via :func:`_run_async_graceful`, which traps
``SIGINT`` → exit 130 and ``SIGTERM`` → exit 143 (FR-54c, Phase 1 shape —
Phase 3 elaborates with batch-aware partial-result emission).

Engineer A's modules (``bridge``, ``auth``, ``config``, ``errors``) are
imported defensively so this CLI loads even when their files are missing on
disk during parallel development. test_smoke.py (``--version`` / ``--help``)
keeps passing regardless of integration state.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Coroutine
from typing import Any

import click

from hue_cli import __version__
from hue_cli.logging_setup import setup_logging
from hue_cli.output import detect
from hue_cli.verbs.batch_cmd import batch_cmd
from hue_cli.verbs.config_cmd import config_group
from hue_cli.verbs.group_cmd import group_cmd_group
from hue_cli.verbs.info_cmd import info_cmd
from hue_cli.verbs.list_cmd import list_group
from hue_cli.verbs.onoff_cmd import off_cmd, on_cmd, toggle_cmd
from hue_cli.verbs.scene_cmd import scene_group
from hue_cli.verbs.sensor_cmd import sensor_group
from hue_cli.verbs.set_cmd import set_cmd

# --- Async graceful runner ---------------------------------------------------


def _run_async_graceful(
    coro: Coroutine[Any, Any, Any],
    *,
    session: Any | None = None,
) -> Any:
    """Run an async coroutine via :func:`asyncio.run` with signal handling.

    Two modes keyed off ``session``:

    * ``session is None`` (single-verb invocations) — Phase 1 behavior:
      SIGINT → exit 130, SIGTERM → exit 143, no drain attempt.
    * ``session`` is a :class:`hue_cli.verbs.batch_cmd.BatchSession` — Phase
      3 graceful-drain behavior (FR-54c):

        1. On signal receipt set ``session.cancel_event`` so the dispatcher
           stops scheduling new ops.
        2. Wait up to 2 s for in-flight tasks to complete.
        3. Emit ``{"event":"interrupted","completed":N,"pending":M}`` to
           stdout — JSONL when batch is in JSON / JSONL mode, a human
           sentence on stderr in TEXT mode.
        4. Exit 130 (SIGINT) or 143 (SIGTERM).

    On POSIX both SIGINT and SIGTERM are wired via
    :meth:`asyncio.AbstractEventLoop.add_signal_handler` when a session is
    supplied; on Windows only SIGINT is reachable (Python forbids
    ``add_signal_handler`` on the ``ProactorEventLoop``) so we fall back to
    a synchronous ``signal.signal`` shim that raises ``SystemExit``.
    """
    exit_sigint = 130
    exit_sigterm = 143

    if session is None:
        # Phase 1 path — preserved verbatim for non-batch verbs.
        def _handle_sigterm(*_args: object) -> None:
            raise SystemExit(exit_sigterm)

        prior_term = signal.getsignal(signal.SIGTERM)
        try:
            with contextlib.suppress(OSError, ValueError):
                signal.signal(signal.SIGTERM, _handle_sigterm)
            try:
                return asyncio.run(coro)
            except KeyboardInterrupt:
                sys.exit(exit_sigint)
            except SystemExit:
                raise
        finally:
            with contextlib.suppress(OSError, ValueError):
                signal.signal(signal.SIGTERM, prior_term)

    # Batch path — graceful drain (FR-54c).
    received_signal: dict[str, int | None] = {"code": None}

    async def _runner() -> Any:
        loop = asyncio.get_running_loop()
        # The session shares its cancel_event with the dispatcher; create
        # it on the running loop so signal handlers can safely set it.
        if session.cancel_event is None:
            session.cancel_event = asyncio.Event()

        def _on_signal(sig_name: str, exit_code: int) -> None:
            received_signal["code"] = exit_code
            assert session.cancel_event is not None
            session.cancel_event.set()

        # POSIX: hook both SIGINT and SIGTERM via the event loop.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal.SIGINT, _on_signal, "SIGINT", exit_sigint)
        with contextlib.suppress(NotImplementedError, RuntimeError, AttributeError):
            loop.add_signal_handler(signal.SIGTERM, _on_signal, "SIGTERM", exit_sigterm)

        try:
            return await coro
        finally:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(signal.SIGINT)
            with contextlib.suppress(NotImplementedError, RuntimeError, AttributeError):
                loop.remove_signal_handler(signal.SIGTERM)

    try:
        result = asyncio.run(_runner())
    except KeyboardInterrupt:
        # Windows fallback or pre-signal-handler-installed Ctrl-C.
        received_signal["code"] = exit_sigint
        result = None

    if received_signal["code"] is not None:
        _emit_interrupted_summary(session)
        sys.exit(received_signal["code"])

    return result


def _emit_interrupted_summary(session: Any) -> None:
    """Write the FR-54c summary line, format-aware.

    JSON / JSONL → ``{"event":"interrupted","completed":N,"pending":M}`` to
    stdout (JSONL one-liner). TEXT → human sentence on stderr.
    QUIET → nothing.
    """
    from hue_cli.output import OutputFormat, emit_jsonl

    fmt = getattr(session, "fmt", OutputFormat.TEXT)
    payload = (
        session.snapshot()
        if hasattr(session, "snapshot")
        else {
            "event": "interrupted",
            "completed": int(getattr(session, "completed", 0)),
            "pending": int(getattr(session, "pending", 0)),
        }
    )

    if fmt is OutputFormat.QUIET:
        return
    if fmt in (OutputFormat.JSON, OutputFormat.JSONL):
        line = next(iter(emit_jsonl([payload])))
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return
    # TEXT
    sys.stderr.write(
        f"interrupted: completed={payload['completed']} pending={payload['pending']}\n"
    )
    sys.stderr.flush()


# --- Top-level group ---------------------------------------------------------


@click.group()
@click.version_option(version=__version__, prog_name="hue-cli")
@click.option("--bridge", "bridge_alias", default=None, help="Bridge alias or id.")
@click.option(
    "--bridge-ip",
    default=None,
    help="Bypass credentials and target a specific IP.",
)
@click.option(
    "--app-key",
    default=None,
    help="Override the credentials-file app-key for this invocation.",
)
@click.option("--json", "json_flag", is_flag=True, help="Pretty JSON output.")
@click.option("--jsonl", "jsonl_flag", is_flag=True, help="Newline-delimited JSON.")
@click.option("--quiet", is_flag=True, help="Suppress stdout.")
@click.option("--timeout", type=float, default=5.0, help="Per-operation timeout (seconds).")
@click.option("--config", "config_path", default=None, help="Path to a config file.")
@click.option(
    "--concurrency",
    type=int,
    default=None,
    help="Override [defaults] concurrency for this invocation.",
)
@click.option(
    "--no-cloud",
    is_flag=True,
    help="Skip cloud NUPNP discovery (bridge discover only).",
)
@click.option(
    "--no-probe",
    is_flag=True,
    help="Skip live reachability probe (auth status, list --probe).",
)
@click.option("-v", "verbose", count=True, help="Verbose stderr logging.")
@click.pass_context
def main(
    ctx: click.Context,
    bridge_alias: str | None,
    bridge_ip: str | None,
    app_key: str | None,
    json_flag: bool,
    jsonl_flag: bool,
    quiet: bool,
    timeout: float,
    config_path: str | None,
    concurrency: int | None,
    no_cloud: bool,
    no_probe: bool,
    verbose: int,
) -> None:
    """hue-cli — deterministic local-LAN CLI for Philips Hue Bridges."""
    fmt = detect(
        force_json=json_flag,
        force_jsonl=jsonl_flag,
        quiet=quiet,
        stdout_is_tty=sys.stdout.isatty(),
    )

    ctx.ensure_object(dict)
    ctx.obj["format"] = fmt
    ctx.obj["bridge_alias"] = bridge_alias
    ctx.obj["bridge_ip"] = bridge_ip
    ctx.obj["app_key"] = app_key
    ctx.obj["timeout"] = timeout
    ctx.obj["config_path"] = config_path
    ctx.obj["concurrency"] = concurrency
    ctx.obj["no_cloud"] = no_cloud
    ctx.obj["no_probe"] = no_probe
    ctx.obj["verbose"] = verbose

    # Wire -v / -vv plus optional [logging] file (§7.3). Config loading is
    # best-effort here: a config-file syntax error must not break ``--help``
    # / ``--version``, so we fall back to verbose-flag-only on any
    # ConfigError. The config_cmd verb path will surface real config errors
    # to operators who run ``hue-cli config show``.
    _setup_logging_from_config(verbose, config_path)

    # Tests inject a fake wrapper directly via ``runner.invoke(..., obj={"wrapper": fake})``;
    # for those invocations ``ctx.obj["wrapper"]`` is already populated and we leave it alone.
    # Otherwise, build a real ``HueWrapper`` from credentials when available so verbs that
    # need a connected bridge can find one in ``ctx.obj["wrapper"]``.
    if "wrapper" not in ctx.obj:
        ctx.obj["wrapper"] = _resolve_wrapper(bridge_alias, bridge_ip, app_key)


def _setup_logging_from_config(verbose: int, config_path: str | None) -> None:
    """Apply the verbose flag and (optionally) attach the [logging] file handler.

    Reads ``[logging] file`` from the resolved :class:`HueConfig` so a
    persistent path-based audit log captures ``-v``/``-vv`` output alongside
    stderr. Config-load failures degrade silently — verbose flags still work,
    operators see the real config error when they run a verb that touches
    the config explicitly (``hue-cli config show``/``validate``).
    """

    file_path: str | None = None
    try:
        from pathlib import Path

        from hue_cli.config import load_config

        explicit = Path(config_path).expanduser() if config_path else None
        cfg = load_config(explicit_path=explicit)
        file_path = cfg.logging.file
    except Exception:
        # Any config error -> degrade to flag-only logging. The dedicated
        # config_cmd verb path is where operators see config errors loud.
        file_path = None

    setup_logging(verbose, file_path)


def _resolve_wrapper(
    bridge_alias: str | None,
    bridge_ip: str | None,
    app_key: str | None,
) -> object | None:
    """Return a ``HueWrapper`` if credentials are available, else ``None``.

    Resolution: ``--bridge-ip`` + ``--app-key`` overrides everything; otherwise consult the
    credentials store for ``--bridge`` (if given) or the single-paired-bridge default.
    Verbs that don't need a bridge (``bridge discover``, ``bridge pair``, ``--help``, etc.)
    tolerate ``None`` and short-circuit on their own.
    """
    try:
        from hue_cli import credentials
        from hue_cli.wrapper import HueWrapper
    except ImportError:
        return None

    if bridge_ip is not None and app_key is not None:
        return HueWrapper(bridge_ip, app_key)

    # Catch ONLY the not-paired-yet case here. Other credential errors
    # (mode 0644, unknown version, malformed JSON) are real operator-visible
    # problems that must surface — not silently re-routed to a generic
    # "No active bridge wrapper" downstream.
    try:
        store = credentials.load()
    except credentials.MissingCredentialsError:
        return None

    if not store.bridges:
        return None

    if bridge_alias is not None:
        creds = store.bridges.get(bridge_alias)
        if creds is None:
            return None
        return HueWrapper(creds.host, creds.app_key)

    if len(store.bridges) == 1:
        creds = next(iter(store.bridges.values()))
        return HueWrapper(creds.host, creds.app_key)

    return None


# --- Verb registration -------------------------------------------------------

# Part B verbs (always available — owned by Engineer B).
main.add_command(list_group, name="list")
main.add_command(info_cmd, name="info")
main.add_command(on_cmd, name="on")
main.add_command(off_cmd, name="off")
main.add_command(toggle_cmd, name="toggle")
main.add_command(set_cmd, name="set")
main.add_command(scene_group, name="scene")
main.add_command(sensor_group, name="sensor")
main.add_command(group_cmd_group, name="group")
main.add_command(config_group, name="config")
main.add_command(batch_cmd, name="batch")


# Part A verbs — registered only when Engineer A's modules are importable.
# This keeps test_smoke.py green during parallel development. At integration
# time these imports will always succeed.
def _try_register_part_a() -> None:
    """Best-effort registration of Engineer A's verb groups."""
    try:
        from hue_cli.verbs.bridge import bridge_group
    except ImportError:
        pass
    else:
        main.add_command(bridge_group, name="bridge")

    try:
        from hue_cli.verbs.auth import auth_group
    except ImportError:
        pass
    else:
        main.add_command(auth_group, name="auth")


_try_register_part_a()
