"""Wrapper-level target resolution tests (FR-49 / FR-50).

The :class:`hue_cli.wrapper.HueWrapper` resolves ``@<name>``,
``@room:<name>``, and ``@zone:<name>`` against the live bridge's
``groups`` collection. Phase 1 shipped this in
:meth:`HueWrapper._resolve_group_target` and Phase 2 used it via the
``onoff_cmd`` and ``set_cmd`` paths through ``resolve_target``.

This module covers the disambiguation behavior the SRD calls out
explicitly:

* FR-49: ``@<name>`` matches Rooms and Zones case-insensitively
* FR-50: when both a Room and a Zone share a name, ``@<name>`` exits
  with a usage error listing both ids; the operator disambiguates with
  ``@room:<name>`` or ``@zone:<name>``

Tests use a tiny bridge fake exposing ``.groups.values()`` — no aiohue
involvement — and call the wrapper's resolver directly.
"""

from __future__ import annotations

from typing import Any

import pytest

from hue_cli.errors import NotFoundError, UsageError
from hue_cli.wrapper import HueWrapper


class _GroupFake:
    """Minimal aiohue-shaped Group: id / name / type, no raw needed for resolution."""

    def __init__(self, group_id: str, name: str, group_type: str) -> None:
        self.id = group_id
        self.name = name
        self.type = group_type
        self.raw = {"name": name, "type": group_type}

    @property
    def state(self) -> dict[str, Any]:
        return {"any_on": False, "all_on": False}

    @property
    def lights(self) -> list[Any]:
        return []

    @property
    def sensors(self) -> list[Any]:
        return []


class _BridgeGroupsFake:
    """Stand-in for aiohue's ``groups`` controller — supports ``.values()``."""

    def __init__(self, groups: list[_GroupFake]) -> None:
        self._groups = {g.id: g for g in groups}

    def values(self) -> list[_GroupFake]:
        return list(self._groups.values())

    def __iter__(self) -> Any:
        return iter(self._groups)

    def __getitem__(self, key: str) -> _GroupFake:
        return self._groups[key]


class _BridgeFake:
    """Minimal bridge stub — only ``.groups`` is touched by ``_resolve_group_target``."""

    def __init__(self, groups: list[_GroupFake]) -> None:
        self.groups = _BridgeGroupsFake(groups)


def _wrapper() -> HueWrapper:
    """Construct a wrapper without opening a connection — only the resolver is exercised."""
    return HueWrapper("192.168.1.10", "fake-app-key")


# ---------------------------------------------------------------------------
# FR-49 — basic ``@<name>`` resolution against Rooms + Zones
# ---------------------------------------------------------------------------


class TestResolveByName:
    """FR-49: ``@<name>`` matches Rooms or Zones case-insensitively."""

    def test_room_only_match(self) -> None:
        bridge = _BridgeFake(
            [
                _GroupFake("1", "Kitchen", "Room"),
                _GroupFake("2", "Bedroom", "Room"),
                _GroupFake("3", "Upstairs", "Zone"),
            ]
        )
        result = _wrapper()._resolve_group_target(bridge, "@kitchen")
        assert result["kind"] == "room"
        assert result["object"].id == "1"

    def test_zone_only_match(self) -> None:
        bridge = _BridgeFake(
            [
                _GroupFake("1", "Kitchen", "Room"),
                _GroupFake("3", "Upstairs", "Zone"),
            ]
        )
        result = _wrapper()._resolve_group_target(bridge, "@upstairs")
        assert result["kind"] == "zone"
        assert result["object"].id == "3"

    def test_case_insensitive_match(self) -> None:
        """FR-49 explicitly says case-insensitive name matching."""
        bridge = _BridgeFake([_GroupFake("1", "Kitchen", "Room")])
        result = _wrapper()._resolve_group_target(bridge, "@KITCHEN")
        assert result["object"].id == "1"

    def test_unknown_name_raises_not_found(self) -> None:
        bridge = _BridgeFake([_GroupFake("1", "Kitchen", "Room")])
        with pytest.raises(NotFoundError):
            _wrapper()._resolve_group_target(bridge, "@nonsense")

    def test_lightgroup_and_luminaire_excluded_from_resolution(self) -> None:
        """FR-49 limits resolution to ``type in ("Room", "Zone")``.

        A LightGroup or Luminaire on the bridge with a colliding name
        SHALL NOT be returned by ``@<name>`` resolution. Phase 1 already
        enforces this; the test pins the contract.
        """

        bridge = _BridgeFake(
            [
                _GroupFake("100", "Office", "LightGroup"),
                _GroupFake("200", "Office", "Luminaire"),
            ]
        )
        with pytest.raises(NotFoundError):
            _wrapper()._resolve_group_target(bridge, "@office")


