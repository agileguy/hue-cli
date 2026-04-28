"""Tests for ``hue_cli.verbs.set_cmd`` — the ``set`` verb (FR-27..38).

A hand-rolled fake wrapper exercises the verb without aiohue / aiohttp; the
fake's ``light_set_state`` / ``group_set_action`` record the kwargs the verb
forwards so tests can assert on the exact wire payload.

Capability gating (FR-36) tests configure the fake light's
``controlcapabilities`` dict and check that the verb exits 5 on a mismatch.
"""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from hue_cli.cli import main
from hue_cli.output import OutputFormat

# --- Fake fixtures ----------------------------------------------------------


class FakeLight:
    """Fake aiohue Light. ``controlcapabilities`` drives FR-36 gating."""

    def __init__(
        self,
        light_id: str,
        name: str,
        *,
        controlcapabilities: dict[str, Any] | None = None,
    ) -> None:
        self.id = light_id
        self.name = name
        # Default: tunable-color light with full sRGB-ish gamut + ct range.
        self.controlcapabilities: dict[str, Any] = (
            controlcapabilities
            if controlcapabilities is not None
            else {
                "ct": {"min": 153, "max": 500},
                "colorgamut": [[0.6750, 0.3220], [0.4090, 0.5180], [0.1670, 0.0400]],
                "colorgamuttype": "B",
            }
        )
        self.set_state_calls: list[dict[str, Any]] = []

    async def set_state(self, **kwargs: Any) -> None:
        self.set_state_calls.append(kwargs)


class FakeGroup:
    def __init__(self, group_id: str, name: str) -> None:
        self.id = group_id
        self.name = name
        self.set_action_calls: list[dict[str, Any]] = []

    async def set_action(self, **kwargs: Any) -> None:
        self.set_action_calls.append(kwargs)


class FakeWrapper:
    def __init__(
        self,
        *,
        target_lookup: dict[str, dict[str, Any]] | None = None,
        all_lights_group: FakeGroup | None = None,
    ) -> None:
        self._target_lookup = target_lookup or {}
        self._all_lights_group = all_lights_group or FakeGroup("0", "all")

    async def resolve_target(self, target: str) -> dict[str, Any]:
        if target in self._target_lookup:
            return dict(self._target_lookup[target])
        return {"kind": "unknown", "record": {}, "object": None}

    async def light_set_state(self, light: FakeLight, **state: Any) -> None:
        await light.set_state(**state)

    async def group_set_action(self, group: FakeGroup, **action: Any) -> None:
        await group.set_action(**action)

    async def get_all_lights_group(self) -> FakeGroup:
        return self._all_lights_group

    async def __aenter__(self) -> FakeWrapper:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


# --- Helpers ----------------------------------------------------------------


def _ctx(wrapper: FakeWrapper) -> dict[str, Any]:
    return {"wrapper": wrapper, "format": OutputFormat.JSON}


def _light_record(light: FakeLight) -> dict[str, Any]:
    return {"kind": "light", "record": {"id": light.id, "name": light.name}, "object": light}


def _group_record(group: FakeGroup, kind: str = "room") -> dict[str, Any]:
    return {"kind": kind, "record": {"id": group.id, "name": group.name}, "object": group}


# --- Brightness (FR-27) -----------------------------------------------------


