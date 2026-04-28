"""Scene verbs (FR-39..42).

Sub-commands:

* ``scene apply <scene-name|scene-id> [--transition <ms>]`` — FR-39, FR-40, FR-41
* ``scene list`` — FR-42 (alias for ``hue-cli list scenes``)

Resolution rules per FR-39 / FR-40:

* If the operator passes a scene id (alphanumeric, bridge-assigned, ~15-16 chars per
  the SRD review note), apply it directly without name lookup.
* Otherwise, case-insensitive name match against ``bridge.scenes``. Multiple matches
  exit 64 (:class:`AmbiguousTargetError`) listing the candidate ids and group names so
  the operator can disambiguate by passing the id.
* Unknown name exits 4 (:class:`NotFoundError`).

For modern ``GroupScene`` entries the wrapper applies via the scene's owning group;
for legacy ``LightScene`` entries (``group_id is None``) the wrapper falls back to
the all-lights group recall — see :meth:`HueWrapper.apply_scene`.

``--transition <ms>`` (FR-41) translates to ``transitiontime`` in deciseconds via
``round(ms/100)`` per §10.6.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Any

import click

from hue_cli.errors import AmbiguousTargetError, NotFoundError, emit_structured_error
from hue_cli.output import OutputFormat
from hue_cli.verbs.list_cmd import list_scenes

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


def _ms_to_deciseconds(ms: int) -> int:
    """Translate a millisecond transition (FR-41) to the wire ``transitiontime`` in ds."""
    return round(ms / 100)


def _resolve_scene(
    scenes: list[dict[str, Any]],
    target: str,
) -> dict[str, Any]:
    """Resolve ``target`` (scene id or case-insensitive name) to one scene record.

    Raises :class:`NotFoundError` (exit 4) if no match, :class:`AmbiguousTargetError`
    (exit 64) if multiple name matches.
    """
    # Direct id match takes precedence — operators reach for the id when their
    # name is ambiguous, and we shouldn't loop over names looking for an id-shape
    # collision. Scene ids are alphanumeric ~15-16 chars, distinct from names.
    for scene in scenes:
        if scene.get("id") == target:
            return scene

    casefold_target = target.casefold()
    matches = [s for s in scenes if str(s.get("name", "")).casefold() == casefold_target]
    if not matches:
        raise NotFoundError(
            f"scene {target!r} not found on bridge",
            hint="Run `hue-cli list scenes` to see available scenes.",
        )
    if len(matches) > 1:
        candidates = ", ".join(
            f"{s.get('id')}(group={s.get('group_id') or 'all'})" for s in matches
        )
        raise AmbiguousTargetError(
            f"scene name {target!r} is ambiguous; pass id instead. Candidates: {candidates}",
        )
    return matches[0]


@click.group(name="scene")
def scene_group() -> None:
    """Apply or list saved scenes (FR-39..42)."""


@scene_group.command("apply")
@click.argument("target")
@click.option(
    "--transition",
    "transition_ms",
    type=int,
    default=None,
    help="Transition time in milliseconds (FR-41).",
)
@click.pass_context
def scene_apply(ctx: click.Context, target: str, transition_ms: int | None) -> None:
    """Apply scene TARGET (id or case-insensitive name) (FR-39..41)."""
    wrapper = _get_wrapper(ctx)
    fmt = _get_format(ctx)
    json_mode = fmt in (OutputFormat.JSON, OutputFormat.JSONL)

    transitiontime = _ms_to_deciseconds(transition_ms) if transition_ms is not None else None

    async def _run() -> None:
        # Match the set/onoff verbs: hold one connection across the resolve +
        # dispatch pair so we don't pay two TCP/TLS handshakes per apply. The
        # wrapper is idempotent under nested ``async with`` (its
        # ``_owns_connection`` flag tracks the outer scope) so this is safe even
        # on direct ``HueWrapper`` instances.
        async with wrapper:
            scenes = await wrapper.list_scenes_records()
            scene = _resolve_scene(scenes, target)
            await wrapper.apply_scene(
                scene_id=str(scene["id"]),
                group_id=scene.get("group_id"),
                transitiontime=transitiontime,
            )

    try:
        asyncio.run(_run())
    except AmbiguousTargetError as exc:
        emit_structured_error(exc, target=target, json_mode=json_mode)
        sys.exit(exc.exit_code)
    except NotFoundError as exc:
        emit_structured_error(exc, target=target, json_mode=json_mode)
        sys.exit(exc.exit_code)


@scene_group.command("list")
@click.option("--filter", "filters", multiple=True)
@click.pass_context
def scene_list(ctx: click.Context, filters: tuple[str, ...]) -> None:
    """Alias for ``hue-cli list scenes`` (FR-42)."""
    # Delegate to the existing list-scenes Click callback so output format and
    # filter semantics stay byte-identical with `hue-cli list scenes`.
    ctx.invoke(list_scenes, filters=filters)
