"""Tests for `hue-cli scene apply` / `hue-cli scene list` (FR-39..42).

The verb tests inject a fake ``HueWrapper`` via Click's ``runner.invoke(...,
obj={"wrapper": fake, "format": ...})`` matching the pattern in
``tests/test_part_b.py``. The fake's ``apply_scene`` records calls so we can
assert the exact arguments the verb produced — including the
milliseconds-to-deciseconds rounding.
"""

from __future__ import annotations

import json
from typing import Any

from click.testing import CliRunner

from hue_cli.cli import main
from hue_cli.output import OutputFormat


class FakeSceneWrapper:
    """In-memory wrapper capturing scene-apply calls for the scene verb tests."""

    def __init__(self, scenes: list[dict[str, Any]]) -> None:
        self._scenes = scenes
        self.apply_scene_calls: list[dict[str, Any]] = []

    async def list_scenes_records(self) -> list[dict[str, Any]]:
        return list(self._scenes)

    async def apply_scene(
        self,
        scene_id: str,
        group_id: str | None,
        *,
        transitiontime: int | None,
    ) -> None:
        self.apply_scene_calls.append(
            {
                "scene_id": scene_id,
                "group_id": group_id,
                "transitiontime": transitiontime,
            }
        )

    # Other Protocol surfaces — minimal stubs so list_cmd.list_scenes (when the
    # `scene list` alias is exercised) sees the same scene fixture.
    async def list_lights_records(self) -> list[dict[str, Any]]:
        return []

    async def list_groups_records(self) -> list[dict[str, Any]]:
        return []

    async def list_sensors_records(self) -> list[dict[str, Any]]:
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

    async def __aenter__(self) -> FakeSceneWrapper:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        return None


def _ctx(wrapper: FakeSceneWrapper, fmt: OutputFormat = OutputFormat.JSON) -> dict[str, Any]:
    return {"wrapper": wrapper, "format": fmt}


def _scene(
    scene_id: str,
    name: str,
    group_id: str | None = "1",
    light_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": scene_id,
        "name": name,
        "group_id": group_id,
        "light_ids": light_ids or ["1"],
        "last_updated": None,
        "recycle": False,
        "locked": False,
        "stale": False,
    }


# --- FR-39 / FR-40: name resolution + apply -----------------------------------


class TestSceneApply:
    def test_apply_by_name_calls_wrapper_with_group_id(self) -> None:
        wrapper = FakeSceneWrapper([_scene("abcDEF1234567", "Sunset", group_id="3")])
        runner = CliRunner()
        result = runner.invoke(main, ["scene", "apply", "Sunset"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert wrapper.apply_scene_calls == [
            {"scene_id": "abcDEF1234567", "group_id": "3", "transitiontime": None}
        ]

    def test_apply_case_insensitive_name(self) -> None:
        # FR-40: scene-name resolution SHALL be case-insensitive.
        wrapper = FakeSceneWrapper([_scene("xyz999", "Movie Night", group_id="2")])
        runner = CliRunner()
        result = runner.invoke(main, ["scene", "apply", "movie night"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert len(wrapper.apply_scene_calls) == 1
        assert wrapper.apply_scene_calls[0]["scene_id"] == "xyz999"

    def test_apply_by_id_direct(self) -> None:
        # Scene ids are alphanumeric ~15-16 chars (per SRD review note).
        wrapper = FakeSceneWrapper([_scene("YsM3kP9qLrT2v", "Wind Down", group_id="1")])
        runner = CliRunner()
        result = runner.invoke(main, ["scene", "apply", "YsM3kP9qLrT2v"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert wrapper.apply_scene_calls[0]["scene_id"] == "YsM3kP9qLrT2v"

    def test_apply_with_transition_converts_ms_to_deciseconds(self) -> None:
        # FR-41: --transition 3000 -> transitiontime=30 ds.
        wrapper = FakeSceneWrapper([_scene("abcDEF1234567", "Sunset", group_id="3")])
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["scene", "apply", "Sunset", "--transition", "3000"],
            obj=_ctx(wrapper),
        )
        assert result.exit_code == 0, result.output
        assert wrapper.apply_scene_calls[0]["transitiontime"] == 30

    def test_apply_legacy_lightscene_passes_none_group_id(self) -> None:
        # FR-39 fallback: legacy LightScene with group_id=None -> wrapper falls
        # back to all-lights group recall. The verb's job is simply to forward
        # group_id=None; the wrapper handles the fallback.
        wrapper = FakeSceneWrapper([_scene("oldLightScene01", "Legacy LightScene", group_id=None)])
        runner = CliRunner()
        result = runner.invoke(main, ["scene", "apply", "oldLightScene01"], obj=_ctx(wrapper))
        assert result.exit_code == 0, result.output
        assert wrapper.apply_scene_calls == [
            {
                "scene_id": "oldLightScene01",
                "group_id": None,
                "transitiontime": None,
            }
        ]

    def test_ambiguous_name_exits_64_with_candidates_in_stderr(self) -> None:
        # FR-40: two scenes both named "Movie Night" (different groups) -> exit 64.
        wrapper = FakeSceneWrapper(
            [
                _scene("aaa111aaa1111", "Movie Night", group_id="1"),
                _scene("bbb222bbb2222", "Movie Night", group_id="2"),
            ]
        )
        runner = CliRunner()
        result = runner.invoke(main, ["scene", "apply", "Movie Night"], obj=_ctx(wrapper))
        assert result.exit_code == 64
        # Both ids and group names listed in stderr/output.
        combined = result.output + (result.stderr if result.stderr_bytes is not None else "")
        assert "aaa111aaa1111" in combined
        assert "bbb222bbb2222" in combined
        assert wrapper.apply_scene_calls == []

    def test_unknown_name_exits_4(self) -> None:
        wrapper = FakeSceneWrapper([_scene("abcDEF1234567", "Sunset", group_id="3")])
        runner = CliRunner()
        result = runner.invoke(main, ["scene", "apply", "no-such-scene"], obj=_ctx(wrapper))
        assert result.exit_code == 4
        assert wrapper.apply_scene_calls == []

    def test_unknown_id_emits_structured_error_with_target(self) -> None:
        wrapper = FakeSceneWrapper([_scene("abcDEF1234567", "Sunset", group_id="3")])
        runner = CliRunner()
        result = runner.invoke(main, ["scene", "apply", "fake-id-here"], obj=_ctx(wrapper))
        assert result.exit_code == 4
        # Structured error per §11.2 lands on stderr.
        combined = result.output + (result.stderr if result.stderr_bytes is not None else "")
        assert "fake-id-here" in combined


# --- FR-42: scene list alias --------------------------------------------------


class TestSceneList:
    def test_scene_list_alias_emits_same_records_as_list_scenes(self) -> None:
        scenes = [
            _scene("abcDEF1234567", "Sunset", group_id="3"),
            _scene("xyz999", "Movie Night", group_id="2"),
        ]
        wrapper = FakeSceneWrapper(scenes)
        runner = CliRunner()
        result_alias = runner.invoke(
            main, ["--json", "scene", "list"], obj=_ctx(wrapper, OutputFormat.JSON)
        )
        result_canonical = runner.invoke(
            main, ["--json", "list", "scenes"], obj=_ctx(wrapper, OutputFormat.JSON)
        )
        assert result_alias.exit_code == 0, result_alias.output
        assert result_canonical.exit_code == 0, result_canonical.output
        assert json.loads(result_alias.output) == json.loads(result_canonical.output)
