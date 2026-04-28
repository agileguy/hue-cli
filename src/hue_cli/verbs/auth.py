"""``auth`` verb group — status, flush, migrate.

Per SRD FR-CRED-4..7 and the §11.2 structured-error contract.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import click

from hue_cli import credentials, wrapper
from hue_cli.credentials import MissingCredentialsError
from hue_cli.errors import HueCliError, emit_structured_error


@click.group(name="auth")
def auth_group() -> None:
    """Local credentials management."""


@auth_group.command(name="status")
@click.option("--no-probe", is_flag=True, help="Skip the live reachability probe.")
@click.option("--json", "json_mode", is_flag=True, help="Pretty JSON output.")
@click.option("--timeout", type=float, default=5.0, show_default=True, help="Probe timeout.")
def status_cmd(*, no_probe: bool, json_mode: bool, timeout: float) -> None:
    """List paired bridges with optional reachability probe (FR-CRED-4).

    Empty/missing credentials emit ``[]`` and exit 0 — not an error.
    """

    try:
        records = asyncio.run(_status_impl(probe=not no_probe, timeout=timeout))
    except HueCliError as exc:
        emit_structured_error(exc, target=None, json_mode=json_mode)
        raise SystemExit(exc.exit_code) from exc

    _emit_records(records, json_mode=json_mode)


@auth_group.command(name="flush")
@click.option("--bridge", "bridge_target", default=None, help="Flush only this bridge id/alias.")
@click.option("--json", "json_mode", is_flag=True, help="Pretty JSON output.")
def flush_cmd(*, bridge_target: str | None, json_mode: bool) -> None:
    """Wipe credentials (FR-CRED-5). ``--bridge`` removes only one entry."""

    if bridge_target is None:
        try:
            credentials.flush_all()
        except HueCliError as exc:
            emit_structured_error(exc, target=None, json_mode=json_mode)
            raise SystemExit(exc.exit_code) from exc
        click.echo(
            "Credentials cleared. The bridge whitelist still contains this app's entry; "
            "remove it via the Hue mobile app or https://account.meethue.com/apps.",
            err=True,
        )
        return

    bridge_id = wrapper.normalize_id(bridge_target)
    removed = credentials.remove_bridge(bridge_id)
    if not removed:
        click.echo(
            f"INFO no credentials entry for {bridge_target} (canonical: {bridge_id})",
            err=True,
        )
        # Removing a non-existent entry is idempotent — exit 0.
        return
    click.echo(
        f"Removed credentials for {bridge_id}. The whitelist entry on the bridge persists; "
        "use the Hue mobile app to revoke it.",
        err=True,
    )


@auth_group.command(name="migrate")
def migrate_cmd() -> None:
    """Forward-looking schema migration verb (FR-CRED-7).

    v1 stub: detects that the on-disk schema is current, emits an INFO line, exits 0.
    """

    try:
        credentials.load()
    except MissingCredentialsError:
        click.echo("INFO no credentials file present, nothing to migrate", err=True)
        return
    except HueCliError as exc:
        emit_structured_error(exc, target=None)
        raise SystemExit(exc.exit_code) from exc

    click.echo(
        f"INFO credentials at v{credentials.CURRENT_VERSION}, no migration needed",
        err=True,
    )


async def _status_impl(*, probe: bool, timeout: float) -> list[dict[str, Any]]:
    """Shared async implementation. Returns one record per paired bridge.

    ``bridge list`` (in ``verbs/bridge.py``) calls this directly to avoid duplication.
    """

    try:
        store = credentials.load()
    except MissingCredentialsError:
        return []

    if not store.bridges:
        return []

    records: list[dict[str, Any]] = []
    if probe:
        probe_results = await asyncio.gather(
            *(wrapper.discover_one(creds.host, timeout) for creds in store.bridges.values()),
            return_exceptions=True,
        )
    else:
        probe_results = [None] * len(store.bridges)

    for (bid, creds), probe_result in zip(store.bridges.items(), probe_results, strict=False):
        record: dict[str, Any] = {
            "id": bid,
            "name": creds.name,
            "host": creds.host,
            "paired_at": creds.paired_at,
        }
        if probe:
            if isinstance(probe_result, BaseException) or probe_result is None:
                record["reachable"] = False
            else:
                record["reachable"] = True
        records.append(record)

    return records


def _emit_records(records: list[dict[str, Any]], *, json_mode: bool) -> None:
    """Print paired-bridge records: pretty JSON when --json or non-tty stdout, else text."""

    if json_mode or not sys.stdout.isatty():
        click.echo(json.dumps(records, indent=2 if json_mode else None))
        return

    if not records:
        click.echo("No paired bridges. Run: hue-cli bridge pair")
        return

    headers = ["id", "name", "host", "paired_at", "reachable"]
    click.echo("\t".join(headers))
    for rec in records:
        click.echo(
            "\t".join(str(rec.get(col, "")) if rec.get(col) is not None else "" for col in headers)
        )


__all__ = ["_status_impl", "auth_group"]
