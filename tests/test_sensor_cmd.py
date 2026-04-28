"""Tests for `hue-cli sensor info` / `hue-cli sensor list` (FR-46, FR-47).

Two test surfaces:

1. Pure ``shape_sensor_info`` unit tests — type-specific projection of a §10.5
   sensor record into the FR-47 output shape. No Click, no wrapper, no aiohue.
2. Verb integration tests — invoke ``main`` with a fake wrapper providing the
   sensor records and assert the verb emits the correctly-shaped JSON.
"""

from __future__ import annotations

import json
from typing import Any

from click.testing import CliRunner

from hue_cli.cli import main
from hue_cli.output import OutputFormat
from hue_cli.wrapper import shape_sensor_info

# --- Pure shape_sensor_info tests --------------------------------------------


class TestShapeSensorInfo:
    def test_motion_sensor_emits_presence_lastupdated_battery_reachable(self) -> None:
        record = {
            "id": "5",
            "name": "Motion Hallway",
            "type": "ZLLPresence",
            "model_id": "SML001",
            "unique_id": "00:11:22:33:44:55-02-0406",
            "state": {"presence": True, "lastupdated": "2026-04-27T14:00:00"},
            "config": {"on": True, "battery": 87, "reachable": True},
        }
        shaped = shape_sensor_info(record)
        assert shaped["presence"] is True
        assert shaped["lastupdated"] == "2026-04-27T14:00:00"
        assert shaped["battery"] == 87
        assert shaped["reachable"] is True
        assert shaped["type"] == "ZLLPresence"

    def test_zll_switch_emits_buttonevent(self) -> None:
        record = {
            "id": "6",
            "name": "Hallway Dimmer",
            "type": "ZLLSwitch",
            "model_id": "RWL021",
            "state": {"buttonevent": 1002, "lastupdated": "2026-04-27T13:55:00"},
            "config": {"on": True, "battery": 100, "reachable": True},
        }
        shaped = shape_sensor_info(record)
        assert shaped["buttonevent"] == 1002
        assert shaped["lastupdated"] == "2026-04-27T13:55:00"
        # ZLLSwitch is battery-powered: surface battery + reachable.
        assert shaped["battery"] == 100
        assert shaped["reachable"] is True

    def test_zgp_switch_emits_buttonevent_without_battery(self) -> None:
        # ZGPSwitch is energy-harvesting (no battery, no reachable).
        record = {
            "id": "7",
            "name": "Tap",
            "type": "ZGPSwitch",
            "model_id": "ZGPSWITCH",
            "state": {"buttonevent": 34, "lastupdated": "2026-04-27T13:00:00"},
            "config": {"on": True},
        }
        shaped = shape_sensor_info(record)
        assert shaped["buttonevent"] == 34
        assert shaped["lastupdated"] == "2026-04-27T13:00:00"
        assert "battery" not in shaped
        assert "reachable" not in shaped

    def test_temperature_sensor_converts_centi_celsius_to_float(self) -> None:
        # FR-47: bridge reports centi-Celsius (e.g. 2150 = 21.5 C); convert to float.
        record = {
            "id": "8",
            "name": "Hallway Temp",
            "type": "ZLLTemperature",
            "model_id": "SML001",
            "state": {"temperature": 2150, "lastupdated": "2026-04-27T14:00:00"},
            "config": {"on": True, "battery": 88, "reachable": True},
        }
        shaped = shape_sensor_info(record)
        assert shaped["temperature"] == 21.5
        assert isinstance(shaped["temperature"], float)
        assert shaped["lastupdated"] == "2026-04-27T14:00:00"
        assert shaped["battery"] == 88

    def test_temperature_negative_value(self) -> None:
        # Hue temperature sensors do report negative values when outside in winter.
        record = {
            "id": "8a",
            "name": "Outdoor Temp",
            "type": "ZLLTemperature",
            "model_id": "SML002",
            "state": {"temperature": -540, "lastupdated": "2026-01-15T07:00:00"},
            "config": {"on": True, "battery": 80, "reachable": True},
        }
        shaped = shape_sensor_info(record)
        assert shaped["temperature"] == -5.4

    def test_temperature_missing_returns_none(self) -> None:
        record = {
            "id": "8b",
            "name": "Broken Temp",
            "type": "ZLLTemperature",
            "model_id": "SML001",
            "state": {"lastupdated": "none"},
            "config": {"on": True, "reachable": False},
        }
        shaped = shape_sensor_info(record)
        assert shaped["temperature"] is None

    def test_light_level_sensor_emits_lightlevel_dark_daylight(self) -> None:
        record = {
            "id": "9",
            "name": "Hallway Lux",
            "type": "ZLLLightLevel",
            "model_id": "SML001",
            "state": {
                "lightlevel": 6234,
                "dark": False,
                "daylight": True,
                "lastupdated": "2026-04-27T14:00:00",
            },
            "config": {"on": True, "battery": 87, "reachable": True},
        }
        shaped = shape_sensor_info(record)
        assert shaped["lightlevel"] == 6234
        assert shaped["dark"] is False
        assert shaped["daylight"] is True

    def test_daylight_synthetic_emits_daylight_plus_sunrise_sunset(self) -> None:
        record = {
            "id": "1",
            "name": "Daylight",
            "type": "Daylight",
            "model_id": "PHDL00",
            "state": {"daylight": True, "lastupdated": "2026-04-27T14:00:00"},
            "config": {
                "on": True,
                "configured": True,
                "sunriseoffset": 30,
                "sunsetoffset": -30,
                "lat": "43.6532",
                "long": "-79.3832",
            },
        }
        shaped = shape_sensor_info(record)
        assert shaped["daylight"] is True
        assert shaped["sunrise"] == 30
        assert shaped["sunset"] == -30
        assert shaped["latitude"] == "43.6532"
        assert shaped["longitude"] == "-79.3832"
        assert shaped["configured"] is True

    def test_clip_generic_passes_state_and_config_through(self) -> None:
        record = {
            "id": "100",
            "name": "Custom Flag",
            "type": "CLIPGenericFlag",
            "model_id": "GenericFlag",
            "state": {"flag": True, "lastupdated": "2026-04-27T14:00:00"},
            "config": {"on": True, "url": "/whatever"},
        }
        shaped = shape_sensor_info(record)
        # FR-47: CLIP* virtual sensors emit raw state + config.
        assert shaped["state"] == {"flag": True, "lastupdated": "2026-04-27T14:00:00"}
        assert shaped["config"] == {"on": True, "url": "/whatever"}

    def test_unknown_type_passes_through_state_and_config(self) -> None:
        record = {
            "id": "200",
            "name": "Future Sensor",
            "type": "ZLLFutureType",
            "model_id": "FUT001",
            "state": {"value": 42},
            "config": {"on": True},
        }
        shaped = shape_sensor_info(record)
        assert shaped["state"] == {"value": 42}
        assert shaped["config"] == {"on": True}


