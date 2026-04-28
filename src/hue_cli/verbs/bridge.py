"""``bridge`` verb group — discover, pair, unpair, list.

Per SRD FR-1..10 and FR-CRED-4a (``bridge list`` aliases into ``auth status``).
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

import click

from hue_cli import credentials, wrapper
from hue_cli.config import HueConfig, load_config
from hue_cli.credentials import BridgeCredentials, MissingCredentialsError
from hue_cli.errors import HueCliError, NotFoundError, UsageError, emit_structured_error
from hue_cli.verbs.auth import _emit_records, _status_impl


@click.group(name="bridge")
def bridge_group() -> None:
    """Bridge discovery, pairing, and credentials lookups."""


@bridge_group.command(name="discover")
@click.option("--bridge-ip", default=None, help="Probe a specific IP, skip mDNS+NUPNP.")
@click.option("--timeout", type=float, default=5.0, show_default=True, help="Discovery deadline.")
@click.option("--cloud/--no-cloud", "cloud_flag", default=None, help="Override cloud_discovery.")
@click.option("--json", "json_mode", is_flag=True, help="Pretty JSON output.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override config file path.",
)
def discover_cmd(
    *,
    bridge_ip: str | None,
    timeout: float,
    cloud_flag: bool | None,
    json_mode: bool,
    config_path: Path | None,
) -> None:
    """Find Hue bridges via mDNS, NUPNP, and configured IPs (FR-1..5)."""

    try:
        config = load_config(config_path)
    except HueCliError as exc:
        emit_structured_error(exc, target=None, json_mode=json_mode)
        raise SystemExit(exc.exit_code) from exc

    cloud = config.cloud_discovery if cloud_flag is None else cloud_flag

    try:
        if bridge_ip is not None:
            results = asyncio.run(_discover_one_impl(bridge_ip, timeout))
        else:
            configured_ips = [b.host for b in config.bridges.values() if b.host]
            results = asyncio.run(
                wrapper.discover(timeout, cloud=cloud, configured_ips=configured_ips)
            )
    except HueCliError as exc:
        emit_structured_error(exc, target=bridge_ip, json_mode=json_mode)
        raise SystemExit(exc.exit_code) from exc

    if not results:
        click.echo("INFO no bridges found", err=True)

    payload = [
        {
            "id": entry.id,
            "host": entry.host,
            "supports_v2": entry.supports_v2,
            "source": entry.source,
        }
        for entry in results
    ]

    if json_mode or not sys.stdout.isatty():
        click.echo(json.dumps(payload, indent=2 if json_mode else None))
        return

    if not payload:
        return

    headers = ["id", "host", "supports_v2", "source"]
    click.echo("\t".join(headers))
    for rec in payload:
        click.echo("\t".join(str(rec[col]) for col in headers))


@bridge_group.command(name="pair")
@click.option("--bridge-ip", default=None, help="Skip discovery; pair this IP directly.")
@click.option("--non-interactive", is_flag=True, help="Skip the press-Enter prompt.")
@click.option(
    "--retry-interval",
    type=float,
    default=2.0,
    show_default=True,
    help="Seconds between LinkButtonNotPressed retries.",
)
@click.option("--timeout", type=float, default=30.0, show_default=True, help="Total pair budget.")
@click.option("--json", "json_mode", is_flag=True, help="Pretty JSON output.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override config file path.",
)
def pair_cmd(
    *,
    bridge_ip: str | None,
    non_interactive: bool,
    retry_interval: float,
    timeout: float,
    json_mode: bool,
    config_path: Path | None,
) -> None:
    """Run the link-button registration flow (FR-6..9)."""

    try:
        config = load_config(config_path)
    except HueCliError as exc:
        emit_structured_error(exc, target=bridge_ip, json_mode=json_mode)
        raise SystemExit(exc.exit_code) from exc

    try:
        target = asyncio.run(_resolve_pair_target(bridge_ip, config, timeout))
    except HueCliError as exc:
        emit_structured_error(exc, target=bridge_ip, json_mode=json_mode)
        raise SystemExit(exc.exit_code) from exc

    click.echo(
        f"Found Hue Bridge {target.id} at {target.host}",
        err=True,
    )
    if not non_interactive:
        click.echo(
            f"Press the link button on the Hue Bridge within {timeout:.0f} seconds, "
            "then press Enter...",
            err=True,
        )
        try:
            sys.stdin.readline()
        except KeyboardInterrupt as exc:
            emit_structured_error(
                UsageError("pair aborted by user", hint="Re-run hue-cli bridge pair."),
                target=target.host,
                json_mode=json_mode,
            )
            raise SystemExit(64) from exc

    app_name = _build_devicetype("hue-cli", socket.gethostname().split(".")[0])
    try:
        app_key = asyncio.run(
            wrapper.pair(
                target.host,
                app_name,
                retry_interval=retry_interval,
                timeout=timeout,
            )
        )
    except HueCliError as exc:
        emit_structured_error(exc, target=target.host, json_mode=json_mode)
        raise SystemExit(exc.exit_code) from exc

    creds = BridgeCredentials(
        app_key=app_key,
        host=target.host,
        name=f"Hue Bridge - {target.id[-6:].upper()}",
        paired_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    credentials.append_bridge(target.id, creds)

    payload = {
        "id": target.id,
        "host": target.host,
        "supports_v2": target.supports_v2,
        "paired_at": creds.paired_at,
    }
    if json_mode:
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(f"Paired. Stored app-key for bridge {target.id}.")


@bridge_group.command(name="unpair")
@click.argument("target", required=False)
@click.option("--json", "json_mode", is_flag=True, help="Pretty JSON output.")
def unpair_cmd(*, target: str | None, json_mode: bool) -> None:
    """Local-only unpair: drop the bridge from the credentials file (FR-10/10a)."""

    try:
        store = credentials.load()
    except MissingCredentialsError:
        click.echo("No credentials to unpair.", err=True)
        return
    except HueCliError as exc:
        emit_structured_error(exc, target=target, json_mode=json_mode)
        raise SystemExit(exc.exit_code) from exc

    if target is None:
        if len(store.bridges) == 1:
            if not sys.stdin.isatty():
                err = UsageError(
                    "no target given and stdin not a tty",
                    hint="Pass the bridge id or alias as an argument.",
                )
                emit_structured_error(err, target=None, json_mode=json_mode)
                raise SystemExit(err.exit_code)
            (only_id,) = store.bridges.keys()
            click.echo(f"Unpair the only bridge {only_id}? [y/N]: ", nl=False, err=True)
            answer = sys.stdin.readline().strip().lower()
            if answer not in {"y", "yes"}:
                click.echo("Aborted.", err=True)
                return
            target = only_id
        else:
            err = UsageError(
                f"unpair requires a target when {len(store.bridges)} bridges are paired",
                hint="Run: hue-cli auth status to list paired bridges.",
            )
            emit_structured_error(err, target=None, json_mode=json_mode)
            raise SystemExit(err.exit_code)

    bridge_id = wrapper.normalize_id(target)
    removed = credentials.remove_bridge(bridge_id)
    if not removed:
        not_found = NotFoundError(
            f"no paired bridge matches {target!r} (canonical: {bridge_id})",
            hint="Run: hue-cli auth status to list paired bridges.",
        )
        emit_structured_error(not_found, target=target, json_mode=json_mode)
        raise SystemExit(not_found.exit_code)

    click.echo(
        f"Unpaired {bridge_id}. The whitelist entry on the bridge is unchanged; remove it "
        "via the Hue mobile app or https://account.meethue.com/apps.",
        err=True,
    )


@bridge_group.command(name="list")
@click.option("--no-probe", is_flag=True, help="Skip the live reachability probe.")
@click.option("--json", "json_mode", is_flag=True, help="Pretty JSON output.")
@click.option("--timeout", type=float, default=5.0, show_default=True, help="Probe timeout.")
def list_cmd(*, no_probe: bool, json_mode: bool, timeout: float) -> None:
    """Alias for ``hue-cli auth status`` (FR-CRED-4a)."""

    try:
        records = asyncio.run(_status_impl(probe=not no_probe, timeout=timeout))
    except HueCliError as exc:
        emit_structured_error(exc, target=None, json_mode=json_mode)
        raise SystemExit(exc.exit_code) from exc

    _emit_records(records, json_mode=json_mode)


async def _resolve_pair_target(
    bridge_ip: str | None,
    config: HueConfig,
    timeout: float,
) -> wrapper.DiscoveredBridge:
    """Resolve which bridge ``pair`` should target.

    Order: explicit ``--bridge-ip``, single mDNS hit, error if multiple/zero.
    """

    if bridge_ip is not None:
        result = await wrapper.discover_one(bridge_ip, timeout)
        if result is None:
            raise NotFoundError(
                f"{bridge_ip} did not respond as a Hue bridge",
                hint="Verify the IP and that the bridge is on the same LAN.",
            )
        return result

    configured_ips = [b.host for b in config.bridges.values() if b.host]
    found = await wrapper.discover(
        timeout=timeout,
        cloud=config.cloud_discovery,
        configured_ips=configured_ips,
    )
    if not found:
        raise NotFoundError(
            "no Hue bridges found via mDNS or configured IPs",
            hint="Pass --bridge-ip <ip> if the bridge is on a non-default interface.",
        )
    if len(found) > 1:
        ids = ", ".join(b.id for b in found)
        raise UsageError(
            f"multiple bridges discovered ({ids}); pass --bridge-ip to disambiguate",
        )
    return found[0]


async def _discover_one_impl(host: str, timeout: float) -> list[wrapper.DiscoveredBridge]:
    """Helper for ``bridge discover --bridge-ip``: probe one IP and return [] on miss (FR-5)."""

    result = await wrapper.discover_one(host, timeout)
    if result is None:
        raise NotFoundError(
            f"{host} did not respond as a Hue bridge",
            hint="Verify the IP and that the bridge is on the same LAN.",
        )
    return [result]


# --- helpers ----------------------------------------------------------------

# Hue v1 ``devicetype`` field (used during pair / create-app-key) is documented
# as "<application_name>#<devicename>" with a 40-char total cap and an allowed
# alphabet of alphanumerics, space, and ``_:-``. The bridge silently truncates
# longer values, which makes diagnostics harder; sanitize and truncate here so
# the registered devicetype is exactly what we send.
_DEVICETYPE_MAX = 40
_DEVICETYPE_ALLOWED = re.compile(r"[^A-Za-z0-9 _:\-]")


def _build_devicetype(app_name: str, device_name: str) -> str:
    """Return a Hue-v1-safe ``<app>#<device>`` string under 40 chars.

    Sanitizes both halves to the allowed alphabet (alphanumerics, space, and
    ``_:-``) by replacing illegal characters with ``-``. Then truncates the
    composed string to 40 chars from the right (we keep the prefix so the
    application name remains identifiable in the bridge whitelist).
    """

    safe_app = _DEVICETYPE_ALLOWED.sub("-", app_name) or "hue-cli"
    safe_dev = _DEVICETYPE_ALLOWED.sub("-", device_name) or "host"
    composed = f"{safe_app}#{safe_dev}"
    if len(composed) <= _DEVICETYPE_MAX:
        return composed
    # Keep the application prefix and ``#`` separator; trim the device tail.
    keep_for_dev = max(0, _DEVICETYPE_MAX - len(safe_app) - 1)
    return f"{safe_app}#{safe_dev[:keep_for_dev]}"


__all__ = ["bridge_group"]
