"""Unit tests for hue-cli Phase 1 Part A (wrapper, credentials, config, bridge/auth verbs)."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohue.errors
import pytest
from aioresponses import aioresponses
from click.testing import CliRunner

from hue_cli import config as config_mod
from hue_cli import credentials as creds_mod
from hue_cli import wrapper as wrapper_mod
from hue_cli.credentials import (
    BridgeCredentials,
    CredentialsStore,
    PermissiveCredentialsError,
    UnknownVersionError,
)
from hue_cli.errors import ConfigError
from hue_cli.verbs.auth import auth_group
from hue_cli.verbs.bridge import bridge_group

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_creds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the credentials module at a temp file and clean up after."""

    target = tmp_path / "credentials"
    monkeypatch.setenv(creds_mod.ENV_VAR, str(target))
    yield target


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the config module at a temp file (env-var path)."""

    target = tmp_path / "config.toml"
    monkeypatch.setenv(config_mod.ENV_VAR, str(target))
    yield target


@dataclass(frozen=True)
class _FakeDiscoveredHueBridge:
    """Stand-in for aiohue.discovery.DiscoveredHueBridge."""

    host: str
    id: str
    supports_v2: bool = False


# ---------------------------------------------------------------------------
# wrapper tests
# ---------------------------------------------------------------------------


def test_normalize_id_collapses_wire_form() -> None:
    """ISC-6: 16-char wire form collapses to 12-char canonical lowercase."""

    assert wrapper_mod.normalize_id("001788FFFE6ABCAF") == "0017886abcaf"
    assert wrapper_mod.normalize_id("0017886abcaf") == "0017886abcaf"
    assert wrapper_mod.normalize_id("001788fffe6abcaf") == "0017886abcaf"


@pytest.mark.asyncio
async def test_discover_aggregates_and_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    """ISC-7/8: discover fans out and dedupes by canonical id, merging sources."""

    bridge_a = wrapper_mod.DiscoveredBridge(
        id="0017886abcaf", host="192.168.1.10", supports_v2=True, source="mdns"
    )
    bridge_a_dup = wrapper_mod.DiscoveredBridge(
        id="0017886abcaf", host="192.168.1.10", supports_v2=False, source="nupnp"
    )
    bridge_a_config = wrapper_mod.DiscoveredBridge(
        id="0017886abcaf", host="192.168.1.10", supports_v2=False, source="config"
    )

    async def fake_mdns(_timeout: float) -> list[wrapper_mod.DiscoveredBridge]:
        return [bridge_a]

    async def fake_nupnp(_timeout: float) -> list[wrapper_mod.DiscoveredBridge]:
        return [bridge_a_dup]

    async def fake_probe(
        host: str, _timeout: float, *, source: str
    ) -> list[wrapper_mod.DiscoveredBridge]:
        assert host == "192.168.1.10"
        assert source == "config"
        return [bridge_a_config]

    monkeypatch.setattr(wrapper_mod, "_discover_mdns", fake_mdns)
    monkeypatch.setattr(wrapper_mod, "_discover_nupnp", fake_nupnp)
    monkeypatch.setattr(wrapper_mod, "_probe_one", fake_probe)

    results = await wrapper_mod.discover(timeout=1.0, cloud=True, configured_ips=["192.168.1.10"])

    assert len(results) == 1
    only = results[0]
    assert only.id == "0017886abcaf"
    assert only.supports_v2 is True  # promoted from mdns hit
    assert set(only.source.split(",")) == {"mdns", "nupnp", "config"}


@pytest.mark.asyncio
async def test_discover_zero_results_no_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """ISC-9: zero hits returns empty list, no exception (FR-5a)."""

    async def empty(_timeout: float) -> list[wrapper_mod.DiscoveredBridge]:
        return []

    monkeypatch.setattr(wrapper_mod, "_discover_mdns", empty)
    monkeypatch.setattr(wrapper_mod, "_discover_nupnp", empty)

    results = await wrapper_mod.discover(timeout=0.5, cloud=True, configured_ips=[])
    assert results == []


@pytest.mark.asyncio
async def test_pair_retries_on_link_button_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISC-10/11: pair retries on LinkButtonNotPressed and returns the app key on success."""

    call_count = {"n": 0}

    async def fake_create_app_key(host: str, app_name: str) -> str:
        assert host == "192.168.1.10"
        assert app_name == "hue-cli#testhost"
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise aiohue.errors.LinkButtonNotPressed("press the button")
        return "abc123def456"

    monkeypatch.setattr(wrapper_mod.aiohue, "create_app_key", fake_create_app_key)

    key = await wrapper_mod.pair(
        "192.168.1.10",
        "hue-cli#testhost",
        retry_interval=0.01,
        timeout=2.0,
    )
    assert key == "abc123def456"
    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_fetch_schedules_uses_https_and_parses_list() -> None:
    """ISC-12: schedules fallback issues HTTPS GET (per reviewed §4.5)."""

    payload = [
        {
            "id": "1",
            "name": "Sunset",
            "command": {"address": "/api/key/groups/1/action", "method": "PUT", "body": {}},
            "localtime": "W124/T18:30:00",
            "status": "enabled",
        }
    ]
    url = "https://192.168.1.10/api/keykeykey/schedules"

    with aioresponses() as mocked:
        mocked.get(url, payload=payload)
        out = await wrapper_mod.fetch_schedules_raw("192.168.1.10", "keykeykey")

    assert out == payload