class TestBrightness:
    def test_brightness_50_emits_bri_127ish(self) -> None:
        light = FakeLight("1", "Plug")
        wrapper = FakeWrapper(target_lookup={"Plug": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Plug", "--brightness", "50"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert len(light.set_state_calls) == 1
        call = light.set_state_calls[0]
        # round(50/100 * 253) + 1 = 128 — within ±1.
        assert "bri" in call
        assert 126 <= call["bri"] <= 129
        assert "on" not in call

    def test_brightness_zero_maps_to_on_false(self) -> None:
        light = FakeLight("1", "Plug")
        wrapper = FakeWrapper(target_lookup={"Plug": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Plug", "--brightness", "0"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert light.set_state_calls == [{"on": False}]

    def test_brightness_100_emits_bri_254(self) -> None:
        light = FakeLight("1", "Plug")
        wrapper = FakeWrapper(target_lookup={"Plug": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Plug", "--brightness", "100"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert light.set_state_calls == [{"bri": 254}]


# --- Color temperature (FR-28, FR-29) ---------------------------------------


class TestColorTemperature:
    def test_kelvin_2700_emits_clamped_ct(self) -> None:
        # Default fake light advertises ct min=153 max=500. 1_000_000/2700 ≈ 370.
        light = FakeLight("1", "Lamp")
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Lamp", "--kelvin", "2700"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert len(light.set_state_calls) == 1
        ct = light.set_state_calls[0]["ct"]
        assert 153 <= ct <= 500
        assert ct == 370

    def test_kelvin_above_range_clamps_to_min_mireds(self) -> None:
        # 10000K → 100 mireds, below the 153 floor → clamps up.
        light = FakeLight("1", "Lamp", controlcapabilities={"ct": {"min": 153, "max": 500}})
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Lamp", "--kelvin", "10000"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert light.set_state_calls[0]["ct"] == 153

    def test_mireds_passes_through(self) -> None:
        light = FakeLight("1", "Lamp")
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Lamp", "--mireds", "300"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert light.set_state_calls == [{"ct": 300}]


# --- Color (FR-30..33) ------------------------------------------------------


class TestColor:
    def test_named_red_emits_xy(self) -> None:
        light = FakeLight("1", "Lamp")
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Lamp", "--color", "red"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert len(light.set_state_calls) == 1
        xy = light.set_state_calls[0]["xy"]
        # Red is (0.6750, 0.3220) in the named table.
        assert abs(xy[0] - 0.6750) < 0.01
        assert abs(xy[1] - 0.3220) < 0.01

    def test_unknown_color_exits_64(self) -> None:
        light = FakeLight("1", "Lamp")
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(
            main, ["set", "Lamp", "--color", "neverheardofit"], obj=_ctx(wrapper)
        )
        assert result.exit_code == 64

    def test_hex_green_emits_green_xy(self) -> None:
        light = FakeLight("1", "Lamp")
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Lamp", "--hex", "#00ff00"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        xy = light.set_state_calls[0]["xy"]
        # Green is around (0.30, 0.60); with gamut B clamp it'll be near
        # the gamut's green vertex (0.4090, 0.5180).
        assert 0.25 <= xy[0] <= 0.45
        assert 0.45 <= xy[1] <= 0.65

    def test_hsv_blue_emits_blue_xy_and_bri_from_v(self) -> None:
        light = FakeLight("1", "Lamp")
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Lamp", "--hsv", "240,100,100"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        call = light.set_state_calls[0]
        xy = call["xy"]
        # Blue primary; with gamut B clamp it lands near (0.167, 0.04) corner.
        assert xy[0] < 0.30
        assert xy[1] < 0.20
        # V=100 → bri=254.
        assert call["bri"] == 254

    def test_xy_passes_through(self) -> None:
        light = FakeLight("1", "Lamp")
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Lamp", "--xy", "0.5,0.5"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert light.set_state_calls == [{"xy": (0.5, 0.5)}]


# --- Mutual exclusion (FR-35) -----------------------------------------------


class TestMutex:
    def test_kelvin_and_color_conflict_exits_64(self) -> None:
        light = FakeLight("1", "Lamp")
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["set", "Lamp", "--kelvin", "2700", "--color", "red"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 64
        # Bridge wasn't touched.
        assert light.set_state_calls == []

    def test_xy_and_hex_within_color_group_exits_64(self) -> None:
        light = FakeLight("1", "Lamp")
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["set", "Lamp", "--xy", "0.5,0.5", "--hex", "#ff0000"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 64
        assert light.set_state_calls == []

    def test_kelvin_and_mireds_conflict_exits_64(self) -> None:
        light = FakeLight("1", "Lamp")
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["set", "Lamp", "--kelvin", "2700", "--mireds", "300"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 64

    def test_brightness_with_color_is_allowed(self) -> None:
        # FR-35 explicitly allows --brightness 30 --color red.
        light = FakeLight("1", "Lamp")
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["set", "Lamp", "--brightness", "30", "--color", "red"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 0, result.output
        assert len(light.set_state_calls) == 1
        call = light.set_state_calls[0]
        assert "bri" in call
        assert "xy" in call


# --- Effects + alert + transition (FR-34, FR-37, FR-38) ---------------------


class TestEffectsAndAlert:
    def test_effect_colorloop_implies_on_true(self) -> None:
        light = FakeLight("1", "Lamp")
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Lamp", "--effect", "colorloop"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        call = light.set_state_calls[0]
        assert call["effect"] == "colorloop"
        assert call["on"] is True

    def test_effect_none_does_not_force_on(self) -> None:
        light = FakeLight("1", "Lamp")
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Lamp", "--effect", "none"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        call = light.set_state_calls[0]
        assert call["effect"] == "none"
        assert "on" not in call

    def test_alert_lselect(self) -> None:
        light = FakeLight("1", "Lamp")
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Lamp", "--alert", "lselect"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert light.set_state_calls[0]["alert"] == "lselect"

    def test_transition_ms_to_deciseconds(self) -> None:
        light = FakeLight("1", "Lamp")
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Lamp", "--transition", "1000"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        # 1000 ms / 100 = 10 deciseconds.
        assert light.set_state_calls[0]["transitiontime"] == 10


# --- Capability gating (FR-36) ----------------------------------------------


class TestCapabilities:
    def test_color_on_tunable_white_only_exits_5(self) -> None:
        # Tunable-white-only: ct present, no colorgamut/colorgamuttype.
        light = FakeLight(
            "1",
            "Lamp",
            controlcapabilities={"ct": {"min": 153, "max": 500}},
        )
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Lamp", "--color", "red"], obj=_ctx(wrapper))
        assert result.exit_code == 5
        assert light.set_state_calls == []

    def test_kelvin_on_fixed_color_only_exits_5(self) -> None:
        # Color-only: gamut present, no ct dict.
        light = FakeLight(
            "1",
            "Lamp",
            controlcapabilities={
                "colorgamut": [[0.6750, 0.3220], [0.4090, 0.5180], [0.1670, 0.0400]],
                "colorgamuttype": "B",
            },
        )
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Lamp", "--kelvin", "2700"], obj=_ctx(wrapper))
        assert result.exit_code == 5
        assert light.set_state_calls == []

    def test_brightness_on_no_color_no_ct_light_works(self) -> None:
        # Plain on/off plug. Brightness should still be valid (FR-36 only
        # gates color/CT, not brightness).
        light = FakeLight("1", "Plug", controlcapabilities={})
        wrapper = FakeWrapper(target_lookup={"Plug": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Plug", "--brightness", "50"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output


# --- Group + 'all' dispatch (FR-22) -----------------------------------------


class TestGroupDispatch:
    def test_room_target_routes_to_set_action(self) -> None:
        kitchen = FakeGroup("1", "Kitchen")
        wrapper = FakeWrapper(target_lookup={"@Kitchen": _group_record(kitchen, "room")})
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["set", "@Kitchen", "--brightness", "30", "--color", "red"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 0, result.output
        assert len(kitchen.set_action_calls) == 1
        call = kitchen.set_action_calls[0]
        assert "bri" in call
        assert "xy" in call

    def test_all_target_uses_all_lights_group(self) -> None:
        all_group = FakeGroup("0", "all")
        wrapper = FakeWrapper(all_lights_group=all_group)
        runner = CliRunner()
        result = runner.invoke(main, ["set", "all", "--brightness", "100"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert all_group.set_action_calls == [{"bri": 254}]

    def test_group_dispatch_skips_capability_check(self) -> None:
        # Groups have no ``controlcapabilities`` of their own — FR-36 is
        # light-scoped. A color call against a group must succeed regardless.
        kitchen = FakeGroup("1", "Kitchen")
        wrapper = FakeWrapper(target_lookup={"@Kitchen": _group_record(kitchen, "room")})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "@Kitchen", "--color", "blue"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output


# --- Bare invocation (no flags) ---------------------------------------------


class TestEmptySet:
    def test_no_flags_exits_64(self) -> None:
        light = FakeLight("1", "Lamp")
        wrapper = FakeWrapper(target_lookup={"Lamp": _light_record(light)})
        runner = CliRunner()
        result = runner.invoke(main, ["set", "Lamp"], obj=_ctx(wrapper))
        # No state to set is a usage error, not an idempotent no-op — the
        # operator likely typed something incomplete.
        assert result.exit_code == 64
        assert light.set_state_calls == []


# --- CLI registration -------------------------------------------------------


class TestRegistration:
    def test_set_appears_in_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "set" in result.output

    def test_set_help_lists_all_flags(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["set", "--help"])
        assert result.exit_code == 0
        for flag in (
            "--brightness",
            "--kelvin",
            "--mireds",
            "--xy",
            "--hex",
            "--color",
            "--hsv",
            "--transition",
            "--effect",
            "--alert",
        ):
            assert flag in result.output, f"missing {flag} in help"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
