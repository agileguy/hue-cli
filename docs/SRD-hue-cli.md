# Software Requirements Document: hue-cli

**Document ID:** SRD-HUE-CLI-001
**Version:** 0.2.0
**Date:** 2026-04-28
**Status:** Reviewed — Ready for Implementation
**Author:** Dan Elliott
**Source:** Derived from operator's verified LAN bridge (BSB002 at 192.168.86.62, API 1.76.0, swversion 1976154040, bridge-id 001788FFFE6ABCAF) + verified `aiohue` 4.8.1 metadata (PyPI/GitHub, released 2026-01-28; requires Python 3.11+) + Philips Hue v1 and v2 API reference at https://developers.meethue.com/

---

## Table of Contents

1. [Overview](#1-overview)
2. [Goals and Non-Goals](#2-goals-and-non-goals)
3. [Background and Prior Art](#3-background-and-prior-art)
4. [Architecture Decision: Wrap vs Reimplement](#4-architecture-decision-wrap-vs-reimplement)
5. [Functional Requirements](#5-functional-requirements)
6. [Authentication and Pairing](#6-authentication-and-pairing)
7. [Non-Functional Requirements](#7-non-functional-requirements)
8. [CLI Surface](#8-cli-surface)
9. [Configuration File](#9-configuration-file)
10. [Data Model](#10-data-model)
11. [Error Model and Exit Codes](#11-error-model-and-exit-codes)
12. [Testing Strategy](#12-testing-strategy)
13. [Distribution and Install](#13-distribution-and-install)
14. [Out of Scope](#14-out-of-scope)
15. [Resolved Decisions](#15-resolved-decisions)
16. [Phase Plan](#16-phase-plan)

---

## 1. Overview

`hue-cli` is a deterministic, scriptable command-line tool for controlling a Philips Hue Bridge and the lights, rooms, zones, scenes, and accessories connected to it. It is not a HomeKit bridge, not a cloud daemon, not an MQTT broker, not a rules engine, and not a GUI dashboard. It is a single binary that takes a verb, a target, and flags, performs one operation against the bridge over the local network, prints a result on stdout, and exits with a meaningful status code. Its job is to be the leaf node in a shell pipeline or cron job alongside its sibling `kasa-cli` — nothing more.

Hue's hub-and-spoke topology (one bridge mediates all device traffic) and button-press pairing model push the design away from `kasa-cli`'s flat-broadcast / cloud-credential pattern in two specific places — bridge discovery + pairing (§5.1, §6) and bridge-stored groups/scenes (§5.3, §5.6). Where Hue's nature requires, this SRD diverges; where it doesn't, it mirrors `kasa-cli` deliberately so the operator's muscle memory carries between the two tools.

---

## 2. Goals and Non-Goals

### 2.1 Goals

- **Discover** Hue bridges on the local network across mDNS, the cloud-discovery NUPNP endpoint, and explicit `--bridge-ip`
- **Pair** with a bridge via the documented button-press flow and persist the returned application key
- **Enumerate** the resources behind a paired bridge — lights, rooms, zones, scenes, sensors, schedules — without making the operator think in Hue's resource taxonomy
- **Control** lights and groups: on, off, toggle, set brightness, set color (xy / hex / Kelvin / named), apply scenes, run effects (`colorloop`, `alert`)
- **Read** sensor and schedule state (read-only — schedule editing belongs in the mobile app)
- **Be scriptable**: deterministic exit codes, JSON/JSONL output, no interactive prompts in non-tty mode (with the exception of `pair`, which is inherently interactive)
- **Resolve targets uniformly** across alias, IP, room name, and zone name — `@kitchen` works whether `kitchen` is a Hue Room or a Hue Zone
- **Run batch operations** across multiple lights or groups in parallel
- **Cache the application key** per bridge so subsequent commands do not re-pair

### 2.2 Non-Goals

- **No GUI.** This is a CLI. Visual dashboards belong elsewhere.
- **No scheduling daemon.** Cron, systemd timers, and launchd handle scheduling. The CLI exposes a verb; the scheduler invokes it.
- **No cloud relay.** Once paired, all traffic is LAN-only. The cloud `discovery.meethue.com` endpoint is consulted **only** as a discovery fallback (§5.1) and can be disabled via config.
- **No automation rules engine.** "If motion sensor fires, turn on hallway" is Home Assistant or Hue's own Rules engine territory, not this tool.
- **No Matter or Thread support.** Different protocol stack entirely.
- **No Tapo, Wiz, or LIFX support.** Different protocols, different vendors.
- **No bridge-side schedule, rule, or resourcelink editing.** Read-only listing of these resources is the v1 ceiling.
- **No scene creation from current state.** Scene authoring is mobile-app territory; the CLI applies pre-saved scenes only.
- **No Entertainment / streaming API.** The dtls-encrypted real-time light streaming protocol is out of scope at all phases.
- **No multi-bridge support in v1.** The config schema does not preclude future multi-bridge but the v1 codepath assumes one bridge at a time. See §6.6.

---

## 3. Background and Prior Art

### 3.1 The two Hue API generations

Both APIs are still served by current bridge firmware (verified on the operator's BSB002 running 1.76.0):

| Property | Hue API v1 (classic) | Hue API v2 (CLIP v2) |
|----------|----------------------|----------------------|
| Introduced | 2014 | 2021 (with second-gen square bridge) |
| URL prefix | `/api/<app-key>/...` | `/clip/v2/resource/...` |
| Auth | App-key in URL path | App-key in `hue-application-key` header |
| Transport | HTTPS REST/JSON | HTTPS REST/JSON + Server-Sent Events |
| Resource model | Flat: `lights`, `groups`, `scenes`, `sensors`, `schedules`, `rules`, `resourcelinks`, `config`, `capabilities` | Layered: `device` → `service`s (one bulb is one Device with one or more Services like `light`, `zigbee_connectivity`, `device_software_update`) |
| Live updates | Poll only | SSE event stream |
| Required for | All v1 use cases | Gradient lights' per-segment colors, dynamic scenes with custom timing, live state events |
| Documentation | https://developers.meethue.com/develop/hue-api/ | https://developers.meethue.com/develop/hue-api-v2/ |

The operator's bridge is BSB002 (square v2 bridge — original round v1/BSB001 is out of scope as he confirmed). Both APIs are available. Roughly 95% of CLI use cases — "turn off the kitchen, set bedroom to warm white at 30%, run the sunset scene" — are fully covered by v1. v2 earns its place when (a) live state monitoring matters or (b) the operator owns gradient/play lights whose multi-segment colors v1 cannot fully address.

### 3.2 aiohue

The canonical async Python library for both Hue APIs is **aiohue** (https://github.com/home-assistant-libs/aiohue), latest stable **4.8.1** (released **2026-01-28** per PyPI), Python 3.11+. Maintained by the Home Assistant team and exercised daily by every HA install with a Hue integration. Verified module layout (master HEAD as of 2026-04-27):

```
aiohue/
├── __init__.py        # exports HueBridgeV1, HueBridgeV2, create_app_key
├── discovery.py       # discover_bridge(host), discover_nupnp(), DiscoveredHueBridge
├── errors.py          # Unauthorized, BridgeBusy, raise_from_error, ...
├── util.py            # create_app_key, normalize_bridge_id
├── v1/
│   ├── __init__.py    # HueBridgeV1
│   ├── config.py      # Config controller
│   ├── groups.py      # Groups, Group, GroupAction
│   ├── lights.py      # Lights, Light, XYPoint, GamutType
│   ├── scenes.py      # Scenes, Scene
│   └── sensors.py     # Sensors + sensor type constants
└── v2/
    ├── __init__.py    # HueBridgeV2
    ├── controllers/   # config, devices, groups, lights, scenes, sensors, ...
    └── models/
```

Real call signatures the wrapper will use (verified against master, not invented):

- `await create_app_key(host: str, app_name: str) -> str` — performs the registration POST
- `discover_bridge(host) -> DiscoveredHueBridge(host, id, supports_v2)` — probes a known IP
- `await discover_nupnp() -> list[DiscoveredHueBridge]` — hits `https://discovery.meethue.com/`
- `HueBridgeV1(host, app_key)` → `await bridge.initialize()` then `bridge.lights`, `bridge.groups`, `bridge.scenes`, `bridge.sensors`, `bridge.config`
- `Light.set_state(on=, bri=, hue=, sat=, xy=, ct=, alert=, effect=, transitiontime=, bri_inc=, sat_inc=, hue_inc=, ct_inc=, xy_inc=)` — aiohue does NOT validate ranges; the wrapper SHALL clamp `bri` to 1–254, treat `xy` as CIE 1931 `(x, y)` floats 0–1, and clamp `ct` to the per-device `controlcapabilities['ct']` range (typical default 153–500 mireds). `bri=0` on the wire is undefined behavior across firmware; the CLI's `--brightness 0` path SHALL emit `on=False` instead and never send `bri=0`.
- `Group.set_action(...same kwargs...+ scene=<scene-id>)` — applying a scene **uses the group `set_action` call with a `scene` kwarg pointing at a scene id**, which targets the group/room to which the scene applies

### 3.3 Known gap: aiohue v1 has no schedules, rules, or resourcelinks

The verified module layout above contains `config`, `groups`, `lights`, `scenes`, `sensors` — and **nothing else**. The Hue v1 API exposes `/api/<key>/schedules`, `/api/<key>/rules`, `/api/<key>/resourcelinks`, and `/api/<key>/capabilities` resources that aiohue v1 simply does not model. This is a known design choice on aiohue's part — the maintainers have steered users toward the v2 API for resources beyond the basic five.

**Implication for hue-cli:** when the CLI needs to read schedules (FR-16, FR-44), the wrapper layer SHALL fall back to issuing a direct `aiohttp` GET against `https://<bridge>/api/<app_key>/schedules` and parse the JSON itself. This is documented as a deliberate design choice in §4.5, not an aiohue bug to be reported. Same applies to `resourcelinks` if the CLI ever needs to enumerate them (currently out of scope).

### 3.4 Other libraries considered

- **`phue`** (https://github.com/studioimaginaire/phue) — synchronous, v1-only, last release in 2017. Rejected on age and sync-blocking grounds. Would force every command to pay an event-loop tax for no benefit.
- **`hue-cli` npm package** (https://www.npmjs.com/package/hue-cli) — JavaScript CLI, last meaningful release ~2018, unmaintained. Not a library option for a Python tool; mentioned only because it shares the name. The Python tool described here will claim the name `hue-cli` in the operator's local install context (`uv tool install`); no PyPI namespace conflict because v1 will not publish to PyPI (per Decision 9 mirror).
- **Home Assistant's Hue integration** — gold standard for in-home automation but the wrong shape for shell scripting. Requires a running HA instance. Mentioned only to delineate scope: `hue-cli` is for users who want shell-native control without standing up Home Assistant.
- **`requests`/`aiohttp` direct against the v1 REST API** — viable but reimplements model classes, throttling, and error-response parsing that aiohue already provides. Rejected for the same reason `kasa-cli` wraps `python-kasa` rather than reimplementing the Smart Home Protocol.

### 3.5 Why a thin custom CLI is justified

aiohue is a library, not a CLI. The combination of (a) bridge alias resolution, (b) `@room` / `@zone` target syntax over Hue's bridge-stored groups, (c) consistent JSON/JSONL output across all verbs, (d) parallelized batch operations with structured failure reporting, (e) persistent app-key caching with chmod 0600 enforcement, and (f) named-color / Kelvin / brightness-percent translation makes a wrapper materially more useful for shell scripting than direct aiohue use from a one-off Python script. The wrapper does not duplicate protocol work; it adds a config-and-output layer on top of a maintained protocol library.

---

## 4. Architecture Decision: Wrap vs Reimplement

### 4.1 Decision

**Wrap aiohue 4.8.1, target the v1 API for the v1 CLI, defer v2 streaming to a later phase.**

### 4.2 Rationale — wrap vs reimplement

| Factor | Wrap aiohue | Reimplement protocol |
|--------|-------------|----------------------|
| v1 REST request/response shaping | Free | ~300 lines + per-firmware variance |
| v2 CLIP request/response shaping | Free (when needed) | ~500 lines + SSE plumbing |
| Light/Group/Scene/Sensor model classes | Free | Tedious, low-value duplication |
| Throttling (Hue bridges rate-limit at 10 req/sec for lights, 1/sec for groups) | Free — aiohue ships an `asyncio_throttle.Throttler` configured to 1 concurrent request per 250 ms | ~50 lines + tuning |
| Bridge auto-retry on 503/429 | Free — aiohue retries up to 25 times on bridge overload | ~30 lines + tuning |
| Bridge id normalization (`001788FFFE6ABCAF` → `0017886abcaf` — 16-char NUPNP form collapses to 12-char canonical by stripping the `fffe` middle) | Free via `aiohue.util.normalize_bridge_id` | Trivial but easy to forget |
| Error mapping (bridge JSON-array errors → Python exceptions) | Free — `raise_from_error` with `Unauthorized`, `BridgeBusy`, etc. | ~40 lines |
| Long-term maintenance burden | Track minor version bumps | Own a protocol stack forever |
| v1 ship time | Days | Weeks |

### 4.3 Rationale — v1 vs v2 API for the v1 CLI

| Use case | v1 sufficient? | Notes |
|----------|----------------|-------|
| Turn light on/off | Yes | `PUT /lights/<id>/state` |
| Set brightness, ct, xy, hsv | Yes | Same endpoint |
| Apply a scene to a room | Yes | `PUT /groups/<id>/action` with `{"scene":"<scene-id>"}` |
| Run `colorloop` or `alert: select` | Yes | Same endpoint |
| List rooms / zones | Yes | `GET /groups`, distinguished by `type: "Room"` vs `type: "Zone"` |
| List sensors | Yes | `GET /sensors` |
| Read motion sensor state | Yes | `GET /sensors/<id>` (poll) |
| **Live event stream** (motion fired, button pressed, light state changed externally) | **No** | v2 SSE only |
| **Gradient strip per-segment colors** | **No** | v2 only |
| **Dynamic scenes with custom timing** | **No** | v2 only |

Phase 1–3 of the CLI cover everything in the "Yes" rows. Phase 4 (no commitment) holds the v2 SSE-streaming scope if the operator ever wants `hue-cli watch --events` style live monitoring.

### 4.4 Implementation language

**Recommendation: Python with `uv` for dependency and tool management.** Reasoning mirrors `kasa-cli` §4.3:

- aiohue is a Python library; using it from Python is idiomatic and avoids a process-boundary tax on every command
- `uv tool install hue-cli` gives single-command global install with isolated venv
- Per-command latency is dominated by network round-trips to the bridge, not language startup
- Operator already has `kasa-cli` in the same Python+uv shape; matching reduces cognitive load

### 4.5 Wrapper handles aiohue's gaps via direct HTTP

For Hue v1 resources aiohue does not model (`schedules`, `rules`, `resourcelinks`, `capabilities`), the wrapper SHALL fall back to direct `aiohttp` GET against `https://<bridge>/api/<app_key>/<resource>` and parse the JSON in the wrapper. Modern bridge firmware (Signify RED-compliance posture, observed on bridges shipping ≥2024) presents a self-signed Signify certificate; the wrapper SHALL configure `aiohttp` with the same TLS context aiohue itself uses (its `ClientSession` accepts the bridge's self-signed cert). Plain HTTP MAY work on older firmware but the SRD does not depend on it. This is a deliberate design choice, documented here so the operator does not discover it later via a stack trace. The fallback path SHALL share the throttler instance with aiohue's own throttler (one concurrent request per 250 ms across both code paths) to respect bridge rate limits.

### 4.6 Considered alternative: Bun-TS shell-out wrapper

Same rejection as `kasa-cli` §4.4: each invocation pays both Python startup AND a process-spawn round-trip; app-key caching across invocations becomes harder; two languages to maintain for a single tool. If stack-uniformity becomes a requirement later, the same CLI surface can be re-fronted in Bun-TS. That is a Phase 5+ concern, not v1.

---

## 5. Functional Requirements

Each FR is atomic and independently testable.

### 5.1 Bridge Discovery

- **FR-1:** `hue-cli bridge discover` SHALL probe for bridges via three independent paths in parallel and aggregate results: (a) **mDNS** browse for service type `_hue._tcp` over the default interface, (b) **NUPNP cloud discovery** via `await discover_nupnp()` against `https://discovery.meethue.com/`, (c) any **bridge IPs configured** in `[bridges.<alias>]` blocks.
- **FR-2:** Discovery output SHALL include, per bridge: `id` (the **12-char canonical lowercase** Bridge ID from `aiohue.util.normalize_bridge_id`, e.g., `0017886abcaf`; the 16-char NUPNP-form `001788FFFE6ABCAF` collapses to this by dropping the `fffe` middle and lowercasing), `host` (IP), `supports_v2` (bool from `aiohue.discovery.DiscoveredHueBridge.supports_v2`), and `source` (`mdns`, `nupnp`, or `config`). All Bridge ID emission across the CLI (FR-2 discovery, `auth status` in `--json` mode, credentials-file keys per FR-9, structured-error `target` fields) SHALL use the canonical 12-char form. Human-readable text-mode columns MAY display the wire 16-char form for legibility; JSON output SHALL NOT.
- **FR-3:** Discovery SHALL complete within `--timeout` seconds (default 5s) and SHALL deduplicate bridges that appear via more than one source by Bridge ID. The `source` field on a deduplicated result SHALL be a comma-separated string of all sources (e.g., `"mdns,config"`).
- **FR-4:** Cloud discovery (NUPNP via `https://discovery.meethue.com/`) SHALL default to **disabled** per Decision 2 — the operator opted for a strict-local discovery posture. Default in config: `[defaults] cloud_discovery = false`. To enable for one invocation: `--cloud`. To enable persistently: set `cloud_discovery = true` in config. When disabled, `bridge discover` runs mDNS + manual-IP only and SHALL NOT make any outbound HTTPS request to `discovery.meethue.com`.
- **FR-5:** `hue-cli bridge discover --bridge-ip <ip>` SHALL skip mDNS and NUPNP and probe only the given IP via `await discover_bridge(host)`. Exit code 4 if the IP does not respond as a Hue bridge within `--timeout`.
- **FR-5a:** Discovery completing with **zero responding bridges** SHALL exit 0 with empty output (`[]` in `--json`/`--jsonl`; empty stdout in text mode) and emit a single INFO log line to stderr stating "no bridges found." Exit code 3 (network error) SHALL be reserved for cases where the discovery itself failed (no usable interface, DNS failure for `discovery.meethue.com` when cloud discovery was requested).
- **FR-5b:** On macOS, mDNS browsing on multi-NIC hosts (Wi-Fi + Tailscale + Docker bridges) MAY return bridges from non-default interfaces or miss bridges on a non-default interface. The CLI SHALL document this in `--help` for `bridge discover` and recommend `--bridge-ip <ip>` on multi-NIC hosts when the bridge is known.

### 5.2 Bridge Pairing

- **FR-6:** `hue-cli bridge pair` SHALL initiate the documented Hue button-press flow: (a) discover or accept `--bridge-ip <ip>`, (b) prompt the operator on stderr to "Press the link button on the Hue Bridge within 30 seconds, then press Enter", (c) call `await create_app_key(host, app_name)` where `app_name` defaults to `"hue-cli#<short-hostname>"` (e.g., `"hue-cli#dans-mbp"`), (d) on success persist the returned app-key per FR-9 and print a success line to stdout; on `LinkButtonNotPressed` (`{"error":{"type":101,...}}` from the bridge) wait `--retry-interval` seconds (default 2s) up to `--timeout` (default 30s) and retry.
- **FR-7:** `pair` is the **only** verb that prompts for stdin in v1. All other verbs SHALL operate non-interactively (per §7.2).
- **FR-8:** `pair --non-interactive` SHALL skip the "press Enter" prompt and immediately enter the link-button polling loop. Useful when an operator has just pressed the button and wants the call to start polling without a confirmation gesture (e.g., scripted setup flows).
- **FR-9:** On successful pairing, the CLI SHALL store the credentials at `~/.config/hue-cli/credentials` (chmod 0600 enforced — created mode 0600 if new, refused if existing mode is more permissive) as JSON `{"version": 1, "bridges": {"<bridge-id>": {"app_key": "<up-to-40-char alphanumeric>", "host": "<ip>", "name": "<bridge-config-name>", "paired_at": "<iso8601>"}}}`. The `<bridge-id>` key uses the **12-char canonical lowercase** form returned by `aiohue.util.normalize_bridge_id` (e.g., `0017886abcaf`, not the wire form `001788FFFE6ABCAF`). The `app_key` is whatever the bridge returns from `create_app_key` — historically up to 40 alphanumeric characters; the CLI SHALL NOT enforce a fixed length on read.
- **FR-9a:** Pairing a second bridge SHALL append to the `bridges` dict; existing entries are not disturbed. This is the foundation for future multi-bridge support without forcing a credential-file migration later. v1 still operates against one bridge at a time per §6.6.
- **FR-10:** `hue-cli bridge unpair <alias|bridge-id>` SHALL remove the named bridge's entry from credentials. The CLI SHALL NOT attempt to delete the user/whitelist entry from the bridge itself — Philips removed the local-API `DELETE /config/whitelist/<key>` endpoint in API 1.31 (Hue Whitelist Security Update), and modern bridges (the operator's BSB002 at API 1.76.0 included) only accept whitelist removal via the Hue mobile app or `https://account.meethue.com/apps`. `unpair` is a local-only operation in v1 and forever.
- **FR-10a:** `unpair` against an unknown alias/id SHALL exit 4. `unpair` with no argument and exactly one bridge in credentials SHALL prompt for confirmation in tty mode (`y/n`) and refuse in non-tty mode (exit 64).

### 5.3 Listing

The listing surface is shaped by Hue's hub-and-spoke topology — every list verb takes a bridge as its implicit subject (resolved via the credential file or `--bridge`).

- **FR-11:** `hue-cli list lights` SHALL print every light known to the bridge with `id`, `name`, `model_id` (e.g., `LCT015`), `product_name` (when present — added in bridge API 1.24, may be `null` on older lights), `type` (e.g., `Extended color light`, `Color temperature light`, `Dimmable light`), `state.on`, `state.reachable`, and `state.brightness_percent` (translated from raw 1-254).
- **FR-12:** `hue-cli list rooms` SHALL print every group of `type: "Room"` with `id`, `name`, `class` (e.g., `Kitchen`, `Bedroom` — Hue's room category), `light_ids`, `state.any_on`, `state.all_on`.
- **FR-13:** `hue-cli list zones` SHALL print every group of `type: "Zone"` with the same shape as rooms (Hue Zones are user-defined cross-room groupings).
- **FR-14:** `hue-cli list scenes` SHALL print every scene with `id`, `name`, `group_id` (the room/zone the scene targets), `light_ids`, `last_updated`. Most modern bridges produce only `GroupScene` entries (group-scoped, created via the Hue mobile app post-2017), but legacy `LightScene` entries (no group, lights array only) MAY exist on older firmware — for those, `group_id` SHALL be `null` and the apply path falls back per FR-39. Scenes whose group has been deleted SHALL also appear with `group_id: null` and a `stale: true` flag.
- **FR-15:** `hue-cli list sensors` SHALL print every sensor with `id`, `name`, `type` (e.g., `ZLLPresence`, `ZLLTemperature`, `ZLLLightLevel`, `ZGPSwitch`, `ZLLSwitch`, `Daylight`), `model_id`, `state` (the type-specific state object — `presence` for motion sensors, `temperature` for temp sensors, `buttonevent` for switches, etc.), and `config.battery` when present. The full set of `type` values aiohue exposes is enumerated in `aiohue/v1/sensors.py` (TYPE_* constants).
- **FR-16:** `hue-cli list schedules` SHALL print every schedule on the bridge via the wrapper's direct-aiohttp fallback (§4.5). Output fields match the §10.7 Schedule data model: `id`, `name`, `description`, `command` (verbatim object the schedule fires), `localtime` (iCal-like spec — bridge's wire field name, used directly), `status` (`enabled` / `disabled`), `autodelete`, `created` (ISO8601), `starttime` (ISO8601, optional).
- **FR-17:** `hue-cli list all` SHALL emit a single object containing all six listings keyed by category. Useful for `jq`-based bridge state snapshots.
- **FR-18:** All list verbs SHALL accept `--filter <key=value>` for simple substring/equality filtering on the top-level fields. Multiple filters are AND-combined. The filter grammar is intentionally minimal — for richer queries, pipe to `jq`.

### 5.4 Info

- **FR-19:** `hue-cli info <target>` SHALL accept any of: a light name/alias, a light id, an IP (resolves to bridge config), `@room-name`, `@zone-name`, scene name. The CLI SHALL infer target type by config lookup → bridge resource lookup, and emit the full record for that resource type.
- **FR-20:** Info output in `--json` mode SHALL be a single JSON object whose shape matches the corresponding §10 record exactly. Stable key names across firmware versions.
- **FR-21:** `hue-cli info bridge` SHALL emit bridge-level info via `bridge.config` (aiohue's Config controller). Field names match §10.1's Bridge data model: `id` (canonical 12-char), `name`, `host` (IP), `mac`, `model_id` (e.g., `BSB002`), `api_version`, `swversion`, `supports_v2`, `paired_at`, `reachable`, plus the bridge-network extras `gateway`, `netmask`, `timezone`, `zigbee_channel`, and `whitelist` (the list of paired apps with each entry's `last_use_date` and `create_date` — useful for spotting stale apps to prune via the Hue app).

### 5.5 Power Control

- **FR-22:** `hue-cli on <target>` SHALL turn the target on. For a light target, this calls `Light.set_state(on=True)`; for a room/zone, `Group.set_action(on=True)`; for `all`, the special "all lights" group via `Groups.get_all_lights_group()` then `set_action(on=True)`.
- **FR-23:** `hue-cli off <target>` SHALL turn the target off via the same dispatch.
- **FR-24:** `hue-cli toggle <target>` SHALL flip the on/off state. For a single light, this requires reading `state.on` first then writing the negation. For a group, the CLI SHALL apply the **consolidate-on** rule (Decision 4): if `state.all_on == true` (every light in the group is currently on) turn them all off; otherwise turn them all on. This deviates from the Hue mobile app's `any_on` toggle semantics on purpose — the operator wanted toggle to converge groups toward an "all on" state unless they're already fully on.
- **FR-25:** Power verbs SHALL be idempotent — calling `on` on an already-on target SHALL exit 0 without error.
- **FR-26:** Unreachable lights (`state.reachable: false`) SHALL emit a warning to stderr but the command still SHALL exit 0 if the bridge accepted the call. The bridge queues commands for unreachable lights; whether they apply when the light returns is the bridge's problem, not the CLI's.

### 5.6 Brightness, Color, Color-Temperature, Effects

User-facing units (operator types these) and wire units (sent to bridge) differ — see §10.6. The CLI converts.

- **FR-27:** `hue-cli set <target> --brightness <0-100>` SHALL set brightness, translating 0-100 to Hue's 1-254 range (linear, with 0 → off and 100 → 254). Brightness 0 SHALL imply `on=False` to match user expectation that "0% brightness" means "off."
- **FR-28:** `hue-cli set <target> --kelvin <K>` SHALL set color temperature, translating Kelvin to mireds via `mireds = round(1_000_000 / K)` and clamping to the device-supported range advertised in `light.controlcapabilities['ct']` (a dict with `min`/`max` mireds keys — aiohue exposes `controlcapabilities` as a dict pass-through of the bridge's `capabilities.control` JSON object, NOT as a dotted attribute). Default-typical range is `153 mireds (≈6500K cool) → 500 mireds (2000K warm)` but the CLI SHALL honor whatever the device reports.
- **FR-29:** `hue-cli set <target> --mireds <M>` SHALL set color temperature in mireds directly, no conversion. Provided for operators who already think in mireds (Hue's native unit).
- **FR-30:** `hue-cli set <target> --xy <x,y>` SHALL set CIE 1931 chromaticity coordinates directly via `set_state(xy=(x, y))`. Out-of-gamut values SHALL NOT be auto-corrected — the bridge clamps to gamut and reports back the actual stored value, which the next `info` will reflect.
- **FR-31:** `hue-cli set <target> --hex <#rrggbb>` SHALL accept hex color, convert sRGB → CIE XY using D65 illuminant and the device's reported `colorgamut` triangle (via `light.colorgamut` from aiohue), and call `set_state(xy=...)`. Unknown gamut SHALL fall back to gamut B (the most common 2014-era gamut) with a warning on stderr.
- **FR-32:** `hue-cli set <target> --color <name>` SHALL accept a named color from a built-in name→xy table. Minimum names: `warm-white` (≈2700K, xy from D65 chromaticity tables), `cool-white` (≈6500K), `daylight` (≈5000K), `red`, `orange`, `yellow`, `green`, `cyan`, `blue`, `purple`, `magenta`, `pink`. Unknown names SHALL exit code 64 with the supported list. Built-in (not config) for cross-machine consistency, mirroring `kasa-cli` FR-19a/19b.
- **FR-33:** `hue-cli set <target> --hsv <h,s,v>` SHALL accept HSV (hue 0-360, saturation 0-100, value 0-100), convert to RGB then to xy via the same gamut path as FR-31.
- **FR-34:** `hue-cli set <target> --transition <ms>` SHALL set the fade duration. Hue's wire format is deciseconds (1 ds = 100 ms); the CLI accepts ms and divides. Default is the bridge default (4 ds = 400 ms) when omitted.
- **FR-35:** `--brightness`, `--kelvin`, `--mireds`, `--xy`, `--hex`, `--color`, `--hsv` MAY be combined where physically meaningful (e.g., `--brightness 30 --color red` is valid; `--kelvin 2700 --color red` is mutually exclusive). Color-spec flags (`--kelvin`/`--mireds` form one group; `--xy`/`--hex`/`--color`/`--hsv` form another) are mutually exclusive **within their group** — exit code 64 on overlap with the offending flags listed.
- **FR-36:** Setting color on a non-color-capable light (e.g., a tunable-white-only bulb) SHALL exit code 5 (unsupported). Setting Kelvin on a fixed-color bulb SHALL exit code 5. Capability check SHALL use `light.controlcapabilities` (aiohue exposes this as a property).
- **FR-37:** `hue-cli set <target> --effect <effect>` SHALL set effects. Supported effect values in v1: `none`, `colorloop` (cycles hues continuously). `--effect colorloop` SHALL also imply `on=True`. The effect persists until cleared with `--effect none` or another `set` invocation overrides it.
- **FR-38:** `hue-cli set <target> --alert <alert>` SHALL set alert effects: `none`, `select` (one breathing-cycle flash, ~1 second), `lselect` (15 seconds of flashing). This is the Hue v1 `alert` field and is the canonical "find my light" feature.

### 5.7 Scenes

- **FR-39:** `hue-cli scene apply <scene-name|scene-id>` SHALL apply a saved scene. Resolution: scene-id (alphanumeric, bridge-assigned — see §10.4) → direct apply; scene-name → look up against `bridge.scenes`. For modern `GroupScene` entries (which dominate post-2017 firmware), use the scene's `group` field then call `bridge.groups[<group-id>].set_action(scene=<scene-id>)`. For legacy `LightScene` entries with no group (rare but possible on older bridges), fall back to `Group 0` (all-lights) recall: `bridge.groups.get_all_lights_group()` then `set_action(scene=<scene-id>)`. The bridge filters the all-lights recall to only the scene's `lights` array, so the practical effect matches the user expectation.
- **FR-40:** Scene-name resolution SHALL be case-insensitive and SHALL exit 64 on ambiguity (multiple scenes with the same name) listing the candidate scene-ids and their group names. Operators disambiguate by passing the id.
- **FR-41:** `hue-cli scene apply <scene> --transition <ms>` SHALL pass `transitiontime` (deciseconds) to `set_action`. Some scenes embed their own transition timing on a per-light basis — passing `transitiontime` overrides this with a uniform fade.
- **FR-42:** `hue-cli scene list` SHALL be an alias for `hue-cli list scenes` (FR-14). The duplication exists because operators reach for `scene list` muscle-memory-wise alongside `scene apply`.
- **FR-43:** Scene CREATION (capturing current room state into a new scene) is **deferred to a future phase**. Hue users typically author scenes in the mobile app; CLI-driven scene creation is a stretch goal and not committed in v1. The wrapper layer's design SHALL NOT preclude it (the v1 endpoint `POST /api/<key>/scenes` is documented), but no verb is shipped in v1.

### 5.8 Schedules (Read-Only)

- **FR-44:** `hue-cli schedule list` SHALL be an alias for `hue-cli list schedules` (FR-16). Read-only.
- **FR-45:** v1 SHALL NOT support creating, editing, enabling, disabling, or deleting bridge-side schedules. Per the Hue ecosystem norm, schedule editing is the mobile app's job (or Home Assistant's). Read-only forever, mirroring `kasa-cli` Decision 3.

### 5.9 Sensors (Read-Only)

- **FR-46:** `hue-cli sensor list` SHALL be an alias for `hue-cli list sensors` (FR-15).
- **FR-47:** `hue-cli sensor info <sensor-name|id>` SHALL emit a single sensor's full state — type-specific. For motion sensors: `presence`, `lastupdated` (ISO8601), `battery`, `reachable`. For switches: `buttonevent` (last button code), `lastupdated`. For temperature sensors: `temperature` (reported in centi-Celsius by Hue, the CLI SHALL convert to °C as a float). For light-level sensors: `lightlevel` (raw uint), `dark`, `daylight`. For the synthetic Daylight sensor: `daylight` bool plus the sunrise/sunset config.
- **FR-48:** Live sensor event watching (motion fired, button pressed) is **out of scope in v1** — that is the v2 SSE API's territory and is held for a possible Phase 4. Operators who need real-time sensor reactions SHALL use Hue's own Rules engine (configured in the mobile app) or Home Assistant.

### 5.10 Groups (Operations Across Lights)

Hue's "group" is bridge-stored — it's a Room or a Zone. The CLI does not maintain operator-config groups (this is a deliberate divergence from `kasa-cli` — see Architectural Note below).

- **FR-49:** A group target (`@<room-or-zone-name>`) SHALL resolve at command execution time by looking up `bridge.groups` for `type in ("Room", "Zone")` and matching `name` case-insensitively.
- **FR-50:** Ambiguous resolution (e.g., a Room and a Zone both named `Office`) SHALL exit 64 with both `ids` listed. Operator disambiguates with `@room:Office` or `@zone:Office` syntax.
- **FR-51:** `hue-cli group list` SHALL be an alias for printing both rooms and zones (`list rooms` + `list zones` merged).
- **FR-52:** Group power and set operations SHALL prefer Hue's group-action endpoint (`Group.set_action(...)`) over fanning out to per-light calls — it's atomic, faster, and avoids the per-light rate limit.

**Architectural Note — divergence from `kasa-cli`:** `kasa-cli` defines groups as local config (`[groups]` in TOML) because Kasa devices have no concept of groups. Hue does — Rooms and Zones are bridge-stored, mobile-app-managed, and richer than a flat alias list. Mirroring `kasa-cli`'s pattern would force the operator to maintain a *parallel* group list in `~/.config/hue-cli/config.toml` that drifts from the bridge's truth. **The CLI reads groups from the bridge and exposes them via `@name` syntax. Local-config groups are not supported in v1.** If the operator wants a logical grouping that isn't a Room or Zone (e.g., "outdoor lights" spanning two zones), the workaround is to create a Hue Zone in the mobile app — this is what Zones are for.

### 5.11 Batch

- **FR-53:** `hue-cli batch --file <path>` SHALL read newline-delimited commands from a file and execute them, emitting one JSONL result per line on stdout. Each line is parsed as a `hue-cli` invocation minus the leading `hue-cli` token (e.g., `set kitchen --brightness 30`).
- **FR-54:** `hue-cli batch --stdin` SHALL accept the same format from stdin for shell-pipe composability.
- **FR-54a:** Batch exit code semantics SHALL match FR-58 (0 / 7 / first-failure-code).
- **FR-54b:** Empty-input batch SHALL exit 0 with no stdout output (`[]` in `--json` mode). Blank lines SHALL be skipped silently. Lines beginning with `#` SHALL be treated as comments and skipped.
- **FR-54c:** On SIGINT or SIGTERM during batch execution, the CLI SHALL: (1) cease dispatching new sub-operations, (2) wait up to 2 seconds for in-flight calls to complete, (3) emit a final JSONL summary line `{"event":"interrupted","completed":N,"pending":M}` to stdout, (4) exit with code 130 (SIGINT) or 143 (SIGTERM). Mirrors `kasa-cli` FR-31c.

### 5.12 Output Formats

Verbatim from `kasa-cli`:

- **FR-55:** Default output SHALL be human-readable text on a tty, JSONL when stdout is a pipe.
- **FR-56:** `--json` SHALL force pretty JSON output regardless of tty detection.
- **FR-57:** `--jsonl` SHALL force one-JSON-per-line output regardless of tty detection.
- **FR-57a:** `--quiet` SHALL suppress all stdout output; only the exit code communicates result.
- **FR-57b:** In `--json` and `--jsonl` modes, on **any** non-zero exit, stdout SHALL be valid parseable JSON or empty. The CLI SHALL never emit malformed JSON. For batch operations with mixed results, stdout JSONL SHALL contain one result object per attempted operation including those that failed (each with its own `error` field per §11.2). Stderr SHALL emit the structured summary error per §11.2 once.

### 5.13 Error Handling

- **FR-58:** Network errors (bridge unreachable, TCP reset, timeout) SHALL exit code 3 with a structured stderr error.
- **FR-59:** Authentication failures (no app-key for the target bridge, app-key revoked from bridge whitelist, `Unauthorized` from aiohue) SHALL exit code 2 with a hint to run `hue-cli bridge pair`.
- **FR-60:** Unknown alias / unresolved target SHALL exit code 4.
- **FR-61:** Capability mismatch (color on a non-color light, schedule edit on read-only path) SHALL exit code 5.
- **FR-62:** `BridgeBusy` (rate-limit exhaustion after aiohue's 25-retry budget) SHALL exit code 3 — operationally the same shape as a network error from the operator's POV. The structured stderr error SHALL distinguish via `error: "bridge_busy"`.
- **FR-63:** Verbose mode (`-v`, `-vv`) SHALL emit progressively detailed JSON-structured logs to stderr; stdout SHALL remain clean.

### 5.14 Configuration Resolution

- **FR-64:** Config file resolution order: (1) `--config <path>` flag if present, (2) `HUE_CLI_CONFIG` env var if set and non-empty, (3) `~/.config/hue-cli/config.toml` if it exists.
- **FR-64a:** If `--config` or `HUE_CLI_CONFIG` is set and the referenced file does not exist or cannot be read, the CLI SHALL exit code 6 (config error). Silent fallback is forbidden.
- **FR-64b:** If only the default location is consulted and it does not exist, the CLI SHALL operate with built-in defaults and emit a single INFO log line on stderr. This SHALL NOT be an error.
- **FR-64c:** `hue-cli config show` SHALL print the effective resolved config (after all overrides) in TOML format. `hue-cli config validate [<path>]` SHALL load and validate a config file and exit 0 / 6.

---

## 6. Authentication and Pairing

### 6.1 The button-press model

Hue authentication is **not** a username/password pair like Kasa's TP-Link cloud creds. The bridge generates an app-key — historically up to 40 alphanumeric characters, also called a "username" in the v1 docs (same value, different name) — when the user physically presses the link button on the bridge top **within ~30 seconds** of the registration POST. This is the entire trust ceremony. The app-key does not expire on its own. The bridge keeps a whitelist of issued keys, and the user can revoke entries via the Hue mobile app or `https://account.meethue.com/apps`. Philips removed the local-API `DELETE /api/<key>/config/whitelist/<key>` endpoint in API 1.31 (Hue Whitelist Security Update); modern bridges no longer accept whitelist deletion via local REST. The CLI's `unpair` is therefore strictly local-only.

The CLI's job is to (a) make the button-press dance scriptable enough that an operator can pair without reading docs, and (b) persist the resulting app-key with sane file permissions so subsequent commands skip the dance.

### 6.2 The pair flow (FR-6)

1. Operator runs `hue-cli bridge pair` (optionally `--bridge-ip <ip>` to skip discovery).
2. CLI discovers or accepts the bridge IP, prints a confirmation to stderr ("Found Hue Bridge - 6ABCAF at 192.168.86.62"), and prompts: "Press the link button on the Hue Bridge within 30 seconds, then press Enter."
3. Operator presses the bridge button (the round button on top of BSB002 — solid blue when pressed and accepting registrations, briefly).
4. Operator presses Enter (or skip with `--non-interactive`).
5. CLI calls `await create_app_key(host, "hue-cli#<short-hostname>")` from `aiohue`. If the bridge returns the `link button not pressed` error (Hue v1 error code 101), the CLI retries every 2 seconds until `--timeout` (default 30s).
6. On success the CLI receives an alphanumeric app-key string (historically up to 40 chars; the CLI does not enforce length) and persists it per FR-9.
7. On exhaust the CLI exits code 2 with a hint to press the button before re-running.

### 6.3 Credentials file format

Single JSON file, `~/.config/hue-cli/credentials`, chmod 0600.

```json
{
  "version": 1,
  "bridges": {
    "0017886abcaf": {
      "app_key": "<up-to-40-char alphanumeric>",
      "host": "192.168.86.62",
      "name": "Hue Bridge - 6ABCAF",
      "paired_at": "2026-04-27T14:32:11Z"
    }
  }
}
```

- **FR-CRED-1:** Top-level `version` integer SHALL be present (currently `1`). Unknown additional keys SHALL cause a config-validation error and exit 6. A missing `version` field SHALL be treated as v1 with a single deprecation warning on stderr; subsequent versions SHALL ship with `hue-cli auth migrate`.
- **FR-CRED-2:** The CLI SHALL refuse to load a credentials file whose mode is more permissive than 0600 (group or world readable/writable) and SHALL exit code 2 with the current mode in the message.
- **FR-CRED-3:** A missing credentials file SHALL be treated as "no bridges paired." Verbs that require a paired bridge (`list`, `info`, `on`, `off`, `set`, `scene`, `batch`) SHALL exit code 2 with a hint to run `hue-cli bridge pair`. Verbs that do NOT require credentials SHALL operate normally on a missing file: `bridge discover`, `bridge pair`, `bridge unpair` (no-op exit 0), `bridge list` (empty), `auth status` (empty array), `auth flush` (no-op exit 0), `config show`, `config validate`, `--help`, `--version`.

### 6.4 No app-key TTL or session caching

Unlike Kasa's KLAP, Hue app-keys do not expire on their own and there is no session-key derivation step per command. Every v1 API call attaches the app-key as URL path. There is therefore **no token cache** to maintain — the credentials file IS the cache. This is materially simpler than `kasa-cli`'s §6.4 KLAP machinery, and the SRD does not need a session-state directory, expiration math, or retry-on-auth-failure flow beyond the obvious "if `Unauthorized` comes back, the key was revoked — exit 2 with a hint to re-pair."

### 6.5 `auth status` and `auth flush`

- **FR-CRED-4:** `hue-cli auth status` SHALL emit, per paired bridge: `id` (canonical 12-char), `name`, `host`, `paired_at`, and a live `reachable: bool` flag from a one-shot `await discover_bridge(host)` probe (skipped with `--no-probe`). `--json` mode emits a JSON array. An empty credentials file (or absent file) SHALL emit `[]` and exit 0 — not an error.
- **FR-CRED-4a:** `hue-cli bridge list` SHALL be an alias for `hue-cli auth status` — same fields, same exit semantics. Exists for surface symmetry alongside `bridge discover` / `bridge pair` / `bridge unpair`.
- **FR-CRED-5:** `hue-cli auth flush` SHALL clear the credentials file (preserving file mode 0600). `hue-cli auth flush --bridge <alias|id>` SHALL remove only that bridge's entry, leaving others intact. This does NOT revoke the app-key on the bridge — Philips removed the local-API whitelist deletion endpoint in API 1.31 (see §6.1 and FR-10), so full revocation requires the Hue mobile app or `https://account.meethue.com/apps`. v1 emits an INFO line on `flush` reminding the operator of this when stderr is a tty.
- **FR-CRED-6:** Re-pairing from the same machine when the bridge already has a whitelist entry with the same `app_name` SHALL **proceed silently** per Decision 12. The bridge accepts duplicate `app_name` entries and issues a fresh app-key; the credentials file is updated to the new key. The CLI SHALL NOT prompt or warn — the operator accepted whitelist accumulation as the tradeoff for a simpler re-pair UX. Operators who want to clean up old entries SHALL prune them via the Hue mobile app (no local-API path exists, per §6.1).
- **FR-CRED-7:** `hue-cli auth migrate` is a forward-looking verb reserved for future credentials-schema migrations (e.g., when `version` advances past `1`). In v1 it SHALL detect the file is already at the current schema version, emit a single INFO line on stderr (`"credentials at v1, no migration needed"`), and exit 0. The verb exists in v1 so future versions can perform real migrations under a stable verb name without breaking shell muscle memory.

### 6.6 Multi-bridge: deferred but not foreclosed

Multi-bridge homes are real (large houses with two or three bridges to extend Zigbee mesh range, small businesses with one bridge per floor) but uncommon in the operator's likely use cases. v1 SHALL operate against one bridge at a time:

- The credentials file already supports multiple `bridges` entries (FR-9a) — the schema does not need to change for multi-bridge.
- The config file `[bridges.<alias>]` blocks (§9.2) likewise pre-shape the surface.
- v1 selects the active bridge by: (a) `--bridge <alias|id>` flag, (b) `[defaults] bridge = "<alias>"`, (c) the only bridge in credentials if exactly one is paired, (d) error 64 if multiple are paired and no selection is given.
- v1 does NOT support a single command targeting two bridges in parallel. That is a Phase 4+ feature with no commitment.

This is a deliberate phase-discipline choice mirroring `kasa-cli`'s "groups are bridge-side" pivot: the schema admits the future feature, the v1 codepath does not implement it.

---

## 7. Non-Functional Requirements

### 7.1 Performance

Targets assume a wired LAN or 5GHz Wi-Fi with <50ms RTT to the bridge. Hue bridges rate-limit to ~10 lights/sec writes, ~1 group/sec writes; aiohue's throttler enforces 1 concurrent request per 250ms which is conservative and rarely the bottleneck.

| Metric | Target |
|--------|--------|
| Bridge discovery (mDNS + NUPNP, default timeout) | < 5 seconds with default timeout |
| Bridge pair (fresh, button pressed promptly) | < 10 seconds end-to-end |
| Single light command (paired bridge) | < 500ms p95 |
| Single group command (paired bridge) | < 1500ms p95 (group ops are rate-limited harder by the bridge) |
| `list lights` against a 30-light bridge | < 1 second p95 (single GET fetches everything) |
| Batch of 10 light operations across one bridge, parallel | < 3 seconds p95 (subject to bridge rate limit) |
| Cold CLI startup (no command, just `--help`) | < 200ms |

### 7.2 Determinism

- Commands SHALL be idempotent where physically possible
- Identical input SHALL produce identical output structure (JSON key set is stable)
- No interactive prompts when stdin/stdout are not ttys, **except** `bridge pair` whose interactivity is the entire point. `bridge pair --non-interactive` provides a non-interactive path.

### 7.3 Observability

- `-v` enables INFO-level structured logs to stderr
- `-vv` enables DEBUG-level logs including raw HTTP request/response bodies (with the app-key segment of the URL redacted to `<app-key>` to avoid leaking it into log files)
- All log lines in verbose mode are single-line JSON
- Optional file logging via `[logging] file = "<path>"` in config — same JSON log lines tee'd to the file (append mode, line-buffered). stderr emission continues regardless. No rotation in v1; `logrotate` is the answer.

### 7.4 Portability

- Supported platforms: macOS 13+ (Apple Silicon and Intel), Linux x86_64 and arm64
- Python 3.11+ required (matches aiohue's minimum and `kasa-cli`'s pin)
- No Windows support in v1

### 7.5 Network model

- All `hue-cli` operations after pairing SHALL be local-LAN. The CLI SHALL NOT make outbound connections to Philips Hue servers under any post-pair code path.
- The **only** outbound HTTPS the CLI ever issues is to `https://discovery.meethue.com/` during `bridge discover` when cloud discovery is enabled (FR-1, FR-4). This is opt-out via `--no-cloud` or `[defaults] cloud_discovery = false`.
- DNS unreachable SHALL NOT block any operation that does not require cloud discovery (after pairing, all calls use the cached bridge IP).
- A bridge unreachable on the LAN SHALL exit code 3 with the bridge address in the error — not code 2 — even if the failure mode is a TCP reset.

---

## 8. CLI Surface

### 8.1 Verb summary

| Verb | Purpose |
|------|---------|
| `bridge discover` | Find Hue bridges on the LAN (mDNS + NUPNP + config) |
| `bridge pair` | Interactive button-press pairing |
| `bridge unpair` | Remove a paired bridge from the local credentials file |
| `bridge list` | List currently paired bridges (alias for `auth status`) |
| `list` | Sub-verbs: `lights`, `rooms`, `zones`, `scenes`, `sensors`, `schedules`, `all` |
| `info` | Show full state of one target (light, room, zone, scene, sensor, or `bridge`) |
| `on` | Power on |
| `off` | Power off |
| `toggle` | Flip on/off state |
| `set` | Brightness, color, color-temp, transition, effect, alert |
| `scene` | Sub-verbs: `apply`, `list` |
| `schedule` | Sub-verb: `list` (read-only) |
| `sensor` | Sub-verbs: `list`, `info` |
| `group` | Sub-verb: `list` (rooms + zones merged) |
| `batch` | Execute commands from file or stdin |
| `config` | `show` (effective config), `validate` (lint a config file) |
| `auth` | `status` (paired bridges + reachability), `flush`, `migrate` |

### 8.2 Target syntax

A target is one of:

- A **light name** as known to the bridge (e.g., `kitchen-pendant`) — case-insensitive
- A **light id** as assigned by the bridge (e.g., `7`)
- A **scene name** (e.g., `Energize`) or **scene id** (alphanumeric, bridge-assigned — typically ~15-16 chars on modern firmware; the CLI does not enforce length)
- A **room or zone name** prefixed with `@` (e.g., `@kitchen`, `@upstairs`)
- A **disambiguated room/zone** (`@room:Office` or `@zone:Office`)
- A **sensor name** or **sensor id**
- The literal `all` to target every light known to the bridge (the special "all lights" group `0` in Hue v1)
- The literal `bridge` for `info bridge` only

There is no IP-based or MAC-based per-light addressing — Hue lights are not network endpoints, they are Zigbee resources behind the bridge.

### 8.3 Common flags

| Flag | Meaning |
|------|---------|
| `--bridge <alias\|id>` | Select bridge when multiple are paired |
| `--bridge-ip <ip>` | Bypass credentials and target a specific IP (requires app-key in env or `--app-key`) |
| `--app-key <key>` | Override the credentials-file app-key for this invocation |
| `--json` | Pretty JSON output |
| `--jsonl` | Newline-delimited JSON output |
| `--quiet` | Suppress stdout |
| `--timeout <seconds>` | Per-operation timeout, default 5 |
| `--config <path>` | Use a non-default config file |
| `--concurrency N` | Override `[defaults] concurrency` for this invocation |
| `--transition <ms>` | Fade duration for `set` and `scene apply` |
| `--no-cloud` | Skip cloud NUPNP discovery (`bridge discover` only) |
| `--no-probe` | Skip live reachability probe (`auth status`, `list --probe`) |
| `-v`, `-vv` | Verbose / very verbose stderr logging |

### 8.4 Worked examples

```text
# Discover bridges on the LAN (mDNS + cloud + config)
$ hue-cli bridge discover
id                 host            name                     supports_v2  source
001788FFFE6ABCAF   192.168.86.62   Hue Bridge - 6ABCAF      true         mdns,nupnp

# Pair with the discovered bridge
$ hue-cli bridge pair --bridge-ip 192.168.86.62
Found Hue Bridge - 6ABCAF at 192.168.86.62
Press the link button on the Hue Bridge within 30 seconds, then press Enter...
[operator presses button, then Enter]
Paired. Stored app-key for bridge 001788FFFE6ABCAF.

# List paired bridges
$ hue-cli auth status --json
[
  {
    "id": "0017886abcaf",
    "name": "Hue Bridge - 6ABCAF",
    "host": "192.168.86.62",
    "paired_at": "2026-04-27T14:32:11Z",
    "reachable": true
  }
]

# List rooms and lights
$ hue-cli list rooms
id   name           class        light_ids   any_on   all_on
1    Kitchen        Kitchen      4,5,6       true     false
2    Living Room    Living room  7,8,9,10    false    false
3    Bedroom        Bedroom      11,12       false    false

$ hue-cli list lights --filter type=Extended\ color\ light --json
[ ... ]

# Turn off the kitchen
$ hue-cli off @kitchen

# Set bedroom to warm white at 30%
$ hue-cli set @bedroom --brightness 30 --kelvin 2700

# Apply a saved scene
$ hue-cli scene apply Sunset

# Apply a scene with a slow fade
$ hue-cli scene apply "Movie Night" --transition 3000

# Run a colorloop on the kitchen pendant
$ hue-cli set kitchen-pendant --effect colorloop

# Find-my-light: 15 seconds of flashing
$ hue-cli set kitchen-pendant --alert lselect

# Bridge state snapshot
$ hue-cli list all --json | jq '.scenes | length'
27

# Read motion sensor
$ hue-cli sensor info hallway-motion --json
{"id":"3","name":"Hallway motion","type":"ZLLPresence","state":{"presence":false,"lastupdated":"2026-04-27T14:30:11"},"config":{"battery":87,"reachable":true}}

# Run a list of commands at once
$ cat night.batch
off @living-room
off @kitchen
set @bedroom --brightness 5 --kelvin 2200
$ hue-cli batch --file night.batch --jsonl

# Show effective config
$ hue-cli config show
```

---

## 9. Configuration File

### 9.1 Location and format

Default path: `~/.config/hue-cli/config.toml` (override via `--config` or `HUE_CLI_CONFIG` env var).

Format: TOML.

### 9.2 Schema

| Section | Field | Type | Default | Purpose |
|---------|-------|------|---------|---------|
| `[defaults]` | `bridge` | string | — | Alias of bridge to use when multiple are paired |
| `[defaults]` | `timeout_seconds` | int | 5 | Per-operation timeout |
| `[defaults]` | `concurrency` | int | 5 | Max parallel ops in batch (lower than `kasa-cli`'s 10 because Hue rate-limits) |
| `[defaults]` | `output_format` | string | `auto` | `auto`/`text`/`json`/`jsonl` |
| `[defaults]` | `cloud_discovery` | bool | `false` | Allow `https://discovery.meethue.com/` during `bridge discover`. Default off per Decision 2 (strict local). Override per-invocation with `--cloud`. |
| `[defaults]` | `transition_ms` | int | — | Default fade duration if `--transition` omitted |
| `[credentials]` | `file_path` | string | `~/.config/hue-cli/credentials` | Default credentials file (chmod 0600) |
| `[logging]` | `file` | string | — | Optional path; when set, JSON log lines are tee'd here |
| `[bridges.<alias>]` | `id` | string | — | Bridge ID, canonical 12-char lowercase form (`normalize_bridge_id` output) for stable identification |
| `[bridges.<alias>]` | `host` | string | — | Static IP (skips re-discovery) |
| `[bridges.<alias>]` | `app_key_file` | string | — | Per-bridge credentials file override |

There is no `[groups]` table — see §5.10's Architectural Note. Hue Rooms and Zones are bridge-stored.

### 9.3 Complete example

```toml
# ~/.config/hue-cli/config.toml

[defaults]
bridge = "home"
timeout_seconds = 5
concurrency = 5
output_format = "auto"
cloud_discovery = false  # Decision 2: strict-local. Override per-invocation with --cloud
transition_ms = 400

[credentials]
file_path = "~/.config/hue-cli/credentials"

[logging]
# Optional. Comment out to disable file logging.
# file = "~/.local/state/hue-cli/log"

[bridges.home]
id = "0017886abcaf"  # canonical 12-char form from normalize_bridge_id (FR-2)
host = "192.168.86.62"

# Future second bridge — schema admits it, v1 codepath does not.
# [bridges.studio]
# id = "001788FFFExxxxxx"
# host = "192.168.86.63"
# app_key_file = "~/.config/hue-cli/credentials.studio"
```

### 9.4 Config validation

`hue-cli config validate` SHALL parse the file, resolve every `[bridges.<alias>]` against the credentials file (alias must match an entry), and exit 0 only if all references resolve. Bridges not in credentials produce code 6 errors.

---

## 10. Data Model

### 10.1 Bridge

```text
Bridge {
  id              : string         # canonical 12-char lowercase from normalize_bridge_id, e.g., "0017886abcaf"
  name            : string         # device-stored name, e.g., "Hue Bridge - 6ABCAF"
  host            : string         # IPv4 dotted quad
  mac             : string         # uppercase colon-separated, e.g., "00:17:88:6A:BC:AF"
  model_id        : string         # "BSB001" (round v1) or "BSB002" (square v2)
  api_version     : string         # e.g., "1.76.0"
  swversion       : string         # firmware build, e.g., "1976154040"
  supports_v2     : bool           # true for BSB002
  paired_at       : ISO8601 string
  reachable       : bool?          # populated by live probe; null if not probed
  # Network + zigbee + whitelist extras populated by `info bridge` (FR-21):
  gateway         : string?        # IPv4 default gateway from bridge config
  netmask         : string?        # IPv4 netmask
  timezone        : string?        # tz name, e.g., "America/Toronto"
  zigbee_channel  : int?           # 11/15/20/25 typically
  whitelist       : [{ id: string, name: string, last_use_date: ISO8601, create_date: ISO8601 }]?
}
```

### 10.2 Light

```text
Light {
  id                : string       # bridge-assigned numeric id as string
  name              : string       # operator-facing
  model_id          : string       # e.g., "LCT015"
  product_name      : string?      # e.g., "Hue color lamp"; null on older lights
  type              : string       # "Extended color light", "Color temperature light", "Dimmable light", "On/Off light"
  manufacturer_name : string       # e.g., "Signify Netherlands B.V."
  swversion         : string
  unique_id         : string       # MAC-like id with endpoint suffix
  features          : string[]     # ["dimmable","color","color-temp"] derived from type + capabilities
  state {
    on              : bool
    reachable       : bool
    brightness      : int          # raw 1-254 from bridge
    brightness_percent : int       # CLI-translated 0-100
    color_mode      : "hs"|"xy"|"ct"|null
    xy              : [float, float]?
    ct_mireds       : int?
    hue             : int?         # raw 0-65535
    sat             : int?         # raw 0-254
    effect          : "none"|"colorloop"
    alert           : "none"|"select"|"lselect"
  }
  control_capabilities {
    ct_min_mireds   : int?
    ct_max_mireds   : int?
    color_gamut     : [[float,float],[float,float],[float,float]]?  # R,G,B vertices
    color_gamut_type: "A"|"B"|"C"|"None"
  }
}
```

### 10.3 Room / Zone (Group)

```text
Group {
  id          : string
  type        : "Room" | "Zone"
  class       : string?       # Hue room category, e.g., "Kitchen", "Bedroom"; null for Zones
  name        : string
  light_ids   : string[]
  sensor_ids  : string[]      # Rooms can include sensors; usually empty for Zones
  state {
    any_on    : bool
    all_on    : bool
  }
}
```

### 10.4 Scene

```text
Scene {
  id             : string     # bridge-assigned, alphanumeric (modern bridges emit ~15-16 chars,
                              #   e.g., "gXdkB1um1SX1jEt"; older bridges may use longer ids)
  name           : string
  group_id       : string?    # room/zone this scene targets; null for legacy LightScene entries
  light_ids      : string[]   # lights affected
  last_updated   : ISO8601 string?
  recycle        : bool       # true if bridge may auto-delete to free space
  locked         : bool
  stale          : bool       # CLI-derived: true if group_id no longer resolves
}
```

### 10.5 Sensor

```text
Sensor {
  id        : string
  name      : string
  type      : string          # "ZLLPresence", "ZLLTemperature", "ZLLLightLevel",
                              # "ZGPSwitch", "ZLLSwitch", "Daylight", "CLIPGenericFlag", ...
  model_id  : string
  unique_id : string?
  state     : object          # type-specific; see FR-47
  config    : {
    on        : bool
    battery   : int?          # percent; absent for non-battery sensors
    reachable : bool?
    ...
  }
}
```

### 10.6 Color and brightness conversions

Operator-facing units (in CLI flags) and wire units (sent to bridge) differ. The CLI does the math.

| User flag | Range | Wire unit | Range | Conversion |
|-----------|-------|-----------|-------|------------|
| `--brightness` | 0–100 (percent) | `bri` | 1–254 | `bri = round(p/100 * 253) + 1` for p>0; p=0 → `on=False` |
| `--kelvin` | typically 2000–6500 | `ct` mireds | 153–500 | `mireds = round(1_000_000 / K)`, clamp to device range |
| `--mireds` | 153–500 | `ct` | 153–500 | passthrough |
| `--xy` | floats 0–1 each | `xy` | floats 0–1 each | passthrough |
| `--hex` | `#rrggbb` | `xy` | floats | sRGB → linear → CIE XYZ → xy, then gamut-clamp using `light.colorgamut` |
| `--hsv` | 0–360, 0–100, 0–100 | `xy` (and `bri` from V) | floats / 1–254 | HSV → RGB → same as `--hex`; V drives `bri` |
| `--color <name>` | enum | `xy` | floats | name → built-in xy table |
| `--transition` | ms | `transitiontime` | deciseconds | `dsec = round(ms/100)` |

### 10.7 Schedule (read-only listing only)

```text
Schedule {
  id              : string
  name            : string
  description     : string
  command         : object        # the bridge action this schedule fires; verbatim from /schedules/<id>.command
  localtime       : string        # iCal-like spec; verbatim
  status          : "enabled"|"disabled"
  autodelete      : bool
  created         : ISO8601 string
  starttime       : ISO8601 string?
}
```

The `command.address`, `command.method`, and `command.body` fields are passed through unmodified — Hue's schedule command grammar is rich and the CLI is read-only here.

---

## 11. Error Model and Exit Codes

### 11.1 Exit code table

Mirrors `kasa-cli` §11.1 verbatim where the semantic carries; Hue-specific occurrences noted.

| Code | Meaning | When |
|------|---------|------|
| 0 | Success | Operation completed; for batch, **every** sub-op succeeded |
| 1 | Bridge / device error | Bridge returned an error response (non-auth, non-network), e.g., a v1 error type other than 1 (unauthorized) or 101 (link button not pressed) |
| 2 | Authentication error | No paired bridge, app-key revoked from whitelist, credentials file mode too permissive, link-button-not-pressed timeout during `pair` |
| 3 | Network error | Timeout, connection refused, no route, mDNS bind failure, NUPNP unreachable when cloud discovery enabled, bridge unreachable on LAN, `BridgeBusy` after retry budget |
| 4 | Target not found | Light/room/zone/scene/sensor name not on bridge, IP not a Hue bridge, alias unknown in config |
| 5 | Unsupported feature | Verb/flag not supported by target (e.g., `set --kelvin` on a fixed-color bulb, `set --color` on a tunable-white-only bulb, future `schedule create`) |
| 6 | Config error | Config file missing when `--config`/`HUE_CLI_CONFIG` was set, invalid TOML, unresolvable references, unknown keys in credentials file |
| 7 | Partial batch failure | ≥1 sub-op succeeded AND ≥1 sub-op failed |
| 64 | Usage error | Invalid CLI invocation: ambiguous `@name` resolution, mutually-exclusive flags (e.g., `--kelvin` + `--color`), unknown named color, `unpair` with no arg in non-tty |
| 130 | SIGINT | Ctrl-C during execution; partial JSONL stream emitted with trailing `{"event":"interrupted",...}` line |
| 143 | SIGTERM | Process terminated; same partial-result + interrupted-line behavior as 130 |

When every sub-op of a batch fails for the **same** reason, the exit code SHALL be that reason's code (e.g., all calls hit `BridgeBusy` → 3). Mixed-failure-reasons SHALL exit 7 — the structured stderr error names the dominant failure.

**FR-58a (exit code 1 mapping):** Bridge error responses with a v1 error type other than 1 (unauthorized → 2 per FR-59), 101 (link-button not pressed → 2 per §6.2), or any code mapped explicitly elsewhere SHALL exit code 1 with `error: "unknown_error"` in the structured stderr payload, the raw v1 error type included in the message. This routes the residual long-tail of bridge errors to a single observable code instead of dropping them into 0.

### 11.2 Structured error object (stderr)

When stdout is JSON/JSONL or `--quiet` is set, errors are emitted to stderr as:

```json
{
  "error": "auth_failed",
  "exit_code": 2,
  "target": "@kitchen",
  "message": "Bridge 001788FFFE6ABCAF rejected app-key (whitelist entry was revoked)",
  "hint": "Run: hue-cli bridge pair --bridge-ip 192.168.86.62"
}
```

The `error` enum is closed and stable. Tooling MAY pattern-match on it. Closed values for v1:

`auth_failed`, `bridge_unreachable`, `bridge_busy`, `not_paired`, `link_button_not_pressed`, `unknown_target`, `ambiguous_target`, `unsupported_feature`, `usage_error`, `config_error`, `partial_failure`, `interrupted`, `unknown_error`.

---

## 12. Testing Strategy

### 12.1 Unit tests

- Mock bridge implementations (using `aioresponses` to intercept aiohttp calls) covering: light on/off/set, group set_action, scene apply, sensor read, schedule list (direct-aiohttp fallback path), bridge config GET, and the `Unauthorized` / `BridgeBusy` / `LinkButtonNotPressed` (error-101) error paths
- `create_app_key` test: simulated bridge returning error-101 three times then a success — CLI SHALL retry until success and persist the key
- Discovery tests: mDNS hit, NUPNP hit, both, neither (zero-bridges → exit 0 with empty JSON), cloud-discovery-disabled
- Pairing concurrency test: two `bridge pair` invocations against the same bridge IP — second SHALL succeed without disturbing the first's persisted entry (the bridge happily issues multiple keys to one app)
- Config parser tests with valid configs, invalid TOML, dangling `[bridges.<alias>]` refs, missing `version` in credentials file, unknown keys
- Output formatter tests asserting JSON key stability across mock light types (color, color-temp-only, dimmable-only, on-off-only): full Light record (FR-19/20), Bridge record, Group/Scene/Sensor records, structured error (§11.2)
- Color conversion tests: `--kelvin` → mireds with clamp, `--hex` → xy via gamut B (when device gamut unknown), `--color red` → built-in xy, brightness 0% → `on=False`, brightness 50% → `bri=127`, brightness 100% → `bri=254`
- Exit-code matrix tests: every code 0/1/2/3/4/5/6/7/64/130/143 SHALL be reachable by at least one test
- Mutually-exclusive flag tests: `--kelvin` + `--color` → 64; `--xy` + `--hex` → 64
- Capability-gating tests: `set --color` on a `Color temperature light` → 5; `set --kelvin` on a `Dimmable light` → 5
- `@room:Office` / `@zone:Office` disambiguation test
- Signal handling test: SIGINT during a 10-element batch SHALL produce ≤10 result lines plus the `{"event":"interrupted",...}` line and exit 130
- File-mode enforcement test: chmod 0644 credentials file → exit 2 on load

### 12.2 Integration tests

Gated on environment variable `HUE_TEST_BRIDGE_IP`. When unset, integration tests are skipped (CI default). When set, the test suite runs against a real bridge on the operator's LAN. CI never sets this variable.

A second variable `HUE_TEST_BRIDGE_APP_KEY` provides an existing app-key to skip the interactive `pair` step during integration runs. If it is unset, the pair-flow integration test SHALL be skipped (it cannot be automated against a physical button).

The integration suite SHALL exercise: discover, list (all six), info on a real light, on/off/toggle, set with brightness/kelvin/xy/color, scene apply, sensor list, schedule list (via direct-aiohttp fallback). It SHALL NOT mutate scenes, schedules, or bridge config.

### 12.3 Fixture corpus

Capture real bridge responses from the operator's BSB002 (with bridge-id, MAC, and app-key redacted) in `tests/fixtures/`:

- `GET /api/<key>` — full bridge state at startup
- `GET /api/<key>/lights` — light catalog
- `GET /api/<key>/groups` — rooms + zones + the special "all lights" group 0
- `GET /api/<key>/scenes` — scene catalog
- `GET /api/<key>/sensors` — sensor catalog including `Daylight` synthetic sensor
- `GET /api/<key>/schedules` — schedule catalog
- An error response of each type the v1 API documents: 1 (unauthorized), 101 (link button not pressed), 7 (invalid value), 6 (parameter not available), 3 (resource not available)

Fixtures SHALL be regeneratable via a `tests/fixtures/regenerate.py` script gated on `HUE_TEST_BRIDGE_IP`.

---

## 13. Distribution and Install

### 13.1 Recommended

```text
uv tool install git+ssh://git@github.com/agileguy/hue-cli
```

Rationale: same as `kasa-cli` §13.1 — personal tool, install pattern the operator already uses, isolated venv, no PyPI release management.

### 13.2 Alternatives considered

- **Publish to PyPI** — rejected for v1. The package name `hue-cli` collides with an unmaintained 2018-era npm package on a different registry but is free on PyPI. Reserving it without intent to maintain feels rude; pin from git instead.
- `pipx install git+...` — works identically; operator's stack guidance prefers `uv`.
- `brew install hue-cli` — would require maintaining a Homebrew tap; not warranted.
- Single-file binary via PyInstaller — increases binary size 10x for marginal install simplification; not recommended.

### 13.3 Versioning

Tag releases as `vX.Y.Z` in git. `uv tool install git+ssh://...@vX.Y.Z` pins to a tag. No PyPI registry, no semver contract beyond what tags promise. `pyproject.toml` SHOULD be PyPI-ready (LICENSE, README, classifiers, project URLs) so that future PyPI publication is a free migration if scope ever expands.

---

## 14. Out of Scope

The following are **explicitly excluded** from v1 to keep the scope honest:

- **TP-Link Tapo, Wiz, LIFX, IKEA Tradfri** — different protocols, different vendors. Not now, not via this SRD.
- **Matter and Thread devices** — different protocol stack.
- **HomeKit bridging.** The Hue bridge already does this natively; no value in CLI-level translation.
- **MQTT or REST relay.** This is a one-shot CLI, not a daemon.
- **Automation rules engine.** "If sunset, then dim lights" belongs in cron, systemd timers, Home Assistant, or Hue's own Rules engine via the mobile app.
- **GUI dashboard.** Visualizations consume JSON output if needed.
- **Cloud relay control.** No remote-network control. Local LAN only after pairing. The single cloud touchpoint is `discovery.meethue.com` during `bridge discover`, opt-out via `--no-cloud`.
- **Scene creation from current state.** Scene authoring is mobile-app territory.
- **Schedule, rule, or resourcelink editing.** Read-only listing only, forever.
- **Hue Entertainment / streaming.** The dtls-encrypted real-time streaming protocol is out of scope at all phases.
- **Firmware updates.** Use the Hue mobile app.
- **Light power-on behavior config (`startupbri`, `startupct`).** Per-light Hue setting; surfaced via `info` only, not editable from the CLI in v1.
- **Multi-bridge parallel ops in one command.** Schema admits multi-bridge; v1 codepath operates against one at a time. See §6.6.
- **Bridge whitelist mutation** — Philips removed the local-API `DELETE /api/<key>/config/whitelist/<key>` endpoint in API 1.31 (Hue Whitelist Security Update). Whitelist removal is no longer possible via the bridge's local REST surface and will not return; the only paths are the Hue mobile app and `https://account.meethue.com/apps`. `unpair` is local-only forever.
- **Comment-preserving TOML round-trip on config writes.** v1 does not write user config files.
- **`groups add` / `groups remove` config mutations.** There is no local `[groups]` section to mutate (§5.10).
- **v2 SSE event streaming** (`watch --events`). No commitment in this SRD.
- **Original round v1 bridge (BSB001).** The operator's bridge is BSB002. BSB001 likely works via the same v1 API, but v1 of the CLI does not test against it and does not commit.

---

## 15. Resolved Decisions

The 12 open questions originally surfaced for sign-off were resolved on 2026-04-27 and are recorded here for traceability. Decisions 2, 4, and 12 deviate from the Architect's recommended defaults. A multi-perspective review on 2026-04-28 produced the corrections rolled into this revision: app-key length (up to 40 alphanumeric, not 32-char hex), bridge-id canonical form (12-char lowercase from `normalize_bridge_id`, not 16-char), `light.controlcapabilities` is a dict access, `DELETE /config/whitelist/<key>` was removed in API 1.31, the wrapper's direct-aiohttp fallback uses HTTPS with bridge-self-signed-cert TLS context, and v1 scenes can be legacy `LightScene` (not always group-scoped). FR-CRED-4a (`bridge list`) and FR-CRED-7 (`auth migrate` v1 stub) were added to back the verbs the surface table promised.

| # | Decision area | Outcome |
|---|---|---|
| 1 | **App-name on pairing** | `hue-cli#<short-hostname>` (e.g. `hue-cli#dans-mbp`). Multiple machines pairing the same bridge get distinguishable whitelist entries in the Hue app. |
| 2 | **Cloud discovery default** | **OFF** — strict-local stance. `[defaults] cloud_discovery = false`. The CLI never makes outbound HTTPS to `discovery.meethue.com` by default. Override per-invocation with `--cloud`. (Deviation from rec: the rec was on-by-default for multi-NIC reliability; the operator chose privacy hardening.) |
| 3 | **`--brightness 0` implies `--off`** | Yes. `set lamp --brightness 0` translates to `on=False` in the API. Matches user expectation that "0% means off." |
| 4 | **Toggle group semantics** | **Consolidate-on rule.** `state.all_on == true` → all off; otherwise turn all on. Differs from the Hue mobile app's `any_on → all off` default — the operator wanted toggle to converge groups toward "all on" unless already fully on. (Deviation from rec.) |
| 5 | **Scene-name resolution** | Case-insensitive. Ambiguity (multiple scenes with the same name modulo case) → exit 64 listing the candidate scene-ids and their group names. Operators disambiguate by passing the id. |
| 6 | **Default batch / @group concurrency** | `5` — lower than `kasa-cli`'s 10 because Hue's API rate-limits more aggressively. Per-invocation override via `--concurrency N`. |
| 7 | **`--effect colorloop` auto-stop** | No auto-stop in v1. The effect persists until cleared by `--effect none` or another `set` overriding. Operators wanting timed loops use cron (`*/10 ... --effect colorloop` + `1-59/10 ... --effect none`). |
| 8 | **Color name table** | Built-in only, 12 names (`warm-white`, `cool-white`, `daylight`, `red`, `orange`, `yellow`, `green`, `cyan`, `blue`, `purple`, `magenta`, `pink`). Mirror `kasa-cli` Decision 8. Config-extensible color names deferred. |
| 9 | **mDNS library** | `zeroconf`. Mature, HA-maintained, handles macOS multi-NIC well. ~200KB dep, accepted. |
| 10 | **SSDP fallback on port 1900** | No. mDNS + manual `--bridge-ip` are sufficient. (Cloud-NUPNP is also disabled per Decision 2.) Avoids a second multicast code path with the same multi-NIC quirks. |
| 11 | **Schedule listing pagination** | None in v1. Hue caps at 100 schedules; even all 100 fit in <1MB JSON. Single GET, single emit. |
| 12 | **Re-pair conflict** | **Always proceed silently.** Bridge accepts duplicate-`app_name` whitelist entries; CLI updates the local credential to the new app-key. No prompts or warnings. Whitelist accumulation is acceptable; operator prunes via Hue app or future `hue-cli bridge prune-whitelist`. (Deviation from rec: the rec was warn-in-tty + prompt; the operator chose simpler.) |

---

## 16. Phase Plan

### 16.1 Phase 1 — MVP (1-2 weeks)

**Deliverable:** Discover, pair, list, info, on, off for one bridge.

- Project skeleton, `uv` packaging, entry point
- Config loader (TOML), `config show`, `config validate` (FR-64, FR-64a/b/c)
- aiohue wrapper with bridge resolution from credentials file
- Verbs: `bridge discover` (mDNS + NUPNP + config), `bridge pair`, `bridge unpair`, `bridge list`
- Verbs: `list lights`, `list rooms`, `list zones`, `list sensors`, `list all`, `info`, `info bridge`
- Verbs: `on`, `off`, `toggle` (lights and groups)
- Credential file with chmod 0600 enforcement (FR-CRED-1..3, FR-9)
- Output: text (default), `--json`, structured-error contract (FR-57b, §11.2)
- Exit codes 0, 1, 2, 3, 4, 6, 64, 130, 143
- Discovery zero-result handling (FR-5a)
- Direct-aiohttp fallback for `list schedules` (the wrapper plumbing for §4.5)
- Unit tests with mocked aiohttp (per §12.1)

### 16.2 Phase 2 — State Control, Scenes, Effects (1 week)

**Deliverable:** Full set verb, scene apply, effects, named colors, sensor info, schedule list.

- Verb: `set` with `--brightness`, `--kelvin`, `--mireds`, `--xy`, `--hex`, `--color`, `--hsv`, `--transition`, `--effect`, `--alert` (FR-27..38)
- Built-in named-color table (FR-32) with the minimum 12 colors
- sRGB → CIE XY conversion via `light.colorgamut` (FR-31), gamut B fallback
- Capability gating with exit code 5 (FR-36)
- Verb: `scene apply`, `scene list` (FR-39..42)
- Verb: `sensor info` with type-specific state shaping (FR-47)
- Verb: `schedule list` (read-only, via direct-aiohttp fallback) (FR-44)
- `auth status` (FR-CRED-4), `bridge list` alias (FR-CRED-4a), `auth flush` (FR-CRED-5), `auth migrate` v1 stub (FR-CRED-7)
- Optional file logging via `[logging] file` (§7.3)

### 16.3 Phase 3 — Groups, Batch, Polish (1 week)

**Deliverable:** Group dispatch, batch operations, full output formats, signal handling.

- `@room` / `@zone` / `@room:name` / `@zone:name` target syntax (FR-49, FR-50)
- Group dispatch via `Group.set_action` (FR-52) for `on`, `off`, `set`
- Verb: `batch` with `--file` and `--stdin`; comments and blank lines (FR-54b)
- `--jsonl` output format finalized; mixed-result JSON-validity contract (FR-57b)
- Exit code 7 for mixed-result batch failures (FR-58)
- SIGINT/SIGTERM handling with `{"event":"interrupted",...}` summary line (FR-54c)
- Per-operation result reporting in JSON
- `--filter` on list verbs (FR-18)

### 16.4 Phase 4 — (Reserved, no commitment)

There is **no Phase 4 deliverable** in this SRD. Candidate scope, in priority order if it ever happens: (a) v2 SSE event streaming for `watch --events` against motion sensors and switches, (b) multi-bridge parallel operations, (c) scene creation from current state, (d) bridge whitelist mutation on `unpair`, (e) gradient strip per-segment color via v2 API. A new SRD will define any of these if/when wanted.

---

**End of document.**