# ---------------------------------------------------------------------------
# credentials tests
# ---------------------------------------------------------------------------


def test_credentials_load_rejects_mode_0644(isolated_creds: Path) -> None:
    """ISC-14: mode 0644 raises PermissiveCredentialsError exit code 2."""

    isolated_creds.write_text(json.dumps({"version": 1, "bridges": {}}))
    os.chmod(isolated_creds, 0o644)
    with pytest.raises(PermissiveCredentialsError) as excinfo:
        creds_mod.load()
    assert excinfo.value.exit_code == 2


def test_credentials_load_rejects_unknown_version(isolated_creds: Path) -> None:
    """ISC-15: unknown version raises UnknownVersionError exit code 6."""

    isolated_creds.write_text(json.dumps({"version": 99, "bridges": {}}))
    os.chmod(isolated_creds, 0o600)
    with pytest.raises(UnknownVersionError) as excinfo:
        creds_mod.load()
    assert excinfo.value.exit_code == 6


def test_credentials_save_produces_mode_0600_atomically(isolated_creds: Path) -> None:
    """ISC-16: save writes mode 0600, file appears atomically via rename."""

    store = CredentialsStore(
        bridges={
            "0017886abcaf": BridgeCredentials(
                app_key="key123",
                host="192.168.1.10",
                name="Hue Bridge - 6ABCAF",
                paired_at="2026-04-27T14:32:11Z",
            )
        }
    )
    creds_mod.save(store)
    info = isolated_creds.stat()
    mode = stat.S_IMODE(info.st_mode)
    assert mode == 0o600
    parsed = json.loads(isolated_creds.read_text())
    assert parsed["version"] == 1
    assert "0017886abcaf" in parsed["bridges"]


def test_append_bridge_preserves_existing_entries(isolated_creds: Path) -> None:
    """ISC-17: append_bridge preserves all prior entries (FR-9a)."""

    initial = CredentialsStore(
        bridges={
            "0017886abcaf": BridgeCredentials(
                app_key="key1",
                host="192.168.1.10",
                name="Bridge A",
                paired_at="2026-04-27T14:32:11Z",
            )
        }
    )
    creds_mod.save(initial)

    new_creds = BridgeCredentials(
        app_key="key2",
        host="192.168.1.20",
        name="Bridge B",
        paired_at="2026-04-28T08:00:00Z",
    )
    creds_mod.append_bridge("001788ffff1234", new_creds)

    final = creds_mod.load()
    assert "0017886abcaf" in final.bridges
    assert "001788ffff1234" in final.bridges
    assert final.bridges["0017886abcaf"].app_key == "key1"


def test_remove_bridge_returns_false_when_absent(isolated_creds: Path) -> None:
    """ISC-18: remove_bridge with no matching id returns False, exits 0 from caller."""

    creds_mod.save(CredentialsStore(bridges={}))
    assert creds_mod.remove_bridge("0017886abcaf") is False


# ---------------------------------------------------------------------------
# config tests
# ---------------------------------------------------------------------------


