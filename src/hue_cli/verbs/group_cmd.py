"""Group verb (FR-51).

Hue's "group" is bridge-stored — it's a Room or a Zone (§5.10). The CLI does
not maintain operator-config groups; instead it reads Rooms + Zones from the
bridge and exposes them via ``@<name>`` target syntax (FR-49 / FR-50, handled
in :mod:`hue_cli.wrapper`).

This module provides the ``group list`` verb (FR-51), which is an alias that
merges ``list rooms`` + ``list zones`` into a single emission. Group dispatch
for ``on`` / ``off`` / ``set`` is handled elsewhere via ``Group.set_action``
(FR-52, see :mod:`hue_cli.verbs.onoff_cmd` and :mod:`hue_cli.verbs.set_cmd`).

Filtering: any group whose ``type`` is not ``Room`` or ``Zone`` (e.g.,
``LightGroup``, ``Luminaire``, ``LightSource``, ``Entertainment``) is
intentionally excluded — operators only target Rooms and Zones from the
CLI, and the bridge auto-creates these other group types for internal
bookkeeping that operators should not have to think about.
"""

from __future__ import annotations

import asyncio

import click

from hue_cli.output import OutputFormat
from hue_cli.verbs.list_cmd import (
    _apply_filters,
    _emit_records,
    _get_format,
    _get_wrapper,
    _parse_filters,
)

_GROUP_TYPES = ("Room", "Zone")


@click.group(name="group")
def group_cmd_group() -> None:
    """Group operations (Rooms + Zones, FR-51).

    Hue groups are bridge-stored. The CLI does not maintain a local
    ``[groups]`` section in config — see SRD §5.10. Targets resolve via
    ``@<name>`` / ``@room:<name>`` / ``@zone:<name>`` at command time.
    """


@group_cmd_group.command("list")
@click.option("--filter", "filters", multiple=True, help="key=value substring filter.")
@click.pass_context
def group_list(ctx: click.Context, filters: tuple[str, ...]) -> None:
    """List bridge groups — Rooms and Zones merged (FR-51).

    Equivalent to ``hue-cli list rooms`` + ``hue-cli list zones`` in a single
    invocation. Other group types reported by the bridge (``LightGroup``,
    ``Luminaire``, ``LightSource``, ``Entertainment``) are excluded — they are
    bridge-internal bookkeeping and not user-facing operation targets.

    ``--filter type=Room`` (or ``type=Zone``) constrains the output to one
    type. ``--filter`` is the standard FR-18 case-insensitive substring
    matcher, AND-combined across multiple flags.
    """
    wrapper = _get_wrapper(ctx)
    fmt: OutputFormat = _get_format(ctx)
    groups = asyncio.run(wrapper.list_groups_records())
    rooms_and_zones = [g for g in groups if g.get("type") in _GROUP_TYPES]
    rooms_and_zones = _apply_filters(rooms_and_zones, _parse_filters(filters))
    columns = ["id", "type", "name", "class", "light_ids"]
    click.echo(_emit_records(rooms_and_zones, columns, fmt), nl=False)
