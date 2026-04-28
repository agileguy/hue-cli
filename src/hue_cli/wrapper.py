"""aiohue wrapper — the only module that imports aiohue directly.

Per SRD §4: every other module talks to Hue through this layer. The wrapper provides
parallel discovery (mDNS + NUPNP + config-IP), the pair flow, an async context manager
around HueBridgeV1, and the §4.5 direct-aiohttp HTTPS fallback for schedules.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import aiohttp
import aiohue  # type: ignore[import-untyped]
import aiohue.discovery  # type: ignore[import-untyped]
import aiohue.errors  # type: ignore[import-untyped]
import aiohue.util  # type: ignore[import-untyped]
from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import (
    AsyncServiceBrowser,
    AsyncServiceInfo,
    AsyncZeroconf,
)

from hue_cli.errors import (
    AuthError,
    BridgeError,
    LinkButtonNotPressedError,
    NetworkError,
    NotFoundError,
    UsageError,
)

_LOG = logging.getLogger(__name__)

_HUE_MDNS_TYPE = "_hue._tcp.local."


@dataclass(frozen=True)
class DiscoveredBridge:
    """A bridge seen on the LAN. ``id`` is always the canonical 12-char form."""

    id: str
    host: str
    supports_v2: bool
    source: str


def normalize_id(wire_id: str) -> str:
    """Return the canonical 12-char lowercase bridge id (§FR-2)."""

    result: str = aiohue.util.normalize_bridge_id(wire_id)
    return result


class HueWrapper:
    """The wrapper surface verbs program against (see ``_protocols.HueWrapperProto``).

    Two usage shapes are supported:

    * ``async with HueWrapper(host, key) as w:`` — long-lived connection; ``w`` is the wrapper
      itself and ``w.bridge`` is the underlying ``aiohue.HueBridgeV1`` for advanced callers.
    * ``await w.list_lights_records()`` (and siblings) without ``async with`` — each record
      method auto-opens, materializes, and auto-closes the bridge connection.

    Verbs use the second shape; the first is available for callers that issue several calls
    against the same connection (Phase 3 batch).
    """

    def __init__(self, host: str, app_key: str) -> None:
        self.host = host
        self.app_key = app_key
        self._bridge: Any | None = None
        self._owns_connection = False

    @property
    def bridge(self) -> Any:
        if self._bridge is None:
            raise RuntimeError(
                "HueWrapper not connected; use 'async with' or call methods directly"
            )
        return self._bridge

    async def __aenter__(self) -> HueWrapper:
        await self._open()
        self._owns_connection = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._close()
        self._owns_connection = False

    async def _open(self) -> None:
        if self._bridge is not None:
            return
        # Assign to a local first so a failed initialize() doesn't leave
        # ``self._bridge`` half-set (a subsequent _open() call must retry,
        # not silently no-op against a never-initialized bridge object).
        bridge = aiohue.HueBridgeV1(self.host, self.app_key)
        try:
            await bridge.initialize()
        except BaseException:
            self._bridge = None
            raise
        self._bridge = bridge

    async def _close(self) -> None:
        if self._bridge is not None:
            await self._bridge.close()
            self._bridge = None

    async def _ensure(self) -> Any:
        """Open a transient connection if needed; return the live bridge object."""
        if self._bridge is None:
            await self._open()
        return self._bridge

    async def _maybe_close(self) -> None:
        """Close a transient connection (no-op if owned by an active ``async with``)."""
        if not self._owns_connection:
            await self._close()

    # --- Record materialization (FR-11..21) ---------------------------------

    async def list_lights_records(self) -> list[dict[str, Any]]:
        """Return §10.2 Light records as dicts. Auto-connects if needed."""
        bridge = await self._ensure()
        try:
            return [_light_to_record(light) for light in bridge.lights.values()]
        finally:
            await self._maybe_close()

    async def list_groups_records(self) -> list[dict[str, Any]]:
        """Return §10.3 Group records (Rooms + Zones). Auto-connects if needed."""
        bridge = await self._ensure()
        try:
            return [_group_to_record(group) for group in bridge.groups.values()]
        finally:
            await self._maybe_close()

    async def list_scenes_records(self) -> list[dict[str, Any]]:
        """Return §10.4 Scene records. Auto-connects if needed."""
        bridge = await self._ensure()
        try:
            valid_group_ids = {gid for gid in bridge.groups}
            return [_scene_to_record(scene, valid_group_ids) for scene in bridge.scenes.values()]
        finally:
            await self._maybe_close()

    async def list_sensors_records(self) -> list[dict[str, Any]]:
        """Return §10.5 Sensor records. Auto-connects if needed."""
        bridge = await self._ensure()
        try:
            return [_sensor_to_record(sensor) for sensor in bridge.sensors.values()]
        finally:
            await self._maybe_close()

    async def list_schedules_records(self) -> list[dict[str, Any]]:
        """Return §10.7 Schedule records via the §4.5 direct-aiohttp HTTPS fallback."""
        raw = await fetch_schedules_raw(self.host, self.app_key)
        return [_schedule_to_record(entry) for entry in raw]

    async def get_bridge_record(self) -> dict[str, Any]:
        """Return the §10.1 Bridge record. Auto-connects if needed."""
        bridge = await self._ensure()
        try:
            return _bridge_to_record(bridge)
        finally:
            await self._maybe_close()

    async def resolve_target(self, target: str) -> dict[str, Any]:
        """Resolve ``target`` per FR-19 precedence and return ``{kind, record, object}``.

        Precedence: ``@room:``/``@zone:`` prefix → ``@<name>`` (group) → light name/id →
        scene name/id → sensor name/id → bridge alias literal ``bridge``.
        """
        bridge = await self._ensure()
        try:
            return self._resolve_target_unlocked(bridge, target)
        finally:
            await self._maybe_close()

    def _resolve_target_unlocked(self, bridge: Any, target: str) -> dict[str, Any]:
        if target == "bridge":
            return {"kind": "bridge", "record": _bridge_to_record(bridge), "object": None}

        if target.startswith("@"):
            return self._resolve_group_target(bridge, target)

        for light in bridge.lights.values():
            if _matches(light, target):
                return {"kind": "light", "record": _light_to_record(light), "object": light}

        for scene in bridge.scenes.values():
            if _matches(scene, target):
                valid = {gid for gid in bridge.groups}
                return {
                    "kind": "scene",
                    "record": _scene_to_record(scene, valid),
                    "object": scene,
                }

        for sensor in bridge.sensors.values():
            if _matches(sensor, target):
                return {
                    "kind": "sensor",
                    "record": _sensor_to_record(sensor),
                    "object": sensor,
                }

        raise NotFoundError(f"target {target!r} not found on bridge")

    def _resolve_group_target(self, bridge: Any, target: str) -> dict[str, Any]:
        body = target[1:]
        constraint: str | None = None
        if ":" in body:
            prefix, _, body = body.partition(":")
            if prefix.lower() in {"room", "zone"}:
                constraint = prefix.capitalize()

        candidates: list[Any] = []
        for group in bridge.groups.values():
            gtype = getattr(group, "type", None)
            if constraint is not None and gtype != constraint:
                continue
            if gtype not in ("Room", "Zone"):
                continue
            if _eq_ci(getattr(group, "name", ""), body):
                candidates.append(group)

        if not candidates:
            raise NotFoundError(f"group {target!r} not found on bridge")
        if len(candidates) > 1:
            ids = ", ".join(
                f"{getattr(g, 'id', '?')}({getattr(g, 'type', '?')})" for g in candidates
            )
            raise UsageError(
                f"group {target!r} is ambiguous; disambiguate with @room:/@zone: ({ids})"
            )

        group = candidates[0]
        kind = "room" if getattr(group, "type", None) == "Room" else "zone"
        return {"kind": kind, "record": _group_to_record(group), "object": group}

    async def light_set_on(self, light: Any, on: bool) -> None:
        """Power-set a light (FR-22/23 dispatch helper)."""
        await self._ensure()
        try:
            await light.set_state(on=on)
        finally:
            await self._maybe_close()

    async def group_set_on(self, group: Any, on: bool) -> None:
        """Power-set a group (FR-22/23 dispatch helper)."""
        await self._ensure()
        try:
            await group.set_action(on=on)
        finally:
            await self._maybe_close()

    async def get_all_lights_group(self) -> Any:
        """Return the special Group 0 (all-lights) for the literal ``all`` target (FR-22)."""
        bridge = await self._ensure()
        try:
            return await bridge.groups.get_all_lights_group()
        finally:
            await self._maybe_close()


def _eq_ci(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def _matches(obj: Any, target: str) -> bool:
    """True if ``target`` equals the object's id or matches its name case-insensitively."""
    if str(getattr(obj, "id", "")) == target:
        return True
    return _eq_ci(getattr(obj, "name", ""), target)