# ---------------------------------------------------------------------------
# FR-50 — Room + Zone with the same name → ambiguous; ``@room:`` / ``@zone:``
# disambiguates
# ---------------------------------------------------------------------------


class TestResolveAmbiguous:
    """FR-50: when a Room and a Zone share a name, ``@<name>`` is ambiguous.

    The operator disambiguates with ``@room:<name>`` or ``@zone:<name>``.
    The ambiguous case SHALL surface as a :class:`UsageError`, which the
    CLI maps to exit 64 per FR-50.
    """

    def test_ambiguous_name_raises_usage_error(self) -> None:
        bridge = _BridgeFake(
            [
                _GroupFake("1", "Office", "Room"),
                _GroupFake("3", "Office", "Zone"),
            ]
        )
        with pytest.raises(UsageError) as info:
            _wrapper()._resolve_group_target(bridge, "@office")
        # The error message should list both ids and their types so the
        # operator can copy-paste the disambiguator they want.
        msg = str(info.value)
        assert "1" in msg and "3" in msg
        assert "Room" in msg and "Zone" in msg
        assert "@room:" in msg or "@zone:" in msg

    def test_room_prefix_disambiguates_to_room(self) -> None:
        bridge = _BridgeFake(
            [
                _GroupFake("1", "Office", "Room"),
                _GroupFake("3", "Office", "Zone"),
            ]
        )
        result = _wrapper()._resolve_group_target(bridge, "@room:Office")
        assert result["kind"] == "room"
        assert result["object"].id == "1"

    def test_zone_prefix_disambiguates_to_zone(self) -> None:
        bridge = _BridgeFake(
            [
                _GroupFake("1", "Office", "Room"),
                _GroupFake("3", "Office", "Zone"),
            ]
        )
        result = _wrapper()._resolve_group_target(bridge, "@zone:Office")
        assert result["kind"] == "zone"
        assert result["object"].id == "3"

    def test_room_prefix_case_insensitive_value(self) -> None:
        """``@room:OFFICE`` (uppercase value) still matches the Room."""
        bridge = _BridgeFake(
            [
                _GroupFake("1", "Office", "Room"),
                _GroupFake("3", "Office", "Zone"),
            ]
        )
        result = _wrapper()._resolve_group_target(bridge, "@room:OFFICE")
        assert result["kind"] == "room"

    def test_room_prefix_when_only_zone_exists_raises_not_found(self) -> None:
        """``@room:Office`` when no Room called Office exists → NotFoundError.

        The constraint applies BEFORE the name match, so a Zone called
        Office is invisible to a ``@room:`` query.
        """

        bridge = _BridgeFake([_GroupFake("3", "Office", "Zone")])
        with pytest.raises(NotFoundError):
            _wrapper()._resolve_group_target(bridge, "@room:Office")

    def test_zone_prefix_when_only_room_exists_raises_not_found(self) -> None:
        bridge = _BridgeFake([_GroupFake("1", "Office", "Room")])
        with pytest.raises(NotFoundError):
            _wrapper()._resolve_group_target(bridge, "@zone:Office")


class TestResolveOnlyRoomsAndZones:
    """FR-49 explicitly: only ``type in ("Room", "Zone")`` participate."""

    def test_at_name_with_room_and_lightgroup_collision_picks_room(self) -> None:
        """A LightGroup colliding with a Room name is invisible — Room wins, no ambiguity."""
        bridge = _BridgeFake(
            [
                _GroupFake("1", "Office", "Room"),
                _GroupFake("100", "Office", "LightGroup"),
            ]
        )
        result = _wrapper()._resolve_group_target(bridge, "@office")
        assert result["kind"] == "room"
        assert result["object"].id == "1"

    def test_at_name_with_zone_and_luminaire_collision_picks_zone(self) -> None:
        bridge = _BridgeFake(
            [
                _GroupFake("3", "Office", "Zone"),
                _GroupFake("200", "Office", "Luminaire"),
            ]
        )
        result = _wrapper()._resolve_group_target(bridge, "@office")
        assert result["kind"] == "zone"
        assert result["object"].id == "3"
