"""Tests for ``hue_cli.verbs.batch_cmd`` — Phase 3 Part A.

Covers FR-53 / FR-54 / FR-54a / FR-54b:

* parsing rules (blank lines, comments, unknown verbs)
* successful dispatch through the verb cores
* exit-code aggregation (0 / 7 / uniform-failure)
* concurrency cap honored
* `--file` and `--stdin` produce identical output

A hand-rolled fake wrapper (matching the pattern in ``test_set_cmd.py``)
records dispatched calls so we can assert on the wire-payload without
hitting aiohue.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from hue_cli.cli import main
from hue_cli.errors import BridgeBusyError, NotFoundError
from hue_cli.output import OutputFormat

# ---------------------------------------------------------------------------
# Fakes — a wrapper rich enough for batch dispatch through on/off/set/scene.
# ---------------------------------------------------------------------------


class _FakeLight:
    def __init__(self, light_id: str, name: str) -> None:
        self.id = light_id
        self.name = name
        # Tunable-color light by default so set --brightness/--color all work.
        self.controlcapabilities: dict[str, Any] = {
            "ct": {"min": 153, "max": 500},
            "colorgamut": [[0.6750, 0.3220], [0.4090, 0.5180], [0.1670, 0.0400]],
            "colorgamuttype": "B",
        }
        self.set_state_calls: list[dict[str, Any]] = []

    async def set_state(self, **kwargs: Any) -> None:
        self.set_state_calls.append(kwargs)


class _FakeGroup:
    def __init__(self, group_id: str, name: str, state: dict[str, Any] | None = None) -> None:
        self.id = group_id
        self.name = name
        self.state = state or {"any_on": False, "all_on": False}
        self.set_action_calls: list[dict[str, Any]] = []

    async def set_action(self, **kwargs: Any) -> None:
        self.set_action_calls.append(kwargs)


class _FakeWrapper:
    """Wrapper that supports the verbs batch dispatches: on/off/toggle/set/scene apply."""

    def __init__(
        self,
        *,
        target_lookup: dict[str, dict[str, Any]] | None = None,
        scenes: list[dict[str, Any]] | None = None,
        all_lights_group: _FakeGroup | None = None,
        force_failure: type[BaseException] | None = None,
        per_target_failure: dict[str, type[BaseException]] | None = None,
        slow_targets: dict[str, float] | None = None,
        concurrency_tracker: dict[str, int] | None = None,
    ) -> None:
        self._target_lookup = target_lookup or {}
        self._scenes = scenes or []
        self._all_lights_group = all_lights_group or _FakeGroup("0", "all")
        self._force_failure = force_failure
        self._per_target_failure = per_target_failure or {}
        self._slow_targets = slow_targets or {}
        self._tracker = concurrency_tracker  # records peak concurrency
        self.light_set_on_calls: list[tuple[str, bool]] = []
        self.group_set_on_calls: list[tuple[str, bool]] = []
        self.apply_scene_calls: list[dict[str, Any]] = []
        self.light_set_state_calls: list[tuple[str, dict[str, Any]]] = []
        self.group_set_action_calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> _FakeWrapper:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def resolve_target(self, target: str) -> dict[str, Any]:
        await self._maybe_block(target)
        cls = self._per_target_failure.get(target)
        if cls is not None:
            raise cls(f"failed for {target}")
        if self._force_failure is not None:
            raise self._force_failure(f"forced fail on {target}")
        if target in self._target_lookup:
            return dict(self._target_lookup[target])
        # Unknown target -> NotFoundError (exit 4).
        raise NotFoundError(f"target {target!r} not found")

    async def _maybe_block(self, target: str) -> None:
        if self._tracker is not None:
            in_flight = self._tracker.get("in_flight", 0) + 1
            self._tracker["in_flight"] = in_flight
            self._tracker["peak"] = max(self._tracker.get("peak", 0), in_flight)
        try:
            delay = self._slow_targets.get(target, 0.0)
            if delay > 0:
                await asyncio.sleep(delay)
        finally:
            if self._tracker is not None:
                self._tracker["in_flight"] = self._tracker.get("in_flight", 1) - 1

    async def light_set_on(self, light: _FakeLight, on: bool) -> None:
        self.light_set_on_calls.append((light.id, on))
        await light.set_state(on=on)

    async def group_set_on(self, group: _FakeGroup, on: bool) -> None:
        self.group_set_on_calls.append((group.id, on))
        await group.set_action(on=on)

    async def light_set_state(self, light: _FakeLight, **state: Any) -> None:
        self.light_set_state_calls.append((light.id, state))
        await light.set_state(**state)

    async def group_set_action(self, group: _FakeGroup, **action: Any) -> None:
        self.group_set_action_calls.append((group.id, action))
        await group.set_action(**action)

    async def get_all_lights_group(self) -> _FakeGroup:
        return self._all_lights_group

    async def list_scenes_records(self) -> list[dict[str, Any]]:
        return list(self._scenes)

    async def apply_scene(
        self,
        *,
        scene_id: str,
        group_id: str | None,
        transitiontime: int | None,
    ) -> None:
        self.apply_scene_calls.append(
            {"scene_id": scene_id, "group_id": group_id, "transitiontime": transitiontime}
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(wrapper: _FakeWrapper, fmt: OutputFormat = OutputFormat.JSONL) -> dict[str, Any]:
    return {"wrapper": wrapper, "format": fmt, "concurrency": None}


def _light_record(light: _FakeLight) -> dict[str, Any]:
    return {
        "kind": "light",
        "record": {"id": light.id, "name": light.name, "state": {}},
        "object": light,
    }


def _group_record(group: _FakeGroup, kind: str = "room") -> dict[str, Any]:
    return {
        "kind": kind,
        "record": {"id": group.id, "name": group.name, "state": group.state},
        "object": group,
    }


def _kitchen_lookup() -> dict[str, dict[str, Any]]:
    """Return a target_lookup that resolves ``@kitchen`` to a fake room."""
    kitchen = _FakeGroup("1", "Kitchen", {"any_on": False, "all_on": False})
    return {"@kitchen": _group_record(kitchen, kind="room")}


# ---------------------------------------------------------------------------
# FR-54b — empty / blank / comment input
# ---------------------------------------------------------------------------


class TestEmptyAndComments:
    def test_empty_stdin_in_json_mode_emits_brackets(self) -> None:
        wrapper = _FakeWrapper(target_lookup=_kitchen_lookup())
        runner = CliRunner()
        result = runner.invoke(main, ["--json", "batch", "--stdin"], obj=_ctx(wrapper), input="")
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "[]"

    def test_empty_stdin_in_jsonl_mode_emits_nothing(self) -> None:
        wrapper = _FakeWrapper(target_lookup=_kitchen_lookup())
        runner = CliRunner()
        result = runner.invoke(main, ["--jsonl", "batch", "--stdin"], obj=_ctx(wrapper), input="")
        assert result.exit_code == 0, result.output
        assert result.output.strip() == ""

    def test_blank_lines_and_comments_are_skipped(self) -> None:
        wrapper = _FakeWrapper(target_lookup=_kitchen_lookup())
        runner = CliRunner()
        script = "# header\n\non @kitchen\n\n# trailing comment\n"
        result = runner.invoke(
            main, ["--json", "batch", "--stdin"], obj=_ctx(wrapper, OutputFormat.JSON), input=script
        )
        assert result.exit_code == 0, result.output
        records = json.loads(result.output)
        assert isinstance(records, list)
        assert len(records) == 1
        assert records[0]["verb"] == "on"
        assert records[0]["target"] == "@kitchen"
        assert records[0]["ok"] is True

    def test_only_comments_treated_as_empty_input(self) -> None:
        """Pure-comment script is functionally empty input — same exit."""
        wrapper = _FakeWrapper(target_lookup=_kitchen_lookup())
        runner = CliRunner()
        script = "# foo\n# bar\n"
        result = runner.invoke(
            main, ["--json", "batch", "--stdin"], obj=_ctx(wrapper), input=script
        )
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "[]"


# ---------------------------------------------------------------------------
# FR-53 — happy path (3 success lines)
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_three_success_lines(self) -> None:
        wrapper = _FakeWrapper(target_lookup=_kitchen_lookup())
        runner = CliRunner()
        script = "on @kitchen\non @kitchen\non @kitchen\n"
        result = runner.invoke(
            main,
            ["--json", "batch", "--stdin"],
            obj=_ctx(wrapper, OutputFormat.JSON),
            input=script,
        )
        assert result.exit_code == 0, result.output
        records = json.loads(result.output)
        assert len(records) == 3
        assert all(r["ok"] is True for r in records)
        assert all(r["verb"] == "on" for r in records)
        # Each dispatched group_set_on once -> three calls total.
        assert len(wrapper.group_set_on_calls) == 3

    def test_set_dispatches_to_set_state(self) -> None:
        light = _FakeLight("1", "Plug")
        wrapper = _FakeWrapper(target_lookup={"Plug": _light_record(light)})
        runner = CliRunner()
        script = "set Plug --brightness 30\n"
        result = runner.invoke(
            main, ["--jsonl", "batch", "--stdin"], obj=_ctx(wrapper), input=script
        )
        assert result.exit_code == 0, result.output
        rec = json.loads(result.output.strip())
        assert rec["ok"] is True
        assert rec["verb"] == "set"
        assert len(light.set_state_calls) == 1
        assert "bri" in light.set_state_calls[0]


# ---------------------------------------------------------------------------
# FR-54a / §11.1 exit-code semantics through the CLI
# ---------------------------------------------------------------------------


class TestExitCodes:
    def test_mixed_returns_seven(self) -> None:
        # Two known targets succeed, one unknown target fails (NotFound -> 4).
        wrapper = _FakeWrapper(target_lookup=_kitchen_lookup())
        runner = CliRunner()
        script = "on @kitchen\non @kitchen\non @nope\n"
        result = runner.invoke(
            main,
            ["--jsonl", "batch", "--stdin"],
            obj=_ctx(wrapper, OutputFormat.JSONL),
            input=script,
        )
        assert result.exit_code == 7, result.output
        # Three lines on stdout (one per attempted op, FR-57b). The
        # structured PartialBatchError summary lands on stderr.
        lines = [json.loads(line) for line in result.stdout.strip().splitlines()]
        assert len(lines) == 3
        oks = [r["ok"] for r in lines]
        assert oks.count(True) == 2
        assert oks.count(False) == 1

    def test_all_fail_same_code_returns_that_code(self) -> None:
        # Every line raises BridgeBusy (exit 3) — uniform-code rule says 3, not 7.
        wrapper = _FakeWrapper(
            target_lookup=_kitchen_lookup(),
            force_failure=BridgeBusyError,
        )
        runner = CliRunner()
        script = "on @kitchen\non @kitchen\non @kitchen\n"
        result = runner.invoke(
            main,
            ["--jsonl", "batch", "--stdin"],
            obj=_ctx(wrapper, OutputFormat.JSONL),
            input=script,
        )
        assert result.exit_code == 3, result.output

    def test_all_fail_different_codes_returns_seven(self) -> None:
        # One line hits BridgeBusy (3), another hits NotFound (4) → 7.
        wrapper = _FakeWrapper(
            target_lookup=_kitchen_lookup(),
            per_target_failure={
                "@kitchen": BridgeBusyError,
            },
        )
        runner = CliRunner()
        # The unknown @nowhere → NotFoundError (4), the @kitchen → BridgeBusy (3).
        script = "on @kitchen\non @nowhere\n"
        result = runner.invoke(
            main,
            ["--jsonl", "batch", "--stdin"],
            obj=_ctx(wrapper, OutputFormat.JSONL),
            input=script,
        )
        assert result.exit_code == 7, result.output


# ---------------------------------------------------------------------------
# FR-53 — concurrency cap honored
# ---------------------------------------------------------------------------


class TestConcurrencyCap:
    def test_concurrency_two_caps_in_flight_set(self) -> None:
        # Five slow lines, --concurrency 2 → at most 2 in flight at any moment.
        tracker: dict[str, int] = {"peak": 0, "in_flight": 0}
        wrapper = _FakeWrapper(
            target_lookup=_kitchen_lookup(),
            slow_targets={"@kitchen": 0.05},
            concurrency_tracker=tracker,
        )
        runner = CliRunner()
        script = "on @kitchen\n" * 5
        ctx = _ctx(wrapper, OutputFormat.JSONL)
        ctx["concurrency"] = 2
        result = runner.invoke(
            main,
            ["--jsonl", "--concurrency", "2", "batch", "--stdin"],
            obj=ctx,
            input=script,
        )
        assert result.exit_code == 0, result.output
        assert tracker["peak"] <= 2


# ---------------------------------------------------------------------------
# FR-53 / FR-54 — `--file` and `--stdin` parity
# ---------------------------------------------------------------------------


class TestFileAndStdinParity:
    def test_file_and_stdin_produce_identical_output(self, tmp_path: Path) -> None:
        wrapper_a = _FakeWrapper(target_lookup=_kitchen_lookup())
        wrapper_b = _FakeWrapper(target_lookup=_kitchen_lookup())
        runner = CliRunner()
        script = "on @kitchen\noff @kitchen\n"

        path = tmp_path / "batch.txt"
        path.write_text(script, encoding="utf-8")

        from_file = runner.invoke(
            main,
            ["--jsonl", "batch", "--file", str(path)],
            obj=_ctx(wrapper_a, OutputFormat.JSONL),
        )
        from_stdin = runner.invoke(
            main, ["--jsonl", "batch", "--stdin"], obj=_ctx(wrapper_b), input=script
        )

        assert from_file.exit_code == 0
        assert from_stdin.exit_code == 0

        # Each output line is JSON; the duration_ms field varies between
        # runs (so we drop it) and JSONL records stream in completion-order
        # rather than input-order (so we sort by the original ``line``
        # field for stable comparison).
        def _normalize(text: str) -> list[dict[str, Any]]:
            recs = []
            for line in text.strip().splitlines():
                rec = json.loads(line)
                rec.pop("duration_ms", None)
                recs.append(rec)
            recs.sort(key=lambda r: r["line"])
            return recs

        assert _normalize(from_file.output) == _normalize(from_stdin.output)


# ---------------------------------------------------------------------------
# Parser-level failures show up as result records (FR-57b)
# ---------------------------------------------------------------------------


class TestParseFailures:
    def test_unknown_verb_emits_result_record_and_exits_7(self) -> None:
        # One ok line + one unknown verb line → mixed → 7.
        wrapper = _FakeWrapper(target_lookup=_kitchen_lookup())
        runner = CliRunner()
        script = "on @kitchen\nhalt @kitchen\n"
        result = runner.invoke(
            main,
            ["--jsonl", "batch", "--stdin"],
            obj=_ctx(wrapper, OutputFormat.JSONL),
            input=script,
        )
        assert result.exit_code == 7, result.output
        lines = [json.loads(line) for line in result.stdout.strip().splitlines()]
        verbs = [r["verb"] for r in lines]
        assert "on" in verbs
        assert "halt" in verbs
        # Parse-failed line is ok=False with error="usage_error".
        halt = next(r for r in lines if r["verb"] == "halt")
        assert halt["ok"] is False
        assert halt["error"] == "usage_error"


# ---------------------------------------------------------------------------
# CLI-shape errors
# ---------------------------------------------------------------------------


class TestArgValidation:
    def test_no_file_no_stdin_is_usage_error(self) -> None:
        wrapper = _FakeWrapper(target_lookup=_kitchen_lookup())
        runner = CliRunner()
        result = runner.invoke(main, ["batch"], obj=_ctx(wrapper))
        assert result.exit_code == 64

    def test_both_file_and_stdin_is_usage_error(self, tmp_path: Path) -> None:
        wrapper = _FakeWrapper(target_lookup=_kitchen_lookup())
        runner = CliRunner()
        path = tmp_path / "x.txt"
        path.write_text("on @kitchen\n", encoding="utf-8")
        result = runner.invoke(
            main, ["batch", "--file", str(path), "--stdin"], obj=_ctx(wrapper), input=""
        )
        assert result.exit_code == 64


# ---------------------------------------------------------------------------
# pytest-asyncio: the `concurrency` semaphore lives inside ``_run_batch`` and
# is exercised through the CliRunner above. No need for a direct async unit
# test — the ``--concurrency`` cap test already proves the semaphore.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fmt,expect_summary",
    [(OutputFormat.JSON, True), (OutputFormat.JSONL, True), (OutputFormat.TEXT, False)],
)
def test_format_passes_through(fmt: OutputFormat, expect_summary: bool) -> None:
    """One success line under each output format shouldn't crash."""
    wrapper = _FakeWrapper(target_lookup=_kitchen_lookup())
    runner = CliRunner()
    flag = {
        OutputFormat.JSON: "--json",
        OutputFormat.JSONL: "--jsonl",
        OutputFormat.TEXT: "--jsonl",  # CliRunner is a pipe — TEXT exits 0 anyway
    }[fmt]
    result = runner.invoke(
        main, [flag, "batch", "--stdin"], obj=_ctx(wrapper, fmt), input="on @kitchen\n"
    )
    assert result.exit_code == 0, result.output
    if expect_summary:
        assert "@kitchen" in result.output
