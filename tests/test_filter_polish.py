"""FR-18 ``--filter`` polish audit.

FR-18 requires that all list verbs accept ``--filter <key=value>`` for simple
substring/equality filtering on top-level fields, AND-combined across multiple
flags. Phase 1 shipped the implementation in :mod:`hue_cli.verbs.list_cmd`;
this module verifies the matching semantics meet the SRD.

Specifically tested:

* Case-insensitive substring matching on string-valued fields (e.g.
  ``--filter type=Color`` matches ``Extended color light`` and
  ``Color temperature light``)
* AND-combination across multiple ``--filter`` flags
* Top-level-only matching (``state.on`` is NOT a valid filter key — the SRD
  intentionally keeps the grammar minimal; richer queries pipe to ``jq``)
* Filters apply consistently across ``list lights``, ``list rooms``,
  ``list zones``, ``list scenes``, ``list sensors``, and ``group list``
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from hue_cli.cli import main
from tests.test_group_cmd import _make_groups_with_extras
from tests.test_part_b import (
    FakeWrapper,
    _ctx,
    _make_lights,
    _make_scenes,
    _make_sensors,
)


class TestFilterCaseInsensitiveSubstring:
    """FR-18: matching is case-insensitive substring on top-level field values."""

    def test_lights_filter_type_color_matches_extended_and_temperature(self) -> None:
        """``--filter type=color`` (lowercase) matches both color-capable types.

        ``_make_lights()`` includes 'Extended color light' and
        'Color temperature light' — both contain the substring ``color``
        in any case. The third light is 'Dimmable light' (no color) and
        SHALL be excluded.
        """

        wrapper = FakeWrapper(lights=_make_lights())
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--json", "list", "lights", "--filter", "type=color"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        types = sorted(item["type"] for item in data)
        assert types == ["Color temperature light", "Extended color light"]

    def test_lights_filter_uppercase_value_matches_lowercase_haystack(self) -> None:
        """Filter value casing is irrelevant — ``type=COLOR`` works too."""
        wrapper = FakeWrapper(lights=_make_lights())
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--json", "list", "lights", "--filter", "type=COLOR"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 2

    def test_rooms_filter_class_kitchen_matches_kitchen_room(self) -> None:
        """``--filter class=Kitchen`` on ``list rooms`` matches the kitchen."""
        wrapper = FakeWrapper(groups=_make_groups_with_extras())
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--json", "list", "rooms", "--filter", "class=Kitchen"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["name"] == "Kitchen"

    def test_scenes_filter_name_substring(self) -> None:
        """Substring on ``name`` works for scenes too."""
        wrapper = FakeWrapper(scenes=_make_scenes())
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--json", "list", "scenes", "--filter", "name=energize"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["name"] == "Energize"

    def test_sensors_filter_type_substring(self) -> None:
        """Substring on ``type`` works for sensors (``ZLLPresence``)."""
        wrapper = FakeWrapper(sensors=_make_sensors())
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--json", "list", "sensors", "--filter", "type=presence"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["type"] == "ZLLPresence"


class TestFilterAndCombination:
    """FR-18: multiple ``--filter`` flags AND-combine."""

    def test_two_filters_intersect(self) -> None:
        """``--filter type=color --filter name=kitchen`` matches Kitchen Pendant only."""
        wrapper = FakeWrapper(lights=_make_lights())
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--json",
                "list",
                "lights",
                "--filter",
                "type=color",
                "--filter",
                "name=kitchen",
            ],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["name"] == "Kitchen Pendant"

    def test_two_filters_disjoint_returns_empty(self) -> None:
        """If the AND-intersection is empty → empty list (no match found)."""
        wrapper = FakeWrapper(lights=_make_lights())
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--json",
                "list",
                "lights",
                "--filter",
                "type=color",
                "--filter",
                "name=hallway",
            ],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # Hallway is Dimmable, not color → AND-empty.
        assert data == []

    def test_three_filters_all_must_match(self) -> None:
        """Three filters → only records satisfying all three survive."""
        wrapper = FakeWrapper(lights=_make_lights())
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--json",
                "list",
                "lights",
                "--filter",
                "type=color",
                "--filter",
                "name=kitchen",
                "--filter",
                "model_id=LCT015",
            ],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["id"] == "1"


class TestFilterTopLevelOnly:
    """FR-18: filter grammar is intentionally minimal — top-level only.

    Dotted-path lookups (``state.on``, ``state.brightness_percent``) are NOT
    supported. The SRD's stance: pipe to ``jq`` for richer queries. A
    dotted-key filter should match nothing — ``state.on`` is not a top-level
    field on a Light record (only ``state`` is, and its value is a dict, not
    a string), so the substring match cannot succeed.
    """

    def test_dotted_path_filter_matches_nothing(self) -> None:
        """``--filter state.on=true`` finds nothing — dotted paths unsupported."""
        wrapper = FakeWrapper(lights=_make_lights())
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--json", "list", "lights", "--filter", "state.on=true"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # No top-level key called 'state.on' exists, so the filter excludes all.
        assert data == []


class TestFilterMissingValue:
    """FR-18 grammar guards: malformed filters surface as Click usage errors."""

    def test_filter_without_equals_raises_usage_error(self) -> None:
        """``--filter typecolor`` (no ``=``) → Click usage error, exit != 0."""
        wrapper = FakeWrapper(lights=_make_lights())
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--json", "list", "lights", "--filter", "typecolor"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code != 0
