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

from hue_cli.errors import LinkButtonNotPressedError, NetworkError, NotFoundError

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
    """Async context manager wrapping ``aiohue.HueBridgeV1``."""

    def __init__(self, host: str, app_key: str) -> None:
        self.host = host
        self.app_key = app_key
        self._bridge: Any | None = None

    async def __aenter__(self) -> Any:
        self._bridge = aiohue.HueBridgeV1(self.host, self.app_key)
        await self._bridge.initialize()
        return self._bridge

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._bridge is not None:
            await self._bridge.close()
            self._bridge = None


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

    aiohue v1 has no schedules controller, so the wrapper falls back to a direct GET. Uses
    HTTPS with self-signed-cert tolerance — the bridge presents a Signify-issued cert that
    standard CA bundles do not trust.
    """

    url = f"https://{host}/api/{app_key}/schedules"
    connector = aiohttp.TCPConnector(ssl=False)
    try:
        async with aiohttp.ClientSession(connector=connector) as session, session.get(url) as resp:
            data: Any = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise NetworkError(f"schedules fetch failed against {host}: {exc!r}") from exc

    if isinstance(data, dict):
        # Bridge wraps single-error responses as {"error": {...}} or v1 array form.
        raise NotFoundError(f"unexpected schedules response shape: {data!r}")
    if not isinstance(data, list):
        raise NotFoundError(f"unexpected schedules response: {data!r}")

    schedules: list[dict[str, Any]] = []
    for entry in data:
        if isinstance(entry, dict) and "error" in entry and isinstance(entry["error"], dict):
            err = entry["error"]
            raise NotFoundError(f"bridge rejected schedules query: {err.get('description', err)}")
        if isinstance(entry, dict):
            schedules.append(entry)
    return schedules


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
