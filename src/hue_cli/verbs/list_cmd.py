"""List verbs (FR-11..18).

Sub-commands:

* ``lights``    — FR-11
* ``rooms``     — FR-12
* ``zones``     — FR-13
* ``scenes``    — FR-14
* ``sensors``   — FR-15
* ``schedules`` — FR-16 (uses wrapper's §4.5 direct-aiohttp fallback)
* ``all``       — FR-17

Each subcommand fetches records via the active :class:`HueWrapperProto`
(provided through Click's ``ctx.obj``), applies any ``--filter key=value``
predicates (FR-18), and emits via :mod:`hue_cli.output`.

Brightness is exposed to operators as a 0-100 percent on
``state.brightness_percent`` (FR-11) — translation is performed by Engineer
A's wrapper when it materializes the §10.2 Light record. The verb here
trusts the wrapper-supplied value and does not re-translate.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import click

from hue_cli.output import OutputFormat, emit_json, emit_jsonl, emit_text

if TYPE_CHECKING:
    from hue_cli._protocols import HueWrapperProto


# --- Filter helpers ----------------------------------------------------------


def _parse_filters(filters: tuple[str, ...]) -> list[tuple[str, str]]:
    """Parse ``--filter key=value`` arguments into ``(key, value)`` pairs.

    FR-18: simple substring match on top-level fields, AND-combined.
    """
    out: list[tuple[str, str]] = []
    for raw in filters:
        if "=" not in raw:
            raise click.UsageError(f"--filter expects key=value, got: {raw!r}")
        key, _, value = raw.partition("=")
        out.append((key.strip(), value.strip()))
    return out


def _match_filters(record: dict[str, Any], filters: list[tuple[str, str]]) -> bool:
    """Return True if ``record`` satisfies every (key, value) filter (AND)."""
    for key, value in filters:
        actual = record.get(key)
        if actual is None:
            return False
        if value.lower() not in str(actual).lower():
            return False
    return True


def _apply_filters(
    records: list[dict[str, Any]], filters: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    if not filters:
        return records
    return [r for r in records if _match_filters(r, filters)]


# --- Emission helper ---------------------------------------------------------


def _emit_records(
    records: list[dict[str, Any]],
    columns: list[str],
    fmt: OutputFormat,
) -> str:
    """Serialize records per the active output format. Returns the rendered text."""
    if fmt is OutputFormat.QUIET:
        return ""
    if fmt is OutputFormat.JSON:
        return emit_json(records) + "\n"
    if fmt is OutputFormat.JSONL:
        return "\n".join(emit_jsonl(records)) + ("\n" if records else "")
    return emit_text(records, columns) + "\n"


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


# --- The group ----------------------------------------------------------------


@click.group(name="list")
def list_group() -> None:
    """List bridge resources (lights, rooms, zones, scenes, sensors, schedules)."""


# --- Subcommands -------------------------------------------------------------


@list_group.command("lights")
@click.option("--filter", "filters", multiple=True, help="key=value substring filter.")
@click.pass_context
def list_lights(ctx: click.Context, filters: tuple[str, ...]) -> None:
    """List all lights known to the bridge (FR-11)."""
    wrapper = _get_wrapper(ctx)
    fmt = _get_format(ctx)
    records = asyncio.run(wrapper.list_lights_records())
    records = _apply_filters(records, _parse_filters(filters))
    columns = ["id", "name", "type", "model_id", "product_name"]
    click.echo(_emit_records(records, columns, fmt), nl=False)


@list_group.command("rooms")
@click.option("--filter", "filters", multiple=True)
@click.pass_context
def list_rooms(ctx: click.Context, filters: tuple[str, ...]) -> None:
    """List all rooms (groups of type=Room) (FR-12)."""
    wrapper = _get_wrapper(ctx)
    fmt = _get_format(ctx)
    groups = asyncio.run(wrapper.list_groups_records())
    rooms = [g for g in groups if g.get("type") == "Room"]
    rooms = _apply_filters(rooms, _parse_filters(filters))
    columns = ["id", "name", "class", "light_ids"]
    click.echo(_emit_records(rooms, columns, fmt), nl=False)


@list_group.command("zones")
@click.option("--filter", "filters", multiple=True)
@click.pass_context
def list_zones(ctx: click.Context, filters: tuple[str, ...]) -> None:
    """List all zones (groups of type=Zone) (FR-13)."""
    wrapper = _get_wrapper(ctx)
    fmt = _get_format(ctx)
    groups = asyncio.run(wrapper.list_groups_records())
    zones = [g for g in groups if g.get("type") == "Zone"]
    zones = _apply_filters(zones, _parse_filters(filters))
    columns = ["id", "name", "light_ids"]
    click.echo(_emit_records(zones, columns, fmt), nl=False)


@list_group.command("scenes")
@click.option("--filter", "filters", multiple=True)
@click.pass_context
def list_scenes(ctx: click.Context, filters: tuple[str, ...]) -> None:
    """List all scenes (FR-14). Legacy LightScenes have group_id null."""
    wrapper = _get_wrapper(ctx)
    fmt = _get_format(ctx)
    records = asyncio.run(wrapper.list_scenes_records())
    records = _apply_filters(records, _parse_filters(filters))
    columns = ["id", "name", "group_id", "last_updated"]
    click.echo(_emit_records(records, columns, fmt), nl=False)


@list_group.command("sensors")
@click.option("--filter", "filters", multiple=True)
@click.pass_context
def list_sensors(ctx: click.Context, filters: tuple[str, ...]) -> None:
    """List all sensors (FR-15). Battery field included when present."""
    wrapper = _get_wrapper(ctx)
    fmt = _get_format(ctx)
    records = asyncio.run(wrapper.list_sensors_records())
    records = _apply_filters(records, _parse_filters(filters))
    columns = ["id", "name", "type", "model_id"]
    click.echo(_emit_records(records, columns, fmt), nl=False)


@list_group.command("schedules")
@click.option("--filter", "filters", multiple=True)
@click.pass_context
def list_schedules(ctx: click.Context, filters: tuple[str, ...]) -> None:
    """List all schedules (FR-16, read-only). Uses §4.5 direct-aiohttp fallback."""
    wrapper = _get_wrapper(ctx)
    fmt = _get_format(ctx)
    records = asyncio.run(wrapper.list_schedules_records())
    records = _apply_filters(records, _parse_filters(filters))
    columns = ["id", "name", "status", "localtime"]
    click.echo(_emit_records(records, columns, fmt), nl=False)


@list_group.command("all")
@click.pass_context
def list_all(ctx: click.Context) -> None:
    """Aggregate all six listings into a single object (FR-17)."""
    wrapper = _get_wrapper(ctx)
    fmt = _get_format(ctx)

    async def _fetch_all() -> dict[str, list[dict[str, Any]]]:
        lights, groups, scenes, sensors, schedules = await asyncio.gather(
            wrapper.list_lights_records(),
            wrapper.list_groups_records(),
            wrapper.list_scenes_records(),
            wrapper.list_sensors_records(),
            wrapper.list_schedules_records(),
        )
        rooms = [g for g in groups if g.get("type") == "Room"]
        zones = [g for g in groups if g.get("type") == "Zone"]
        return {
            "lights": lights,
            "rooms": rooms,
            "zones": zones,
            "scenes": scenes,
            "sensors": sensors,
            "schedules": schedules,
        }

    aggregate = asyncio.run(_fetch_all())

    if fmt is OutputFormat.QUIET:
        return
    if fmt is OutputFormat.JSON:
        click.echo(emit_json(aggregate), nl=True)
        return
    if fmt is OutputFormat.JSONL:
        # JSONL of an aggregate emits one line per category as
        # ``{"category": <name>, "records": [...]}`` for stable consumption.
        lines = list(emit_jsonl([{"category": k, "records": v} for k, v in aggregate.items()]))
        click.echo("\n".join(lines), nl=True)
        return
    # TEXT — emit each category's table with a heading.
    parts: list[str] = []
    for category, records in aggregate.items():
        parts.append(f"== {category} ({len(records)}) ==")
        if records:
            cols = list(records[0].keys())[:5]
            parts.append(emit_text(records, cols))
        parts.append("")
    click.echo("\n".join(parts), nl=False)
