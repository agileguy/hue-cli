"""Tests for the ``group list`` verb (FR-51) and group-type filtering.

The verb is an alias that merges ``list rooms`` + ``list zones`` from the
bridge's ``groups`` collection. Other group types reported by the bridge
(``LightGroup``, ``Luminaire``, ``LightSource``, ``Entertainment``) are
intentionally excluded — they are bridge-internal bookkeeping, not
operator-facing operation targets per SRD §5.10.

Tests use the :class:`FakeWrapper` from :mod:`tests.test_part_b` so the verb
is exercised without aiohue. Click's ``runner.invoke(..., obj={...})``
injects the fake wrapper and the active output format directly.
"""

from __future__ import annotations

import json
from typing import Any

from click.testing import CliRunner

from hue_cli.cli import main
from hue_cli.output import OutputFormat
from tests.test_part_b import FakeWrapper, _ctx


def _make_groups_with_extras() -> list[dict[str, Any]]:
    """Two Rooms + one Zone + one LightGroup + one Luminaire.

    The fixture exercises the FR-51 filter: only Rooms and Zones survive;
    the LightGroup and Luminaire are dropped before emission. Names are
    distinct enough that a downstream ``--filter`` test can target each
    one explicitly without ambiguity.
    """

    return [
        {
            "id": "1",
            "type": "Room",
            "name": "Kitchen",
            "class": "Kitchen",
            "light_ids": ["1", "2"],
            "sensor_ids": [],
            "state": {"any_on": True, "all_on": True},
        },
        {
            "id": "2",
            "type": "Room",
            "name": "Bedroom",
            "class": "Bedroom",
            "light_ids": ["3"],
            "sensor_ids": [],
            "state": {"any_on": False, "all_on": False},
        },
        {
            "id": "3",
            "type": "Zone",
            "name": "Upstairs",
            "class": None,
            "light_ids": ["3", "4"],
            "sensor_ids": [],
            "state": {"any_on": True, "all_on": False},
        },
        {
            "id": "100",
            "type": "LightGroup",
            "name": "Bridge LightGroup 100",
            "class": None,
            "light_ids": ["1"],
            "sensor_ids": [],
            "state": {"any_on": False, "all_on": False},
        },
        {
            "id": "200",
            "type": "Luminaire",
            "name": "Hue Iris luminaire",
            "class": None,
            "light_ids": ["5"],
            "sensor_ids": [],
            "state": {"any_on": False, "all_on": False},
        },
    ]


class TestGroupList:
    """FR-51: ``group list`` merges Rooms + Zones; excludes other group types."""

    def test_merges_rooms_and_zones_excludes_other_types(self) -> None:
        """Default (no filter) → 2 Rooms + 1 Zone; LightGroup/Luminaire dropped."""
        wrapper = FakeWrapper(groups=_make_groups_with_extras())
        runner = CliRunner()
        result = runner.invoke(main, ["--json", "group", "list"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 3
        kinds = sorted(item["type"] for item in data)
        assert kinds == ["Room", "Room", "Zone"]
        # Names verify it's the right three (Kitchen, Bedroom, Upstairs).
        assert {item["name"] for item in data} == {"Kitchen", "Bedroom", "Upstairs"}

    def test_filter_type_room_returns_rooms_only(self) -> None:
        """``--filter type=Room`` constrains to rooms (FR-18 + FR-51)."""
        wrapper = FakeWrapper(groups=_make_groups_with_extras())
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--json", "group", "list", "--filter", "type=Room"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 2
        assert all(item["type"] == "Room" for item in data)

    def test_filter_type_zone_returns_zones_only(self) -> None:
        """``--filter type=Zone`` constrains to zones (FR-18 + FR-51)."""
        wrapper = FakeWrapper(groups=_make_groups_with_extras())
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--json", "group", "list", "--filter", "type=Zone"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["type"] == "Zone"
        assert data[0]["name"] == "Upstairs"

    def test_lightgroup_and_luminaire_never_surface_even_via_filter(self) -> None:
        """Filtering on ``type=LightGroup`` returns nothing — they are pre-filtered.

        FR-51 says ``group list`` is rooms + zones merged. A user cannot
        coax a LightGroup or Luminaire out via ``--filter type=LightGroup``;
        the type filter applies AFTER the Room/Zone constraint.
        """

        wrapper = FakeWrapper(groups=_make_groups_with_extras())
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--json", "group", "list", "--filter", "type=LightGroup"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data == []

    def test_json_shape_is_stable(self) -> None:
        """Each emitted record has the §10.3 Group fields (id/type/name/class/light_ids/...)."""
        wrapper = FakeWrapper(groups=_make_groups_with_extras())
        runner = CliRunner()
        result = runner.invoke(main, ["--json", "group", "list"], obj=_ctx(wrapper))
        data = json.loads(result.output)
        assert data, "expected at least one record"
        sample = data[0]
        # §10.3 contract: every emitted record has these keys.
        for key in ("id", "type", "name", "class", "light_ids", "sensor_ids", "state"):
            assert key in sample, f"missing {key!r} from {sample!r}"

    def test_jsonl_emits_one_line_per_group(self) -> None:
        """JSONL mode → 3 lines (2 Rooms + 1 Zone), one compact JSON object each."""
        wrapper = FakeWrapper(groups=_make_groups_with_extras())
        runner = CliRunner()
        result = runner.invoke(
            main, ["--jsonl", "group", "list"], obj=_ctx(wrapper, OutputFormat.JSONL)
        )
        assert result.exit_code == 0, result.output
        lines = [line for line in result.output.split("\n") if line.strip()]
        assert len(lines) == 3
        for line in lines:
            obj = json.loads(line)
            assert obj["type"] in ("Room", "Zone")

    def test_quiet_suppresses_stdout(self) -> None:
        """--quiet → no stdout output, exit 0."""
        wrapper = FakeWrapper(groups=_make_groups_with_extras())
        runner = CliRunner()
        result = runner.invoke(
            main, ["--quiet", "group", "list"], obj=_ctx(wrapper, OutputFormat.QUIET)
        )
        assert result.exit_code == 0, result.output
        assert result.output == ""

    def test_empty_bridge_returns_empty_list(self) -> None:
        """No groups on bridge → empty JSON array, exit 0."""
        wrapper = FakeWrapper(groups=[])
        runner = CliRunner()
        result = runner.invoke(main, ["--json", "group", "list"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == []

    def test_filter_class_kitchen_matches_kitchen_room(self) -> None:
        """``--filter class=Kitchen`` matches the Kitchen room's class field."""
        wrapper = FakeWrapper(groups=_make_groups_with_extras())
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--json", "group", "list", "--filter", "class=Kitchen"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["name"] == "Kitchen"
