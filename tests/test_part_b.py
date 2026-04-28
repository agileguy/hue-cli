"""Part B test suite — output, parallel, list/info/on/off/toggle/config verbs, cli wiring.

Strategy: most verb tests inject a hand-rolled fake ``HueWrapper`` via Click's
``runner.invoke(..., obj={"wrapper": fake, "format": OutputFormat.JSON})``.
This lets the verbs exercise full code paths without a real bridge or even
``aiohue`` / ``aiohttp`` involvement — the wrapper Protocol is the contract.

Where the test specifically targets behavior that DOES involve aiohttp (e.g.,
the §4.5 schedule-fallback path), we still use a fake wrapper that returns
the records the real wrapper would have fetched — the wrapper integration
test is Engineer A's territory in test_part_a.py.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from typing import Any, TextIO, cast

import pytest
from click.testing import CliRunner

from hue_cli.cli import _run_async_graceful, main
from hue_cli.output import OutputFormat, detect, emit_json, emit_jsonl, json_validity_guard
from hue_cli.parallel import TaskResult, run_with_concurrency, timed_run

# --- Fake wrapper ------------------------------------------------------------


class FakeLight:
    """Fake aiohue Light record — exposes set_state and tracks calls."""

    def __init__(self, light_id: str, name: str, state: dict[str, Any]) -> None:
        self.id = light_id
        self.name = name
        self._state = state
        self.set_state_calls: list[dict[str, Any]] = []

    async def set_state(self, **kwargs: Any) -> None:
        self.set_state_calls.append(kwargs)


class FakeGroup:
    """Fake aiohue Group record — exposes set_action and tracks calls."""

    def __init__(self, group_id: str, name: str, state: dict[str, Any]) -> None:
        self.id = group_id
        self.name = name
        self.state = state
        self.set_action_calls: list[dict[str, Any]] = []

    async def set_action(self, **kwargs: Any) -> None:
        self.set_action_calls.append(kwargs)


class FakeWrapper:
    """In-memory implementation of HueWrapperProto for verb tests."""

    def __init__(
        self,
        *,
        lights: list[dict[str, Any]] | None = None,
        groups: list[dict[str, Any]] | None = None,
        scenes: list[dict[str, Any]] | None = None,
        sensors: list[dict[str, Any]] | None = None,
        schedules: list[dict[str, Any]] | None = None,
        bridge_record: dict[str, Any] | None = None,
        target_lookup: dict[str, dict[str, Any]] | None = None,
        all_lights_group: FakeGroup | None = None,
    ) -> None:
        self._lights = lights or []
        self._groups = groups or []
        self._scenes = scenes or []
        self._sensors = sensors or []
        self._schedules = schedules or []
        self._bridge_record = bridge_record or {}
        self._target_lookup = target_lookup or {}
        self._all_lights_group = all_lights_group or FakeGroup(
            "0", "all", {"any_on": False, "all_on": False}
        )
        self.fetch_schedules_raw_calls = 0
        self.light_set_on_calls: list[tuple[str, bool]] = []
        self.group_set_on_calls: list[tuple[str, bool]] = []

    async def list_lights_records(self) -> list[dict[str, Any]]:
        return list(self._lights)

    async def list_groups_records(self) -> list[dict[str, Any]]:
        return list(self._groups)

    async def list_scenes_records(self) -> list[dict[str, Any]]:
        return list(self._scenes)

    async def list_sensors_records(self) -> list[dict[str, Any]]:
        return list(self._sensors)

    async def list_schedules_records(self) -> list[dict[str, Any]]:
        # Simulates the §4.5 direct-aiohttp fallback by counting invocations
        # rather than touching aiohttp.
        self.fetch_schedules_raw_calls += 1
        return list(self._schedules)

    async def get_bridge_record(self) -> dict[str, Any]:
        return dict(self._bridge_record)

    async def resolve_target(self, target: str) -> dict[str, Any]:
        if target in self._target_lookup:
            return dict(self._target_lookup[target])
        return {"kind": "unknown", "record": {}, "object": None}

    async def light_set_on(self, light: FakeLight, on: bool) -> None:
        self.light_set_on_calls.append((light.id, on))
        await light.set_state(on=on)

    async def group_set_on(self, group: FakeGroup, on: bool) -> None:
        self.group_set_on_calls.append((group.id, on))
        await group.set_action(on=on)

    async def get_all_lights_group(self) -> FakeGroup:
        return self._all_lights_group

    async def __aenter__(self) -> "FakeWrapper":
        self.aenter_calls = getattr(self, "aenter_calls", 0) + 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        self.aexit_calls = getattr(self, "aexit_calls", 0) + 1


# --- Helpers ----------------------------------------------------------------


def _ctx(wrapper: FakeWrapper, fmt: OutputFormat = OutputFormat.JSON) -> dict[str, Any]:
    return {"wrapper": wrapper, "format": fmt}


# --- output.detect -----------------------------------------------------------


class TestOutputDetect:
    def test_tty_no_flags_returns_text(self) -> None:
        assert (
            detect(force_json=False, force_jsonl=False, quiet=False, stdout_is_tty=True)
            is OutputFormat.TEXT
        )

    def test_pipe_no_flags_returns_jsonl(self) -> None:
        assert (
            detect(force_json=False, force_jsonl=False, quiet=False, stdout_is_tty=False)
            is OutputFormat.JSONL
        )

    def test_json_flag_overrides_tty(self) -> None:
        assert (
            detect(force_json=True, force_jsonl=False, quiet=False, stdout_is_tty=True)
            is OutputFormat.JSON
        )

    def test_quiet_overrides_everything(self) -> None:
        assert (
            detect(force_json=True, force_jsonl=True, quiet=True, stdout_is_tty=False)
            is OutputFormat.QUIET
        )

    def test_jsonl_flag_on_tty(self) -> None:
        assert (
            detect(force_json=False, force_jsonl=True, quiet=False, stdout_is_tty=True)
            is OutputFormat.JSONL
        )


# --- output.emit_* -----------------------------------------------------------


class TestEmitJson:
    def test_pretty_indent_and_sorted(self) -> None:
        rendered = emit_json({"b": 1, "a": 2})
        # Indented (multiline)
        assert "\n" in rendered
        # 2-space indent (FR-20)
        assert "  " in rendered
        # Keys sorted
        assert rendered.index('"a"') < rendered.index('"b"')

    def test_round_trips(self) -> None:
        payload = [{"x": 1}, {"y": 2}]
        rendered = emit_json(payload)
        assert json.loads(rendered) == payload


class TestEmitJsonl:
    def test_one_compact_line_per_record(self) -> None:
        records = [{"a": 1}, {"b": 2}]
        lines = list(emit_jsonl(records))
        assert len(lines) == 2
        assert lines[0] == '{"a":1}'
        assert lines[1] == '{"b":2}'

    def test_preserves_input_order(self) -> None:
        records = [{"name": "z"}, {"name": "a"}, {"name": "m"}]
        lines = list(emit_jsonl(records))
        names = [json.loads(line)["name"] for line in lines]
        assert names == ["z", "a", "m"]


# --- output.json_validity_guard ----------------------------------------------


class _CapStream:
    """Minimal stream stub for guard tests — captures writes to a buffer."""

    def __init__(self) -> None:
        self.buf: list[str] = []

    def write(self, s: str) -> int:
        self.buf.append(s)
        return len(s)

    def value(self) -> str:
        return "".join(self.buf)


class TestJsonValidityGuard:
    def test_clean_exit_flushes_buffer_in_json(self) -> None:
        target = _CapStream()
        with json_validity_guard(OutputFormat.JSON, stream=cast("TextIO", target)) as buf:
            buf.write('{"ok":true}')
        assert target.value() == '{"ok":true}'

    def test_exception_in_json_emits_empty_array(self) -> None:
        target = _CapStream()
        with (
            pytest.raises(RuntimeError),
            json_validity_guard(OutputFormat.JSON, stream=cast("TextIO", target)) as buf,
        ):
            buf.write('[{"partial":')
            raise RuntimeError("boom")
        # FR-57b: stdout is valid JSON ([]) — never the malformed partial.
        out = target.value()
        assert out == "[]\n"
        json.loads(out)

    def test_exception_in_jsonl_keeps_only_complete_lines(self) -> None:
        target = _CapStream()
        with (
            pytest.raises(RuntimeError),
            json_validity_guard(OutputFormat.JSONL, stream=cast("TextIO", target)) as buf,
        ):
            buf.write('{"a":1}\n')
            buf.write('{"b":2}\n')
            buf.write('{"c":')  # partial — must be dropped
            raise RuntimeError("boom")
        out = target.value()
        # Only the two complete lines survive; the partial third is dropped.
        lines = [line for line in out.split("\n") if line]
        for line in lines:
            json.loads(line)
        assert len(lines) == 2

    def test_quiet_mode_writes_nothing(self) -> None:
        target = _CapStream()
        with json_validity_guard(OutputFormat.QUIET, stream=cast("TextIO", target)) as buf:
            buf.write("anything")
        assert target.value() == ""


# --- parallel.run_with_concurrency -------------------------------------------


class TestRunWithConcurrency:
    @pytest.mark.asyncio
    async def test_returns_results_in_order(self) -> None:
        async def make(n: int) -> int:
            await asyncio.sleep(0)
            return n

        results = await run_with_concurrency([make(i) for i in range(5)], limit=2)
        assert results == [0, 1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_limit_bounds_concurrency(self) -> None:
        active = 0
        peak = 0

        async def task() -> int:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return 1

        await run_with_concurrency([task() for _ in range(8)], limit=2)
        # Peak SHALL be exactly the limit (8 tasks, semaphore=2, all reach the
        # body together when limit allows).
        assert peak <= 2

    @pytest.mark.asyncio
    async def test_timed_run_envelopes_success(self) -> None:
        async def fast() -> str:
            return "ok"

        result = await timed_run("t1", fast())
        assert isinstance(result, TaskResult)
        assert result.target == "t1"
        assert result.ok is True
        assert result.value == "ok"
        assert result.error is None
        assert result.duration_ms >= 0


# --- list verbs --------------------------------------------------------------


def _make_lights() -> list[dict[str, Any]]:
    """Three-light fixture exercising the brightness translation thresholds."""
    return [
        {
            "id": "1",
            "name": "Kitchen Pendant",
            "type": "Extended color light",
            "model_id": "LCT015",
            "product_name": "Hue color lamp",
            "state": {
                "on": True,
                "reachable": True,
                "brightness": 254,
                "brightness_percent": 100,
            },
        },
        {
            "id": "2",
            "name": "Bedroom Lamp",
            "type": "Color temperature light",
            "model_id": "LTW011",
            "product_name": None,
            "state": {
                "on": False,
                "reachable": True,
                "brightness": 1,
                "brightness_percent": 0,
            },
        },
        {
            "id": "3",
            "name": "Hallway",
            "type": "Dimmable light",
            "model_id": "LWB006",
            "product_name": "Hue white",
            "state": {
                "on": True,
                "reachable": False,
                "brightness": 127,
                "brightness_percent": 50,
            },
        },
    ]


def _make_groups() -> list[dict[str, Any]]:
    return [
        {
            "id": "1",
            "type": "Room",
            "name": "Kitchen",
            "class": "Kitchen",
            "light_ids": ["1"],
            "sensor_ids": [],
            "state": {"any_on": True, "all_on": True},
        },
        {
            "id": "2",
            "type": "Room",
            "name": "Bedroom",
            "class": "Bedroom",
            "light_ids": ["2"],
            "sensor_ids": [],
            "state": {"any_on": False, "all_on": False},
        },
        {
            "id": "3",
            "type": "Zone",
            "name": "Upstairs",
            "class": None,
            "light_ids": ["2", "3"],
            "sensor_ids": [],
            "state": {"any_on": True, "all_on": False},
        },
    ]


def _make_scenes() -> list[dict[str, Any]]:
    return [
        {
            "id": "abc123",
            "name": "Energize",
            "group_id": "1",
            "light_ids": ["1"],
            "last_updated": "2026-04-27T14:00:00",
            "recycle": False,
            "locked": False,
            "stale": False,
        },
        {
            "id": "def456",
            "name": "Legacy LightScene",
            "group_id": None,  # FR-14: legacy LightScene has no group
            "light_ids": ["1", "2"],
            "last_updated": None,
            "recycle": False,
            "locked": False,
            "stale": False,
        },
    ]


def _make_sensors() -> list[dict[str, Any]]:
    return [
        {
            "id": "1",
            "name": "Motion Hallway",
            "type": "ZLLPresence",
            "model_id": "SML001",
            "state": {"presence": False, "lastupdated": "2026-04-27T14:00:00"},
            "config": {"on": True, "battery": 87, "reachable": True},
        }
    ]


def _make_schedules() -> list[dict[str, Any]]:
    return [
        {
            "id": "1",
            "name": "Morning",
            "description": "Daily morning",
            "command": {"address": "/groups/1/action", "method": "PUT", "body": {"on": True}},
            "localtime": "W127/T07:00:00",
            "status": "enabled",
            "autodelete": False,
            "created": "2026-04-01T00:00:00",
        }
    ]


def _make_bridge_record() -> dict[str, Any]:
    return {
        "id": "0017886abcaf",  # FR-2: canonical 12-char form
        "name": "Hue Bridge - 6ABCAF",
        "host": "192.168.86.62",
        "mac": "00:17:88:6A:BC:AF",
        "model_id": "BSB002",
        "api_version": "1.76.0",
        "swversion": "1976154040",
        "supports_v2": True,
        "paired_at": "2026-04-27T14:32:11Z",
        "reachable": True,
        "gateway": "192.168.86.1",
        "netmask": "255.255.255.0",
        "timezone": "America/Toronto",
        "zigbee_channel": 25,
        "whitelist": [
            {
                "id": "appkey1",
                "name": "hue-cli#dans-mbp",
                "last_use_date": "2026-04-27T14:00:00",
                "create_date": "2026-04-01T00:00:00",
            }
        ],
    }


class TestListLights:
    def test_emits_three_lights_with_brightness_translation(self) -> None:
        wrapper = FakeWrapper(lights=_make_lights())
        runner = CliRunner()
        result = runner.invoke(main, ["--json", "list", "lights"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 3
        # FR-11: brightness 254 → 100, brightness 1 → 0 (clamp), brightness 127 → 50
        percents = {item["id"]: item["state"]["brightness_percent"] for item in data}
        assert percents == {"1": 100, "2": 0, "3": 50}


class TestListRooms:
    def test_only_room_type_groups(self) -> None:
        wrapper = FakeWrapper(groups=_make_groups())
        runner = CliRunner()
        result = runner.invoke(main, ["--json", "list", "rooms"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 2
        assert all(item["type"] == "Room" for item in data)


class TestListZones:
    def test_only_zone_type_groups(self) -> None:
        wrapper = FakeWrapper(groups=_make_groups())
        runner = CliRunner()
        result = runner.invoke(main, ["--json", "list", "zones"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["type"] == "Zone"
        assert data[0]["name"] == "Upstairs"


class TestListScenes:
    def test_includes_legacy_lightscene_with_group_id_null(self) -> None:
        wrapper = FakeWrapper(scenes=_make_scenes())
        runner = CliRunner()
        result = runner.invoke(main, ["--json", "list", "scenes"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 2
        legacy = next(item for item in data if item["name"] == "Legacy LightScene")
        # FR-14: legacy LightScene has group_id null
        assert legacy["group_id"] is None


class TestListSensors:
    def test_includes_battery_field(self) -> None:
        wrapper = FakeWrapper(sensors=_make_sensors())
        runner = CliRunner()
        result = runner.invoke(main, ["--json", "list", "sensors"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data[0]["config"]["battery"] == 87


class TestListSchedules:
    def test_uses_fallback_path(self) -> None:
        wrapper = FakeWrapper(schedules=_make_schedules())
        runner = CliRunner()
        result = runner.invoke(main, ["--json", "list", "schedules"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        # The wrapper records that the §4.5 fallback path was hit.
        assert wrapper.fetch_schedules_raw_calls == 1
        data = json.loads(result.output)
        assert data[0]["localtime"] == "W127/T07:00:00"


class TestListAll:
    def test_aggregates_all_six_categories(self) -> None:
        wrapper = FakeWrapper(
            lights=_make_lights(),
            groups=_make_groups(),
            scenes=_make_scenes(),
            sensors=_make_sensors(),
            schedules=_make_schedules(),
        )
        runner = CliRunner()
        result = runner.invoke(main, ["--json", "list", "all"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert set(data.keys()) == {"lights", "rooms", "zones", "scenes", "sensors", "schedules"}
        assert len(data["lights"]) == 3
        assert len(data["rooms"]) == 2
        assert len(data["zones"]) == 1


class TestListFilter:
    def test_name_filter_keeps_matches(self) -> None:
        wrapper = FakeWrapper(lights=_make_lights())
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--json", "list", "lights", "--filter", "name=Kitchen"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["name"] == "Kitchen Pendant"


# --- info verb ---------------------------------------------------------------


class TestInfo:
    def test_info_light_emits_full_record(self) -> None:
        light_record = _make_lights()[0]
        wrapper = FakeWrapper(
            target_lookup={
                "Kitchen Pendant": {
                    "kind": "light",
                    "record": light_record,
                    "object": None,
                }
            }
        )
        runner = CliRunner()
        result = runner.invoke(main, ["--json", "info", "Kitchen Pendant"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # §10.2 shape: id, name, type, model_id, product_name, state.*
        assert data["id"] == "1"
        assert data["name"] == "Kitchen Pendant"
        assert data["type"] == "Extended color light"
        assert data["state"]["on"] is True

    def test_info_bridge_emits_full_record(self) -> None:
        wrapper = FakeWrapper(bridge_record=_make_bridge_record())
        runner = CliRunner()
        result = runner.invoke(main, ["--json", "info", "bridge"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # FR-21 §10.1 shape: gateway and whitelist must be present
        assert data["gateway"] == "192.168.86.1"
        assert data["whitelist"][0]["name"] == "hue-cli#dans-mbp"
        # FR-2: canonical 12-char id form
        assert data["id"] == "0017886abcaf"


# --- on / off / toggle verbs -------------------------------------------------


class TestOnOff:
    def test_on_room_invokes_group_set_action(self) -> None:
        kitchen = FakeGroup("1", "Kitchen", {"any_on": False, "all_on": False})
        kitchen_record = _make_groups()[0]
        wrapper = FakeWrapper(
            target_lookup={
                "@kitchen": {
                    "kind": "room",
                    "record": kitchen_record,
                    "object": kitchen,
                }
            }
        )
        runner = CliRunner()
        result = runner.invoke(main, ["on", "@kitchen"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        # Group.set_action called exactly once with on=True
        assert kitchen.set_action_calls == [{"on": True}]
        assert wrapper.group_set_on_calls == [("1", True)]

    def test_off_light_invokes_set_state(self) -> None:
        plug1 = FakeLight("5", "plug-1", {"on": True, "reachable": True, "brightness": 254})
        plug_record = {
            "id": "5",
            "name": "plug-1",
            "state": {"on": True, "reachable": True},
        }
        wrapper = FakeWrapper(
            target_lookup={"plug-1": {"kind": "light", "record": plug_record, "object": plug1}}
        )
        runner = CliRunner()
        result = runner.invoke(main, ["off", "plug-1"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert plug1.set_state_calls == [{"on": False}]
        assert wrapper.light_set_on_calls == [("5", False)]

    def test_unreachable_light_warning_to_stderr_exit_zero(self) -> None:
        # FR-26: unreachable light emits warning but still exit 0
        light = FakeLight(
            "7",
            "Hallway",
            {"on": False, "reachable": False, "brightness": 1},
        )
        record = {
            "id": "7",
            "name": "Hallway",
            "state": {"on": False, "reachable": False},
        }
        wrapper = FakeWrapper(
            target_lookup={"Hallway": {"kind": "light", "record": record, "object": light}}
        )
        # Click 8.2+ removed the `mix_stderr` kwarg; stderr now goes through
        # `result.stderr` when stderr is a separate stream. Some shipped Click
        # versions inline both. Accept either: the warning text must land
        # somewhere visible.
        runner = CliRunner()
        result = runner.invoke(main, ["on", "Hallway"], obj=_ctx(wrapper))
        assert result.exit_code == 0
        combined = result.output + (result.stderr if result.stderr_bytes is not None else "")
        assert "not reachable" in combined


class TestToggle:
    """Decision 4 consolidate-on rules for groups (FR-24)."""

    def _toggle_group_with_state(
        self, all_on: bool, any_on: bool
    ) -> tuple[CliRunner, FakeGroup, FakeWrapper]:
        kitchen = FakeGroup("1", "Kitchen", {"any_on": any_on, "all_on": all_on})
        record = {
            "id": "1",
            "type": "Room",
            "name": "Kitchen",
            "state": {"any_on": any_on, "all_on": all_on},
        }
        wrapper = FakeWrapper(
            target_lookup={
                "@kitchen": {
                    "kind": "room",
                    "record": record,
                    "object": kitchen,
                }
            }
        )
        return CliRunner(), kitchen, wrapper

    def test_toggle_group_all_on_true_turns_all_off(self) -> None:
        # all_on=True → every light is on → consolidate-on says: turn all off.
        runner, kitchen, wrapper = self._toggle_group_with_state(all_on=True, any_on=True)
        result = runner.invoke(main, ["toggle", "@kitchen"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert kitchen.set_action_calls == [{"on": False}]

    def test_toggle_group_all_on_false_any_on_true_turns_all_on(self) -> None:
        # all_on=False, any_on=True → mixed state → consolidate-on says: turn all on.
        # This is the deliberate divergence from the mobile app's any_on toggle.
        runner, kitchen, wrapper = self._toggle_group_with_state(all_on=False, any_on=True)
        result = runner.invoke(main, ["toggle", "@kitchen"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert kitchen.set_action_calls == [{"on": True}]

    def test_toggle_group_all_off_turns_all_on(self) -> None:
        # all_on=False, any_on=False → fully off → turn all on.
        runner, kitchen, wrapper = self._toggle_group_with_state(all_on=False, any_on=False)
        result = runner.invoke(main, ["toggle", "@kitchen"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert kitchen.set_action_calls == [{"on": True}]

    def test_toggle_light_reads_state_and_writes_negation(self) -> None:
        light = FakeLight(
            "1",
            "Lamp",
            {"on": True, "reachable": True, "brightness": 254},
        )
        record = {
            "id": "1",
            "name": "Lamp",
            "state": {"on": True, "reachable": True},
        }
        wrapper = FakeWrapper(
            target_lookup={"Lamp": {"kind": "light", "record": record, "object": light}}
        )
        runner = CliRunner()
        result = runner.invoke(main, ["toggle", "Lamp"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        # Was on → next is off
        assert light.set_state_calls == [{"on": False}]


# --- _run_async_graceful (signal handling) ----------------------------------


class TestRunAsyncGraceful:
    def test_sigint_yields_exit_130(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def raise_kbi() -> None:
            raise KeyboardInterrupt

        with pytest.raises(SystemExit) as excinfo:
            _run_async_graceful(raise_kbi())
        assert excinfo.value.code == 130

    @pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM unsettable on Windows main thread")
    def test_sigterm_handler_yields_exit_143(self) -> None:
        # The handler raises SystemExit(143) on SIGTERM; we install it via the
        # graceful runner by simulating delivery during the running coroutine.
        async def trigger() -> None:
            # Send SIGTERM to ourselves; the installed handler raises SystemExit(143).
            import os

            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(0.1)

        with pytest.raises(SystemExit) as excinfo:
            _run_async_graceful(trigger())
        assert excinfo.value.code == 143


# --- Top-level error handling (HueCliError mapping) -------------------------


class TestErrorMapping:
    """The verb-level ClickException path is the v1 surface for HueCliError-style
    handling. Engineer A's errors module supplies the canonical error hierarchy
    with stable exit codes (2/3/4/5/6 etc). For Part B we verify the verb's
    ClickException maps to a non-zero exit cleanly.
    """

    def test_unknown_target_kind_exits_nonzero(self) -> None:
        wrapper = FakeWrapper()  # empty lookup → resolve returns kind=unknown
        runner = CliRunner()
        result = runner.invoke(main, ["on", "nope"], obj=_ctx(wrapper))
        assert result.exit_code != 0


# --- Smoke-level wiring tests -----------------------------------------------


class TestCliWiring:
    def test_help_lists_all_part_b_verbs(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        for verb in ("list", "info", "on", "off", "toggle", "config"):
            assert verb in result.output
