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
    """Subset of aiohue's ``Light`` model the verbs touch."""

    id: str
    name: str

    async def set_state(
        self,
        *,
        on: bool | None = ...,
        bri: int | None = ...,
        transitiontime: int | None = ...,
    ) -> None: ...


class GroupProto(Protocol):
    """Subset of aiohue's ``Group`` model the verbs touch."""

    id: str
    name: str

    async def set_action(
        self,
        *,
        on: bool | None = ...,
        bri: int | None = ...,
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

    async def get_all_lights_group(self) -> GroupProto:
        """Return the special Group 0 (all lights) for ``all`` target."""

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
