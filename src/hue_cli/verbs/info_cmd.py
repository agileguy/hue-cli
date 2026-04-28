"""Info verb (FR-19..21).

``info <target>`` resolves a target by lookup precedence (light → room →
zone → scene → sensor → IP → bridge alias) and emits the corresponding §10
record. ``info bridge`` is the explicit Bridge-record path (FR-21) and emits
the full §10.1 shape including network/zigbee/whitelist extras.

Resolution itself is delegated to the wrapper (``resolve_target``) so the
verb stays free of bridge-resource lookup logic — the wrapper is the single
place where Hue's resource model is interpreted.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import click

from hue_cli.output import OutputFormat, emit_json, emit_jsonl

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


@click.command(name="info")
@click.argument("target")
@click.pass_context
def info_cmd(ctx: click.Context, target: str) -> None:
    """Show full state of one TARGET (FR-19..21).

    TARGET may be a light id/name, ``@room-name``, ``@zone-name``, scene
    name/id, sensor name/id, or the literal ``bridge`` (FR-21).
    """
    wrapper = _get_wrapper(ctx)
    fmt = _get_format(ctx)

    if target == "bridge":
        record = asyncio.run(wrapper.get_bridge_record())
    else:
        resolved = asyncio.run(wrapper.resolve_target(target))
        record = resolved.get("record", {})

    if fmt is OutputFormat.QUIET:
        return
    if fmt is OutputFormat.JSON:
        click.echo(emit_json(record))
        return
    if fmt is OutputFormat.JSONL:
        click.echo(next(iter(emit_jsonl([record]))))
        return
    # TEXT: emit a key: value listing with stable ordering.
    for key in sorted(record.keys()):
        click.echo(f"{key}: {record[key]}")
