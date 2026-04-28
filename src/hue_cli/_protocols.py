"""Protocol stubs for cross-module surfaces during parallel development.

Engineer A owns the concrete implementations of ``HueWrapper`` (in
``wrapper.py``) and the error / config / credentials modules. Engineer B's
verbs need to reference those types for mypy strictness without a hard
import-time dependency on Engineer A's files (which may be missing on disk
during parallel branch work).

The Protocols here are STRUCTURAL — they describe the methods Engineer B's
verbs invoke on Engineer A's wrapper. The concrete ``HueWrapper`` does NOT
need to inherit from ``HueWrapperProto``; duck typing is sufficient. At
integration time, if Engineer A's surface diverges, the contract notes in
the merge commit document the reconciliation needed.

Tests inject a fake wrapper via Click's ``ctx.obj`` so the verbs can be
exercised without aiohue / aiohttp at all.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Protocol


class LightProto(Protocol):
    """Subset of aiohue's ``Light`` model the verbs touch.

    The ``set_state`` signature covers every wire field the Phase 2 ``set``
    verb forwards (brightness, color temp, color in xy/hue/sat, effect,
    alert, transition). Protocols are structural — the concrete
    ``aiohue.Light.set_state`` accepts these kwargs already; we just declare
    them here so mypy can type-check the verb's call sites.
    """

    id: str
    name: str

    async def set_state(
        self,
        *,
        on: bool | None = ...,
        bri: int | None = ...,
        ct: int | None = ...,
        xy: tuple[float, float] | None = ...,
        hue: int | None = ...,
        sat: int | None = ...,
        effect: str | None = ...,
        alert: str | None = ...,
        transitiontime: int | None = ...,
    ) -> None: ...


class GroupProto(Protocol):
    """Subset of aiohue's ``Group`` model the verbs touch.

    Same kwargs as :class:`LightProto.set_state` plus ``scene`` (only valid
    on group dispatch — Hue v1 scene-recall is a group-level operation).
    """

    id: str
    name: str

    async def set_action(
        self,
        *,
        on: bool | None = ...,
        bri: int | None = ...,
        ct: int | None = ...,
        xy: tuple[float, float] | None = ...,
        hue: int | None = ...,
        sat: int | None = ...,
        effect: str | None = ...,
        alert: str | None = ...,
        scene: str | None = ...,
        transitiontime: int | None = ...,
    ) -> None: ...


class HueWrapperProto(Protocol):
    """Engineer A's wrapper surface used by Engineer B's verbs.

    The wrapper presents a unified, throttler-shared view over aiohue's
    bridge object plus the direct-aiohttp fallback paths (§4.5) for
    resources aiohue does not model (schedules).

    Verbs receive a wrapper instance via Click's ``ctx.obj`` and call the
    methods declared here. The actual class lives in ``hue_cli.wrapper``
    (Engineer A); tests inject fake objects implementing this Protocol.
    """

    async def list_lights_records(self) -> list[dict[str, Any]]:
        """Return Light records as plain dicts shaped per §10.2."""

    async def list_groups_records(self) -> list[dict[str, Any]]:
        """Return Group records (Rooms + Zones + 'all lights' g0) per §10.3."""

    async def list_scenes_records(self) -> list[dict[str, Any]]:
        """Return Scene records per §10.4."""

    async def list_sensors_records(self) -> list[dict[str, Any]]:
        """Return Sensor records per §10.5."""

    async def list_schedules_records(self) -> list[dict[str, Any]]:
        """Return Schedule records per §10.7 (uses §4.5 direct-aiohttp path)."""

    async def get_bridge_record(self) -> dict[str, Any]:
        """Return the §10.1 Bridge record including network/zigbee/whitelist."""

    async def resolve_target(self, target: str) -> dict[str, Any]:
        """Resolve a target string to its kind + record per FR-19 precedence.

        Returns a dict shaped ``{"kind": "light"|"room"|"zone"|"scene"|"sensor"|"bridge",
        "record": <§10 record dict>, "object": <aiohue model | None>}``.
        ``object`` is the live aiohue Light/Group/Sensor for power-control
        dispatch (None for bridge / scene targets where the verb works off
        the record alone).
        """

    async def light_set_on(self, light: LightProto, on: bool) -> None:
        """Helper for ``on``/``off``/``toggle`` verb dispatch on a light."""

    async def group_set_on(self, group: GroupProto, on: bool) -> None:
        """Helper for ``on``/``off``/``toggle`` verb dispatch on a group."""

    async def light_set_state(self, light: LightProto, **state: Any) -> None:
        """Helper for ``set`` verb dispatch on a light (FR-22 / FR-27..38).

        Thin pass-through to ``light.set_state(**state)``. Lives on the
        wrapper rather than as a direct call so test fakes can record the
        kwargs without monkey-patching the underlying object, and so the
        wrapper retains the option to attach throttling / retries here later.
        """

    async def group_set_action(self, group: GroupProto, **action: Any) -> None:
        """Helper for ``set`` verb dispatch on a group / 'all' (FR-22).

        Same shape as :meth:`light_set_state` but for ``Group.set_action``.
        """

    async def get_all_lights_group(self) -> GroupProto:
        """Return the special Group 0 (all lights) for ``all`` target."""

    async def apply_scene(
        self,
        scene_id: str,
        group_id: str | None,
        *,
        transitiontime: int | None,
    ) -> None:
        """Apply ``scene_id`` (FR-39).

        For modern ``GroupScene`` entries pass the scene's ``group_id``; the
        wrapper routes to ``bridge.groups[group_id].set_action(scene=...)``.
        For legacy ``LightScene`` entries with no group, pass ``group_id=None``
        and the wrapper falls back to the all-lights group recall.
        ``transitiontime`` is in deciseconds (caller does the ms->ds round).
        """

    async def __aenter__(self) -> HueWrapperProto:
        """Open the underlying bridge connection for the ``async with`` block.

        Compose verbs (``on``/``off``/``toggle``) wrap a resolve+dispatch pair
        in ``async with`` so the aiohttp ``ClientSession`` stays alive across
        both calls — the Light/Group object returned by ``resolve_target``
        carries a bound ``_request`` that would otherwise outlive its session.
        """

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the underlying bridge connection at block exit."""
