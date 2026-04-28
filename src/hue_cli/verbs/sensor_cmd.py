"""Sensor verbs (FR-46, FR-47).

Sub-commands:

* ``sensor list`` — FR-46 (alias for ``hue-cli list sensors``)
* ``sensor info <sensor-name|id>`` — FR-47 (type-specific state shaping)

The type-specific shaping is delegated to :func:`hue_cli.wrapper.shape_sensor_info`
which keeps the per-type projection a pure function unit-testable without Click.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Any

import click

from hue_cli.errors import NotFoundError, emit_structured_error
from hue_cli.output import OutputFormat, emit_json, emit_jsonl
from hue_cli.verbs.list_cmd import list_sensors
from hue_cli.wrapper import shape_sensor_info

if TYPE_CHECKING:
    from hue_cli._protocols import HueWrapperProto


def _get_wrapper(ctx: click.Context) -> HueWrapperProto:
    obj = ctx.obj or {}
    wrapper = obj.get("wrapper") if isinstance(obj, dict) else None
    if wrapper is None:
        raise click.ClickException("No active bridge wrapper. Run `hue-cli bridge pair` first.")
    return wrapper  # type: ignore[no-any-return]


def _get_format(ctx: click.Context) -> OutputFormat:
    obj = ctx.obj or {}
    fmt = obj.get("format") if isinstance(obj, dict) else None
    return fmt if isinstance(fmt, OutputFormat) else OutputFormat.TEXT


def _resolve_sensor(records: list[dict[str, Any]], target: str) -> dict[str, Any]:
    """Resolve ``target`` (sensor id or case-insensitive name) to one record.

    Raises :class:`NotFoundError` (exit 4) if no match. Sensor names on a
    bridge are typically unique within a type (the Hue app deduplicates by
    appending an index), so name ambiguity is not modeled here — id is the
    operator's escape hatch if duplicates ever occur.
    """
    casefold_target = target.casefold()
    for record in records:
        if str(record.get("id", "")) == target:
            return record
        if str(record.get("name", "")).casefold() == casefold_target:
            return record
    raise NotFoundError(
        f"sensor {target!r} not found on bridge",
        hint="Run `hue-cli list sensors` to see available sensors.",
    )


@click.group(name="sensor")
def sensor_group() -> None:
    """Read-only sensor info and listing (FR-46, FR-47)."""


@sensor_group.command("list")
@click.option("--filter", "filters", multiple=True)
@click.pass_context
def sensor_list(ctx: click.Context, filters: tuple[str, ...]) -> None:
    """Alias for ``hue-cli list sensors`` (FR-46)."""
    ctx.invoke(list_sensors, filters=filters)


@sensor_group.command("info")
@click.argument("target")
@click.pass_context
def sensor_info(ctx: click.Context, target: str) -> None:
    """Show TARGET sensor's type-shaped state (FR-47)."""
    wrapper = _get_wrapper(ctx)
    fmt = _get_format(ctx)
    json_mode = fmt in (OutputFormat.JSON, OutputFormat.JSONL)

    async def _run() -> dict[str, Any]:
        records = await wrapper.list_sensors_records()
        record = _resolve_sensor(records, target)
        return shape_sensor_info(record)

    try:
        shaped = asyncio.run(_run())
    except NotFoundError as exc:
        emit_structured_error(exc, target=target, json_mode=json_mode)
        sys.exit(exc.exit_code)

    if fmt is OutputFormat.QUIET:
        return
    if fmt is OutputFormat.JSON:
        click.echo(emit_json(shaped))
        return
    if fmt is OutputFormat.JSONL:
        click.echo(next(iter(emit_jsonl([shaped]))))
        return
    # TEXT: stable key: value listing.
    for key in sorted(shaped.keys()):
        click.echo(f"{key}: {shaped[key]}")
