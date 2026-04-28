# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-04-28

### Added — Phase 1 MVP

Per [SRD §16.1](docs/SRD-hue-cli.md). One-bridge discover/pair/list/info/on/off/toggle.

- **Discovery (FR-1..5b)** — `bridge discover` with parallel mDNS + NUPNP + config-IP probes;
  cloud discovery defaults OFF (Decision 2 strict-local).
- **Pairing (FR-6..10a)** — `bridge pair` button-press flow with `LinkButtonNotPressed` retry,
  app-name length-clamped to 40-char Hue device-type limit. `bridge unpair` is local-only —
  `DELETE /config/whitelist/<key>` was removed by Philips in API 1.31.
- **Credentials store (FR-9, FR-CRED-1..7)** — chmod-0600 atomic JSON writer, canonical 12-char
  bridge-id keys from `aiohue.util.normalize_bridge_id`. `auth status`, `bridge list` alias,
  `auth flush`, `auth migrate` v1 stub.
- **TOML config (FR-64..64c)** — three-tier path resolution, `config show`, `config validate`.
- **Listing (FR-11..18)** — `list lights/rooms/zones/scenes/sensors/schedules/all`,
  `--filter key=value` substring AND-combined.
- **Info (FR-19..21)** — `info <target>` resolves by name/id/`@room`/`@zone`/`bridge`.
- **Power (FR-22..26)** — `on`/`off`/`toggle`. Toggle on groups uses **Decision 4 consolidate-on**
  semantics (`state.all_on == True` → off; otherwise → on).
- **Output (FR-55..57b)** — text/JSON/JSONL/quiet, FR-57b JSON-validity guard, sorted JSON keys.
- **Errors (§11, FR-58..63)** — closed-set `error` enum, structured stderr per §11.2,
  exit codes 0/1/2/3/4/5/6/64/130/143.
- **Schedules fallback (§4.5)** — direct-aiohttp HTTPS GET against `/api/<key>/schedules`
  (aiohue v1 has no schedules controller); Hue v1 dict-of-id collection shape correctly handled;
  `error.type == 1` → `AuthError` (exit 2) per FR-59.

### Fixed during Phase 1 review

- Record materialization now reads `aiohue` model `.raw` for dict-shaped fields
  (Light.state, Group.class, Scene.group, Bridge.config network/zigbee/whitelist).
- Connection lifecycle: compose ops (`resolve_target → light_set_on`,
  `get_all_lights_group → group_set_on`) now share a single bridge connection, eliminating
  `RuntimeError: Session is closed` on real hardware.
- `_open()` no longer leaves `self._bridge` half-initialized when `initialize()` raises.
- `cli.py` credential resolution surfaces `PermissiveCredentialsError` and `ConfigError`
  rather than silently re-routing to "No active bridge wrapper."
- Real-shape regression tests in `tests/test_wrapper_records.py` exercise the materializers
  against fakes built on actual aiohue object shapes.
