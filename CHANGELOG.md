# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] — 2026-04-28

### Added — Phase 3 (groups, batch, polish) — final SRD §16.3 deliverable

- **`batch` verb (FR-53, FR-54, FR-54a/b/c)** — `--file <path>` and `--stdin` accept newline-delimited
  `hue-cli` invocations (minus the leading `hue-cli` token). `shlex` tokenization handles quoted
  args. Empty input → exit 0. Comments (lines starting with `#`) and blank lines skipped silently.
  `--concurrency N` overrides `[defaults] concurrency = 5`. Each line emits one JSONL result record
  (`line`, `verb`, `target`, `ok`, `duration_ms`, `error`, `result`). Records stream in completion
  order so partial output survives mid-batch SIGINT.
- **§11.1 exit-code collapse (`aggregate_exit_code`)** — empty/all-ok → 0; mixed (≥1 ok + ≥1 fail) → 7;
  all-fail-uniform → that code; multi-mode-fail → 7. Pure function, eight test branches.
- **FR-54c graceful drain on SIGINT/SIGTERM** — async-aware signal handlers via `loop.add_signal_handler`;
  on signal: cease dispatching, give in-flight tasks ≤2 seconds to finish, cancel the rest, emit
  `{"event":"interrupted","completed":N,"pending":M}` JSONL summary line, exit 130/143.
- **`group list` verb (FR-51)** — alias for `list rooms` + `list zones` merged. Bridge-stored groups
  only (LightGroup/Luminaire/Entertainment excluded per Hue v1 conventions).
- **Reentrant wrapper** — `HueWrapper.__aenter__/__aexit__` use a depth counter so a single outer
  `async with wrapper:` in `_run_batch` amortizes one TCP+TLS handshake across the entire batch.
  Inner per-verb `async with` calls become no-ops.

### Fixed during Phase 3 review

- **Dispatch race** — task creation now yields between `create_task` calls so the signal handler
  can fire mid-loop. Previously every task was queued before any `await` boundary.
- **Drain budget enforcement** — replaced unbounded `await asyncio.gather(*tasks)` with a
  `_wait_with_cancel` helper that races task completion against `cancel_event.wait()`. Pending
  tasks are reaped via `gather(*pending, return_exceptions=True)` to suppress aiohttp
  `Unclosed connection` ResourceWarnings.
- **Streaming partial results on cancel** — `_run_batch` accepts an `on_result` callback so JSONL
  records hit stdout as each task completes (not buffered until end). On mid-batch SIGINT, the
  operator sees N completed records + summary line.
- **Mid-flight cancel test** — rewritten to schedule `cancel_after()` via `asyncio.create_task`
  BEFORE awaiting `_run_batch` so the drain path is actually exercised. Slow ops + concurrency=2
  guarantee real mid-flight state. Verified RED→GREEN: 20s → <2.5s after fix.

### Changed

- **JSONL emission order** — batch results stream in completion order, not input order. The SRD
  doesn't mandate input ordering; completion order is the better operator UX (records appear as
  they land) and is required for the streaming-on-cancel contract above.
- **Inline `#` parser behavior documented** — `# comment line` is honored only as the FIRST char
  of a line. `on @kitchen # note` is a parse error (shlex preserves `#` as a token). Trailing
  comments must be on their own line.

## [0.3.0] — 2026-04-28

### Added — Phase 2 (state control, scenes, sensors, file logging)

Per [SRD §16.2](docs/SRD-hue-cli.md).

- **`set` verb (FR-27..38)** — `--brightness 0-100` (with 0 → `on=False`), `--kelvin K`, `--mireds M`,
  `--xy x,y`, `--hex #rrggbb`, `--color <name>`, `--hsv h,s,v`, `--transition ms`,
  `--effect none|colorloop`, `--alert none|select|lselect`. Mutex enforcement per FR-35; capability
  gating per FR-36 (exit 5 on unsupported feature).
- **Color library (`colors.py`)** — D65 sRGB→CIE 1931 conversion, gamut-triangle clamp via
  point-in-triangle + closest-edge fallback, kelvin↔mireds, brightness percent↔raw, 12 named colors
  (`warm-white`, `cool-white` at ~4000K, `daylight` at ~6500K, `red`, `orange`, `yellow`, `green`,
  `cyan`, `blue`, `purple`, `magenta`, `pink`).
- **`scene apply <name|id>` (FR-39..41)** — case-insensitive name resolution; ambiguous → exit 64
  with both candidate ids and group names; `--transition ms` for uniform fade; legacy `LightScene`
  fallback to all-lights group recall when `scene.group` is null.
- **`scene list`, `sensor list`** (FR-42, FR-46) — aliases for the corresponding `list` sub-verbs.
- **`sensor info <name|id>` (FR-47)** — type-specific shaping for motion (presence/lastupdated/battery),
  switch (buttonevent), temperature (centi-Celsius → °C float), light-level (lightlevel/dark/daylight),
  Daylight synthetic (sunrise/sunset), CLIP* virtual sensors (raw passthrough).
- **File logging (§7.3)** — `[logging] file = <path>` tees JSON log lines to disk; `-v` enables INFO,
  `-vv` enables DEBUG. Stderr emission preserved alongside file output.

### Fixed during Phase 2 review

- **FR-31 gamut B fallback** — when `light.controlcapabilities` does not advertise a `colorgamut`,
  the verb now falls back to gamut B `[[0.6750, 0.3220], [0.4090, 0.5180], [0.1670, 0.0400]]`
  with a stderr WARNING per the SRD's required behavior.
- **CT capability gate** — `_supports_ct` now accepts `controlcapabilities = {"ct": {}}` (empty dict),
  matching real-world Hue White (LWA001) firmware that advertises CT support without a published
  range. The bridge remains the authoritative gate.
- **Connection lifecycle** — `scene apply` now wraps `resolve → dispatch` in `async with wrapper:`
  matching the Phase 1 invariant for set/onoff verbs (eliminates per-call TLS handshakes).
- **Cool-white labeling** — retuned to ~4000K (xy ≈ 0.3804, 0.3768) to match operator expectation;
  `daylight` keeps the ~6500K D65 point.
- **Legacy LightScene observability** — wrapper emits `WARNING` on the all-lights fallback path
  so `-v` operators can distinguish modern vs legacy scene recalls.

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