# --- Verb integration tests --------------------------------------------------


class FakeSensorWrapper:
    """Minimal wrapper exposing sensor records for the sensor verb tests."""

    def __init__(self, sensors: list[dict[str, Any]]) -> None:
        self._sensors = sensors

    async def list_sensors_records(self) -> list[dict[str, Any]]:
        return list(self._sensors)

    # The other Protocol surfaces are unused by sensor_cmd but must exist for
    # the alias path through list_cmd.list_sensors when ``sensor list`` runs.
    async def list_lights_records(self) -> list[dict[str, Any]]:
        return []

    async def list_groups_records(self) -> list[dict[str, Any]]:
        return []

    async def list_scenes_records(self) -> list[dict[str, Any]]:
        return []

    async def list_schedules_records(self) -> list[dict[str, Any]]:
        return []

    async def get_bridge_record(self) -> dict[str, Any]:
        return {}

    async def resolve_target(self, target: str) -> dict[str, Any]:
        return {"kind": "unknown", "record": {}, "object": None}

    async def light_set_on(self, light: Any, on: bool) -> None:  # pragma: no cover
        return None

    async def group_set_on(self, group: Any, on: bool) -> None:  # pragma: no cover
        return None

    async def light_set_state(self, light: Any, **state: Any) -> None:  # pragma: no cover
        return None

    async def group_set_action(self, group: Any, **action: Any) -> None:  # pragma: no cover
        return None

    async def get_all_lights_group(self) -> Any:  # pragma: no cover
        return None

    async def apply_scene(
        self,
        scene_id: str,
        group_id: str | None,
        *,
        transitiontime: int | None,
    ) -> None:  # pragma: no cover
        return None

    async def __aenter__(self) -> FakeSensorWrapper:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        return None