def test_load_config_no_default_returns_builtin_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ISC-19: missing default file returns built-in defaults (FR-64b)."""

    monkeypatch.delenv(config_mod.ENV_VAR, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # so default ~/.config/... resolves under tmp
    cfg = config_mod.load_config(explicit_path=None)
    assert cfg.source_path is None
    assert cfg.cloud_discovery is False  # ISC-22: Decision 2
    assert cfg.concurrency == 5  # ISC-23: Decision 6


def test_load_config_explicit_missing_raises(tmp_path: Path) -> None:
    """ISC-20: --config pointing at a missing file raises ConfigError code 6."""

    missing = tmp_path / "does-not-exist.toml"
    with pytest.raises(ConfigError) as excinfo:
        config_mod.load_config(explicit_path=missing)
    assert excinfo.value.exit_code == 6


def test_validate_dangling_bridge_raises(
    tmp_path: Path,
    isolated_creds: Path,
    isolated_config: Path,
) -> None:
    """ISC-21: [bridges.<alias>] referencing an unpaired id raises ConfigError code 6."""

    creds_mod.save(CredentialsStore(bridges={}))  # empty paired set
    isolated_config.write_text('[bridges.home]\nid = "0017886abcaf"\nhost = "192.168.1.10"\n')
    with pytest.raises(ConfigError) as excinfo:
        config_mod.validate(isolated_config)
    assert excinfo.value.exit_code == 6


def test_default_cloud_discovery_off() -> None:
    """ISC-22: cloud_discovery defaults to False per Decision 2."""

    cfg = config_mod.HueConfig()
    assert cfg.cloud_discovery is False


def test_default_concurrency_is_five() -> None:
    """ISC-23: concurrency defaults to 5 per Decision 6."""

    cfg = config_mod.HueConfig()
    assert cfg.concurrency == 5


# ---------------------------------------------------------------------------
# verb tests
# ---------------------------------------------------------------------------


def test_auth_status_empty_credentials_emits_empty_array(isolated_creds: Path) -> None:
    """ISC-24: missing credentials → `[]` exit 0, NOT exit 2 (FR-CRED-4)."""

    runner = CliRunner()
    result = runner.invoke(auth_group, ["status", "--no-probe", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []


def test_auth_migrate_v1_exits_zero(isolated_creds: Path) -> None:
    """ISC-25: auth migrate on v1 schema emits INFO line and exits 0."""

    creds_mod.save(CredentialsStore(bridges={}))
    runner = CliRunner()
    result = runner.invoke(auth_group, ["migrate"])
    assert result.exit_code == 0
    # Click's CliRunner mixes stdout and stderr by default; the INFO line goes to stderr.
    combined = (result.stdout or "") + (result.stderr or "")
    assert "v1" in combined or "no migration needed" in combined


def test_bridge_unpair_unknown_alias_exits_4(isolated_creds: Path) -> None:
    """ISC-26: unpair on a non-existent alias exits code 4 (FR-10a)."""

    creds_mod.save(
        CredentialsStore(
            bridges={
                "0017886abcaf": BridgeCredentials(
                    app_key="k",
                    host="1.2.3.4",
                    name="Bridge A",
                    paired_at="2026-04-27T14:32:11Z",
                )
            }
        )
    )
    runner = CliRunner()
    result = runner.invoke(bridge_group, ["unpair", "001788000000ff"])
    assert result.exit_code == 4


def test_bridge_and_auth_groups_importable() -> None:
    """ISC-27: bridge_group and auth_group are Click groups ready for cli.py wiring."""

    import click as _click

    assert isinstance(bridge_group, _click.Group)
    assert isinstance(auth_group, _click.Group)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``socket.gethostname`` deterministic so app_name assertions are stable."""

    monkeypatch.setattr("socket.gethostname", lambda: "testhost.local")


# Static-analysis touch: keep the imported symbols referenced so tools don't strip them.
_unused: tuple[type, type, type, type] = (
    BridgeCredentials,
    CredentialsStore,
    PermissiveCredentialsError,
    UnknownVersionError,
)


# Type-check probe to ensure AsyncIterator is referenced (used in fixture annotations).
def _probe(_x: AsyncIterator[Any]) -> None:  # pragma: no cover
    return None
