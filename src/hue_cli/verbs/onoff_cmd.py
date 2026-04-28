"""Power-control verbs: ``on`` / ``off`` / ``toggle`` (FR-22..26).

Dispatch shape (FR-22 / FR-23):

* light target  → ``Light.set_state(on=...)`` via the wrapper
* room/zone     → ``Group.set_action(on=...)`` via the wrapper
* literal ``all`` → ``Groups.get_all_lights_group()`` then ``set_action(on=...)``

``toggle`` (FR-24) on a single light flips ``state.on``. On a group it
implements **Decision 4 (consolidate-on)**: if every light in the group is
already on (``state.all_on == True``) turn them all off; otherwise turn them
all on. This is the operator's deliberate divergence from the Hue mobile
app's ``any_on`` toggle semantics.

Power verbs are idempotent (FR-25) — calling ``on`` on an already-on target
is a no-op exit 0. Unreachable lights (``state.reachable == False``) emit a
warning to stderr (FR-26) but the call still exits 0 if the bridge accepted
the command (the bridge queues the action for when the light returns).
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Any, cast

import click

if TYPE_CHECKING:
    from hue_cli._protocols import GroupProto, HueWrapperProto, LightProto


def _get_wrapper(ctx: click.Context) -> HueWrapperProto:
    obj = ctx.obj or {}
    wrapper = obj.get("wrapper") if isinstance(obj, dict) else None
    if wrapper is None:
        raise click.ClickException("No active bridge wrapper. Run `hue-cli bridge pair` first.")
    return wrapper  # type: ignore[no-any-return]


async def _apply_power(
    wrapper: HueWrapperProto,
    target: str,
    on: bool,
) -> dict[str, Any]:
    """Resolve the target and apply the requested power state.

    Returns a small result dict suitable for JSON / JSONL emission. For
    light targets, an ``unreachable`` warning is emitted on stderr per
    FR-26 when ``state.reachable == False``.

    Wraps the resolve + dispatch pair in ``async with wrapper`` so that the
    underlying aiohttp ``ClientSession`` stays alive across both calls. The
    Light/Group object returned by ``resolve_target`` carries a ``_request``
    bound to that session — without the shared connection lifetime, the
    follow-up ``set_state``/``set_action`` would raise
    ``RuntimeError: Session is closed`` on a real bridge.
    """
    async with wrapper:
        if target == "all":
            group = await wrapper.get_all_lights_group()
            await wrapper.group_set_on(group, on)
            return {"target": "all", "kind": "group", "on": on}

        resolved = await wrapper.resolve_target(target)
        kind = resolved.get("kind")
        record = resolved.get("record", {})
        obj = resolved.get("object")

        if kind == "light":
            await wrapper.light_set_on(cast("LightProto", obj), on)
            state = record.get("state", {}) if isinstance(record, dict) else {}
            if isinstance(state, dict) and state.get("reachable") is False:
                click.echo(
                    f"warning: light {target!r} is not reachable; command queued by bridge",
                    err=True,
                )
            return {"target": target, "kind": "light", "on": on}

        if kind in ("room", "zone"):
            await wrapper.group_set_on(cast("GroupProto", obj), on)
            return {"target": target, "kind": kind, "on": on}

        raise click.ClickException(f"target {target!r} is not a light or group (kind={kind!r})")


async def _apply_toggle(wrapper: HueWrapperProto, target: str) -> dict[str, Any]:
    """Resolve the target and flip its on/off state per FR-24 + Decision 4.

    Like ``_apply_power``, the resolve + dispatch pair runs inside a single
    ``async with wrapper`` block so the aiohttp session stays alive for the
    Light/Group object's bound ``_request`` callable.
    """
    async with wrapper:
        if target == "all":
            group = await wrapper.get_all_lights_group()
            # Decision 4 applied to the implicit "all" group: read the wrapper's
            # group record so all_on / any_on are visible.
            all_on = bool(getattr(group, "state", {}).get("all_on", False))
            next_on = not all_on
            await wrapper.group_set_on(group, next_on)
            return {"target": "all", "kind": "group", "on": next_on}

        resolved = await wrapper.resolve_target(target)
        kind = resolved.get("kind")
        record = resolved.get("record", {})
        obj = resolved.get("object")

        if kind == "light":
            state = record.get("state", {}) if isinstance(record, dict) else {}
            was_on = bool(state.get("on")) if isinstance(state, dict) else False
            next_on = not was_on
            await wrapper.light_set_on(cast("LightProto", obj), next_on)
            if isinstance(state, dict) and state.get("reachable") is False:
                click.echo(
                    f"warning: light {target!r} is not reachable; command queued by bridge",
                    err=True,
                )
            return {"target": target, "kind": "light", "on": next_on}

        if kind in ("room", "zone"):
            # Decision 4: consolidate-on. all_on=True → off; otherwise → on.
            state = record.get("state", {}) if isinstance(record, dict) else {}
            all_on = bool(state.get("all_on")) if isinstance(state, dict) else False
            next_on = not all_on
            await wrapper.group_set_on(cast("GroupProto", obj), next_on)
            return {"target": target, "kind": kind, "on": next_on}

        raise click.ClickException(f"target {target!r} is not toggleable (kind={kind!r})")


@click.command(name="on")
@click.argument("target")
@click.pass_context
def on_cmd(ctx: click.Context, target: str) -> None:
    """Turn TARGET on (FR-22)."""
    wrapper = _get_wrapper(ctx)
    asyncio.run(_apply_power(wrapper, target, True))


@click.command(name="off")
@click.argument("target")
@click.pass_context
def off_cmd(ctx: click.Context, target: str) -> None:
    """Turn TARGET off (FR-23)."""
    wrapper = _get_wrapper(ctx)
    asyncio.run(_apply_power(wrapper, target, False))


@click.command(name="toggle")
@click.argument("target")
@click.pass_context
def toggle_cmd(ctx: click.Context, target: str) -> None:
    """Toggle TARGET on/off (FR-24, Decision 4 consolidate-on for groups)."""
    wrapper = _get_wrapper(ctx)
    try:
        asyncio.run(_apply_toggle(wrapper, target))
    except click.ClickException:
        raise
    except Exception as exc:
        click.echo(f"toggle failed: {exc}", err=True)
        sys.exit(1)