def _ctx(wrapper: FakeSensorWrapper, fmt: OutputFormat = OutputFormat.JSON) -> dict[str, Any]:
    return {"wrapper": wrapper, "format": fmt}


class TestSensorInfoVerb:
    def test_motion_sensor_info_returns_json_with_presence(self) -> None:
        wrapper = FakeSensorWrapper(
            [
                {
                    "id": "5",
                    "name": "Motion Hallway",
                    "type": "ZLLPresence",
                    "model_id": "SML001",
                    "state": {"presence": True, "lastupdated": "2026-04-27T14:00:00"},
                    "config": {"on": True, "battery": 87, "reachable": True},
                }
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            main, ["--json", "sensor", "info", "Motion Hallway"], obj=_ctx(wrapper)
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["presence"] is True
        assert data["battery"] == 87

    def test_temperature_sensor_info_emits_float_celsius(self) -> None:
        wrapper = FakeSensorWrapper(
            [
                {
                    "id": "8",
                    "name": "Hallway Temp",
                    "type": "ZLLTemperature",
                    "model_id": "SML001",
                    "state": {"temperature": 2150, "lastupdated": "2026-04-27T14:00:00"},
                    "config": {"on": True, "battery": 88, "reachable": True},
                }
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            main, ["--json", "sensor", "info", "Hallway Temp"], obj=_ctx(wrapper)
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["temperature"] == 21.5

    def test_unknown_sensor_exits_4(self) -> None:
        wrapper = FakeSensorWrapper([])
        runner = CliRunner()
        result = runner.invoke(main, ["sensor", "info", "no-such"], obj=_ctx(wrapper))
        assert result.exit_code == 4

    def test_resolve_by_id_works(self) -> None:
        wrapper = FakeSensorWrapper(
            [
                {
                    "id": "42",
                    "name": "Some Sensor",
                    "type": "ZLLPresence",
                    "model_id": "SML001",
                    "state": {"presence": False, "lastupdated": "2026-04-27T14:00:00"},
                    "config": {"on": True, "battery": 50, "reachable": True},
                }
            ]
        )
        runner = CliRunner()
        result = runner.invoke(main, ["--json", "sensor", "info", "42"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["id"] == "42"
        assert data["presence"] is False

    def test_case_insensitive_name_resolve(self) -> None:
        wrapper = FakeSensorWrapper(
            [
                {
                    "id": "1",
                    "name": "Motion Hallway",
                    "type": "ZLLPresence",
                    "model_id": "SML001",
                    "state": {"presence": True, "lastupdated": "2026-04-27T14:00:00"},
                    "config": {"on": True, "battery": 87, "reachable": True},
                }
            ]
        )
        runner = CliRunner()
        result = runner.invoke(
            main, ["--json", "sensor", "info", "motion hallway"], obj=_ctx(wrapper)
        )
        assert result.exit_code == 0, result.output


class TestSensorListAlias:
    def test_sensor_list_emits_same_as_list_sensors(self) -> None:
        sensors = [
            {
                "id": "1",
                "name": "Motion Hallway",
                "type": "ZLLPresence",
                "model_id": "SML001",
                "state": {"presence": False, "lastupdated": "2026-04-27T14:00:00"},
                "config": {"on": True, "battery": 87, "reachable": True},
            }
        ]
        wrapper = FakeSensorWrapper(sensors)
        runner = CliRunner()
        result_alias = runner.invoke(
            main, ["--json", "sensor", "list"], obj=_ctx(wrapper, OutputFormat.JSON)
        )
        result_canonical = runner.invoke(
            main, ["--json", "list", "sensors"], obj=_ctx(wrapper, OutputFormat.JSON)
        )
        assert result_alias.exit_code == 0, result_alias.output
        assert result_canonical.exit_code == 0, result_canonical.output
        assert json.loads(result_alias.output) == json.loads(result_canonical.output)