def _safe(obj: Any, attr: str, default: Any = None) -> Any:
    """Best-effort getattr that swallows ``AiohueException`` style errors."""
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default


def _field(container: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict OR attribute from an object, uniformly.

    aiohue's v1 models mix shapes: ``Light.state`` is a property that returns a
    plain dict, ``Config.bridgeid`` is a property whose value lives in
    ``Config.raw["bridgeid"]``. Inside the record-materializers we frequently
    have a "container" (``state``, ``config.raw``, ``state_obj``) that may be
    either a dict on a real bridge or an attribute-bearing mock in tests; this
    helper picks the right access without forcing every call site to branch.
    """
    if container is None:
        return default
    if isinstance(container, dict):
        return container.get(key, default)
    return _safe(container, key, default)


def _bri_to_percent(bri: Any) -> int | None:
    """Translate raw 1-254 brightness to 0-100 percent (FR-11)."""
    if bri is None:
        return None
    try:
        b = int(bri)
    except (TypeError, ValueError):
        return None
    if b <= 1:
        return 0
    if b >= 254:
        return 100
    return round((b - 1) / 253 * 100)


def _light_to_record(light: Any) -> dict[str, Any]:
    # aiohue's ``Light.state`` is a property returning a plain dict; ``_field``
    # handles dict/attribute access uniformly so this body works equally on a
    # real aiohue Light and on attribute-bearing test mocks.
    state_obj = _safe(light, "state")
    state: dict[str, Any] = {}
    if state_obj is not None:
        bri = _field(state_obj, "bri")
        state = {
            "on": bool(_field(state_obj, "on", False)),
            "reachable": bool(_field(state_obj, "reachable", False)),
            "brightness": int(bri) if isinstance(bri, int) else None,
            "brightness_percent": _bri_to_percent(bri),
            "color_mode": _field(state_obj, "colormode"),
            "xy": _field(state_obj, "xy"),
            "ct_mireds": _field(state_obj, "ct"),
            "hue": _field(state_obj, "hue"),
            "sat": _field(state_obj, "sat"),
            "effect": _field(state_obj, "effect", "none"),
            "alert": _field(state_obj, "alert", "none"),
        }

    # ``controlcapabilities`` is a property on aiohue's Light returning a dict
    # (``raw["capabilities"]["control"]``); attribute access here is correct.
    capabilities_obj = _safe(light, "controlcapabilities") or {}
    if isinstance(capabilities_obj, dict):
        ct = capabilities_obj.get("ct") or {}
        gamut = capabilities_obj.get("colorgamut")
        gamut_type = capabilities_obj.get("colorgamuttype")
    else:
        ct, gamut, gamut_type = {}, None, None

    return {
        "id": str(_safe(light, "id", "")),
        "name": _safe(light, "name", ""),
        "model_id": _safe(light, "modelid"),
        "product_name": _safe(light, "productname"),
        "type": _safe(light, "type"),
        "manufacturer_name": _safe(light, "manufacturername"),
        "swversion": _safe(light, "swversion"),
        "unique_id": _safe(light, "uniqueid"),
        "state": state,
        "control_capabilities": {
            "ct_min_mireds": ct.get("min") if isinstance(ct, dict) else None,
            "ct_max_mireds": ct.get("max") if isinstance(ct, dict) else None,
            "color_gamut": gamut,
            "color_gamut_type": gamut_type,
        },
    }


def _group_to_record(group: Any) -> dict[str, Any]:
    # ``Group.state`` is a ``GroupState`` TypedDict (dict subclass) on aiohue;
    # ``_field`` accepts both dict and attribute-bearing mocks.
    state_obj = _safe(group, "state") or {}
    any_on = bool(_field(state_obj, "any_on", False))
    all_on = bool(_field(state_obj, "all_on", False))
    light_ids = _safe(group, "lights") or []
    sensor_ids = _safe(group, "sensors") or []
    # ``class`` is a Python keyword; aiohue does not expose it as a property,
    # so it lives only in ``group.raw["class"]``.
    group_class = _field(_safe(group, "raw"), "class")
    return {
        "id": str(_safe(group, "id", "")),
        "type": _safe(group, "type"),
        "class": group_class,
        "name": _safe(group, "name", ""),
        "light_ids": [str(x) for x in light_ids],
        "sensor_ids": [str(x) for x in sensor_ids],
        "state": {"any_on": any_on, "all_on": all_on},
    }


def _scene_to_record(scene: Any, valid_group_ids: set[str]) -> dict[str, Any]:
    # aiohue's ``Scene`` exposes ``lights`` as a property but does NOT expose
    # ``group``; the group id for GroupScene-typed scenes lives only in
    # ``scene.raw["group"]``. Prefer raw, fall back to attribute access for
    # non-aiohue mocks.
    raw = _safe(scene, "raw")
    raw_group_id = _field(raw, "group")
    if raw_group_id is None:
        raw_group_id = _safe(scene, "group")
    group_id = str(raw_group_id) if raw_group_id is not None else None
    stale = group_id is not None and group_id not in {str(x) for x in valid_group_ids}
    if stale:
        group_id = None
    light_ids = _safe(scene, "lights")
    if light_ids is None:
        light_ids = _field(raw, "lights", [])
    return {
        "id": str(_safe(scene, "id", "")),
        "name": _safe(scene, "name", ""),
        "group_id": group_id,
        "light_ids": [str(x) for x in light_ids],
        "last_updated": _safe(scene, "lastupdated"),
        "recycle": bool(_safe(scene, "recycle", False)),
        "locked": bool(_safe(scene, "locked", False)),
        "stale": stale,
    }


def _sensor_to_record(sensor: Any) -> dict[str, Any]:
    state_obj = _safe(sensor, "state") or {}
    config_obj = _safe(sensor, "config") or {}
    if not isinstance(state_obj, dict):
        state_obj = {}
    if not isinstance(config_obj, dict):
        config_obj = {}
    return {
        "id": str(_safe(sensor, "id", "")),
        "name": _safe(sensor, "name", ""),
        "type": _safe(sensor, "type"),
        "model_id": _safe(sensor, "modelid"),
        "unique_id": _safe(sensor, "uniqueid"),
        "state": state_obj,
        "config": config_obj,
    }


def _schedule_to_record(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(raw.get("id", "")),
        "name": raw.get("name", ""),
        "description": raw.get("description", ""),
        "command": raw.get("command", {}),
        "localtime": raw.get("localtime", raw.get("time", "")),
        "status": raw.get("status", ""),
        "autodelete": bool(raw.get("autodelete", False)),
        "created": raw.get("created"),
        "starttime": raw.get("starttime"),
    }


def _bridge_to_record(bridge: Any) -> dict[str, Any]:
    config = _safe(bridge, "config")
    if config is None:
        return {}
    # aiohue's ``Config`` exposes only a few real properties (bridgeid, name,
    # mac, modelid, swversion, apiversion); ipaddress/gateway/netmask/timezone/
    # zigbeechannel/whitelist live only in ``config.raw``. ``_field`` reads from
    # whichever shape the caller has; ``_safe`` is used where the property is
    # known to exist on aiohue's Config so mocks can also override it.
    raw = _safe(config, "raw")

    raw_id = (
        _safe(config, "bridgeid") or _safe(config, "bridge_id") or _field(raw, "bridgeid") or ""
    )
    bid = normalize_id(str(raw_id)) if raw_id else ""

    # ``whitelist`` on a v1 bridge is a dict keyed by app-key id.
    whitelist_raw = _field(raw, "whitelist") or {}
    whitelist: list[dict[str, Any]] = []
    if isinstance(whitelist_raw, dict):
        for key, entry in whitelist_raw.items():
            if not isinstance(entry, dict):
                continue
            whitelist.append(
                {
                    "id": key,
                    "name": entry.get("name", ""),
                    "last_use_date": entry.get("last use date") or entry.get("last_use_date"),
                    "create_date": entry.get("create date") or entry.get("create_date"),
                }
            )

    return {
        "id": bid,
        "name": _safe(config, "name", "") or _field(raw, "name", ""),
        "host": _field(raw, "ipaddress") or _field(raw, "ip", ""),
        "mac": _safe(config, "mac", "") or _field(raw, "mac", ""),
        "model_id": _safe(config, "modelid") or _field(raw, "modelid"),
        "api_version": _safe(config, "apiversion") or _field(raw, "apiversion"),
        "swversion": _safe(config, "swversion") or _field(raw, "swversion"),
        # ``supports_v2`` is a discovery-time capability (probed via
        # aiohue.discovery.is_v2_bridge in DiscoveredBridge), NOT a field on
        # the v1 Config endpoint. ``bridge discover`` and DiscoveredBridge
        # carry the real value; the bridge record surfaces ``None`` to make
        # the absence explicit rather than silently defaulting to False.
        "supports_v2": None,
        "paired_at": None,
        "reachable": True,
        "gateway": _field(raw, "gateway"),
        "netmask": _field(raw, "netmask"),
        "timezone": _field(raw, "timezone"),
        "zigbee_channel": _field(raw, "zigbeechannel"),
        "whitelist": whitelist,
    }


async def discover(
    timeout: float,
    *,
    cloud: bool,
    configured_ips: list[str],
) -> list[DiscoveredBridge]:
    """Fan out mDNS, NUPNP (if ``cloud``), and config-IP probes; aggregate and dedupe."""

    tasks: list[asyncio.Task[list[DiscoveredBridge]]] = [
        asyncio.create_task(_discover_mdns(timeout)),
    ]
    if cloud:
        tasks.append(asyncio.create_task(_discover_nupnp(timeout)))
    for ip in configured_ips:
        tasks.append(asyncio.create_task(_probe_one(ip, timeout, source="config")))

    gathered: list[list[DiscoveredBridge] | BaseException] = await asyncio.gather(
        *tasks, return_exceptions=True
    )

    by_id: dict[str, DiscoveredBridge] = {}
    for chunk in gathered:
        if isinstance(chunk, BaseException):
            _LOG.debug("discovery sub-task raised: %r", chunk)
            continue
        for bridge in chunk:
            existing = by_id.get(bridge.id)
            if existing is None:
                by_id[bridge.id] = bridge
                continue
            merged_sources = sorted({*existing.source.split(","), *bridge.source.split(",")})
            by_id[bridge.id] = DiscoveredBridge(
                id=existing.id,
                host=existing.host,
                supports_v2=existing.supports_v2 or bridge.supports_v2,
                source=",".join(merged_sources),
            )

    return list(by_id.values())


async def discover_one(host: str, timeout: float) -> DiscoveredBridge | None:
    """Probe a single IP. Returns ``None`` if it does not respond as a Hue bridge."""

    results = await _probe_one(host, timeout, source="config")
    return results[0] if results else None


async def pair(
    host: str,
    app_name: str,
    *,
    retry_interval: float,
    timeout: float,
) -> str:
    """Run the link-button registration loop. Returns the raw app-key string on success."""

    deadline = asyncio.get_running_loop().time() + timeout
    last_error: BaseException | None = None
    while True:
        try:
            key: str = await aiohue.create_app_key(host, app_name)
            return key
        except aiohue.errors.LinkButtonNotPressed as exc:
            last_error = exc
        except aiohue.errors.AiohueException as exc:
            raise NetworkError(f"bridge {host} returned {exc!r}") from exc
        except aiohttp.ClientError as exc:
            raise NetworkError(f"bridge {host} unreachable: {exc!r}") from exc

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise LinkButtonNotPressedError(
                f"link button on {host} not pressed within {timeout:.0f}s",
                hint="Press the link button on top of the bridge then retry.",
            ) from last_error
        await asyncio.sleep(min(retry_interval, max(remaining, 0.0)))


async def fetch_schedules_raw(host: str, app_key: str) -> list[dict[str, Any]]:
    """Direct-aiohttp HTTPS GET against the bridge's /schedules endpoint (§4.5).

    aiohue v1 has no schedules controller, so the wrapper falls back to a direct GET.

    TLS posture: we use ``ssl=False`` for the LAN-local request. This deviates from a
    "match aiohue's TLS context" rule only in spelling — aiohue v1 itself does not
    verify the bridge's Signify-issued certificate against the standard CA bundle
    either. The bridge presents a per-device cert that public CAs do not chain to,
    and the bridge is by definition LAN-local; the threat model already excludes
    same-LAN MITM at the §4 transport level.

    Response shape: a successful Hue v1 collection GET is a JSON object keyed by
    resource id (``{"1": {...}, "2": {...}}``); error responses are a JSON list
    of one or more ``{"error": {"type": int, "description": str, ...}}`` objects.
    Some bridges return ``[]`` for an empty collection.

    Error mapping per FR-58a / FR-59:

    * type ``1``  (unauthorized user)  → :class:`AuthError`        (exit 2)
    * type ``101`` (link button not pressed) → :class:`LinkButtonNotPressedError`
    * any other  → :class:`BridgeError`                            (exit 1)
    """

    url = f"https://{host}/api/{app_key}/schedules"
    connector = aiohttp.TCPConnector(ssl=False)
    try:
        async with aiohttp.ClientSession(connector=connector) as session, session.get(url) as resp:
            data: Any = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise NetworkError(f"schedules fetch failed against {host}: {exc!r}") from exc

    schedules: list[dict[str, Any]] = []

    if isinstance(data, list):
        # v1 error envelope or already-normalized list (some test fixtures and
        # legacy bridges).
        for entry in data:
            if isinstance(entry, dict) and "error" in entry and isinstance(entry["error"], dict):
                _raise_for_v1_error(entry["error"])
            if isinstance(entry, dict):
                schedules.append(entry)
        return schedules

    if isinstance(data, dict):
        # Bridge may wrap single-error responses as ``{"error": {...}}`` directly.
        if "error" in data and isinstance(data["error"], dict):
            _raise_for_v1_error(data["error"])
        # Successful collection response: dict keyed by id. Tag each value with
        # its key so downstream callers see the canonical id field.
        for sched_id, body in data.items():
            if not isinstance(body, dict):
                continue
            schedules.append({"id": sched_id, **body})
        return schedules

    raise BridgeError(f"unexpected schedules response: {data!r}")


def _raise_for_v1_error(err: dict[str, Any]) -> None:
    """Raise the appropriate hue-cli exception for a v1 error object.

    Type ``1`` (unauthorized user) → :class:`AuthError`.
    Type ``101`` (link button not pressed) → :class:`LinkButtonNotPressedError`.
    Anything else → :class:`BridgeError`.
    """

    err_type = err.get("type")
    description = err.get("description", "")
    if err_type == 1:
        raise AuthError(
            f"bridge rejected request: {description or 'unauthorized user'}",
            hint="Re-run hue-cli bridge pair to obtain a fresh app key.",
        )
    if err_type == 101:
        raise LinkButtonNotPressedError(
            f"bridge requires link-button press: {description or 'link button not pressed'}",
            hint="Press the link button on the Hue bridge then retry.",
        )
    raise BridgeError(
        f"bridge rejected request (type {err_type!r}): {description or err!r}",
    )


async def _discover_mdns(timeout: float) -> list[DiscoveredBridge]:
    """Browse zeroconf for ``_hue._tcp`` and resolve each hit."""

    results: dict[str, DiscoveredBridge] = {}
    azc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    pending: list[asyncio.Task[None]] = []

    def _on_state_change(
        zc: Any,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if state_change is not ServiceStateChange.Added:
            return
        pending.append(asyncio.create_task(_resolve_mdns(azc, service_type, name, results)))

    browser = AsyncServiceBrowser(
        azc.zeroconf,
        [_HUE_MDNS_TYPE],
        handlers=[_on_state_change],
    )
    try:
        await asyncio.sleep(timeout)
        if pending:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=2)
    finally:
        await browser.async_cancel()
        await azc.async_close()
    return list(results.values())


async def _resolve_mdns(
    azc: AsyncZeroconf,
    service_type: str,
    name: str,
    sink: dict[str, DiscoveredBridge],
) -> None:
    info = AsyncServiceInfo(service_type, name)
    if not await info.async_request(azc.zeroconf, 2000):
        return
    addresses = info.parsed_scoped_addresses()
    if not addresses:
        return
    host = addresses[0]
    raw_id_bytes = info.properties.get(b"id") if info.properties else None
    if raw_id_bytes is None:
        # Probe the bridge directly to pick up its id.
        probe = await _probe_one(host, timeout=3, source="mdns")
        for entry in probe:
            sink[entry.id] = entry
        return
    if isinstance(raw_id_bytes, bytes):
        raw_id = raw_id_bytes.decode("ascii", errors="replace")
    else:
        raw_id = str(raw_id_bytes)
    bridge_id = normalize_id(raw_id)
    supports_v2 = await _async_supports_v2(host)
    sink[bridge_id] = DiscoveredBridge(
        id=bridge_id, host=host, supports_v2=supports_v2, source="mdns"
    )


async def _async_supports_v2(host: str) -> bool:
    """Best-effort capability probe; returns False on any failure."""

    try:
        result: bool = await aiohue.discovery.is_v2_bridge(host)
    except Exception:
        return False
    return bool(result)


async def _discover_nupnp(timeout: float) -> list[DiscoveredBridge]:
    """Cloud NUPNP discovery via aiohue. Returns empty list on any error."""

    try:
        async with asyncio.timeout(timeout):
            entries: list[Any] = await aiohue.discovery.discover_nupnp()
    except (TimeoutError, aiohttp.ClientError, OSError, socket.gaierror) as exc:
        _LOG.debug("NUPNP discovery failed: %r", exc)
        return []

    out: list[DiscoveredBridge] = []
    for entry in entries:
        out.append(
            DiscoveredBridge(
                id=normalize_id(entry.id),
                host=entry.host,
                supports_v2=bool(getattr(entry, "supports_v2", False)),
                source="nupnp",
            )
        )
    return out


async def _probe_one(host: str, timeout: float, *, source: str) -> list[DiscoveredBridge]:
    """Probe ``host`` via ``aiohue.discovery.discover_bridge``. Empty list if not a bridge."""

    try:
        async with asyncio.timeout(timeout):
            entry: Any = await aiohue.discovery.discover_bridge(host)
    except (TimeoutError, aiohttp.ClientError, OSError, socket.gaierror) as exc:
        _LOG.debug("probe of %s failed: %r", host, exc)
        return []
    return [
        DiscoveredBridge(
            id=normalize_id(entry.id),
            host=entry.host,
            supports_v2=bool(getattr(entry, "supports_v2", False)),
            source=source,
        )
    ]
