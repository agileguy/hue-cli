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
from hue_cli.output import detect
from hue_cli.verbs.config_cmd import config_group
from hue_cli.verbs.info_cmd import info_cmd
from hue_cli.verbs.list_cmd import list_group
from hue_cli.verbs.onoff_cmd import off_cmd, on_cmd, toggle_cmd

# --- Async graceful runner ---------------------------------------------------


def _run_async_graceful(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run an async coroutine via :func:`asyncio.run` with signal handling.

    SIGINT  → exits 130 (FR-54c, Phase 1 shape).
    SIGTERM → exits 143.

    Phase 3 will elaborate this for batch-aware partial-result behavior
    (emit ``{"event":"interrupted","completed":N,"pending":M}`` JSONL line
    before exit). Phase 1 just needs the exit-code plumbing correct.
    """
    exit_sigint = 130
    exit_sigterm = 143

    def _handle_sigterm(*_args: object) -> None:
        # SIGTERM is unsettable on Windows main thread; if we get here we're
        # on POSIX. Translate to a ``SystemExit(143)``.
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

    # Tests inject a fake wrapper directly via ``runner.invoke(..., obj={"wrapper": fake})``;
    # for those invocations ``ctx.obj["wrapper"]`` is already populated and we leave it alone.
    # Otherwise, build a real ``HueWrapper`` from credentials when available so verbs that
    # need a connected bridge can find one in ``ctx.obj["wrapper"]``.
    if "wrapper" not in ctx.obj:
        ctx.obj["wrapper"] = _resolve_wrapper(bridge_alias, bridge_ip, app_key)


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
main.add_command(config_group, name="config")


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
