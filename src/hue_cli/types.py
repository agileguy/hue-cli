"""Record dataclasses for the §10 data model.

Frozen dataclasses matching SRD §10 shapes verbatim. These are the canonical
in-memory record shapes used by every verb's output path and serialized to
JSON / JSONL via :mod:`hue_cli.output`.

Bridge.id is the canonical 12-char lowercase form per FR-2 (e.g.,
``0017886abcaf``); the 16-char NUPNP wire form is collapsed by
``aiohue.util.normalize_bridge_id`` upstream of the wrapper layer. All JSON
emission of bridge ids SHALL use the 12-char form.

Scene.id is bridge-assigned alphanumeric (~15-16 chars on modern firmware,
longer on older bridges). The CLI does NOT enforce a fixed length per §10.4.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WhitelistEntry:
    """One entry in a Bridge's app-key whitelist (§10.1)."""

    id: str
    name: str
    last_use_date: str
    create_date: str


@dataclass(frozen=True)
class Bridge:
    """Bridge record per §10.1.

    ``id`` is the canonical 12-char lowercase form (FR-2). ``reachable`` is
    populated only when a live probe has been performed; ``None`` otherwise.
    The network/zigbee/whitelist extras (``gateway``, ``netmask``, ``timezone``,
    ``zigbee_channel``, ``whitelist``) are populated by ``info bridge`` (FR-21)
    and may be ``None`` for the leaner ``auth status`` / ``bridge list`` shape.
    """

    id: str
    name: str
    host: str
    mac: str
    model_id: str
    api_version: str
    swversion: str
    supports_v2: bool
    paired_at: str
    reachable: bool | None = None
    gateway: str | None = None
    netmask: str | None = None
    timezone: str | None = None
    zigbee_channel: int | None = None
    whitelist: list[WhitelistEntry] | None = None


@dataclass(frozen=True)
class LightState:
    """Light state sub-record per §10.2."""

    on: bool
    reachable: bool
    brightness: int
    brightness_percent: int
    color_mode: str | None = None
    xy: tuple[float, float] | None = None
    ct_mireds: int | None = None
    hue: int | None = None
    sat: int | None = None
    effect: str = "none"
    alert: str = "none"


@dataclass(frozen=True)
class LightControlCapabilities:
    """Light control_capabilities sub-record per §10.2."""

    ct_min_mireds: int | None = None
    ct_max_mireds: int | None = None
    color_gamut: tuple[tuple[float, float], tuple[float, float], tuple[float, float]] | None = None
    color_gamut_type: str | None = None


@dataclass(frozen=True)
class Light:
    """Light record per §10.2."""

    id: str
    name: str
    model_id: str
    type: str
    manufacturer_name: str
    swversion: str
    unique_id: str
    features: list[str]
    state: LightState
    control_capabilities: LightControlCapabilities
    product_name: str | None = None


@dataclass(frozen=True)
class GroupState:
    """Group state sub-record per §10.3."""

    any_on: bool
    all_on: bool


@dataclass(frozen=True)
class Group:
    """Group record (Room or Zone) per §10.3."""

    id: str
    type: str
    name: str
    light_ids: list[str]
    sensor_ids: list[str]
    state: GroupState
    class_: str | None = None


@dataclass(frozen=True)
class Scene:
    """Scene record per §10.4.

    ``group_id`` is ``None`` for legacy LightScene entries (no group) per FR-14
    and for scenes whose group has been deleted (in which case ``stale=True``).
    """

    id: str
    name: str
    light_ids: list[str]
    recycle: bool
    locked: bool
    stale: bool
    group_id: str | None = None
    last_updated: str | None = None


@dataclass(frozen=True)
class SensorConfig:
    """Sensor config sub-record per §10.5."""

    on: bool
    battery: int | None = None
    reachable: bool | None = None
    extras: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Sensor:
    """Sensor record per §10.5.

    ``state`` is type-specific (presence/temperature/buttonevent/etc) and is
    represented as a free-form dict here; downstream tools may pattern-match
    on ``type`` to interpret it.
    """

    id: str
    name: str
    type: str
    model_id: str
    state: dict[str, object]
    config: SensorConfig
    unique_id: str | None = None


@dataclass(frozen=True)
class Schedule:
    """Schedule record per §10.7 (read-only listing only).

    ``localtime`` mirrors the bridge's wire field name verbatim per the
    FR-16 / §10.7 alignment in the reviewed SRD. ``starttime`` is optional
    (only present once the schedule has been activated by the bridge).
    """

    id: str
    name: str
    description: str
    command: dict[str, object]
    localtime: str
    status: str
    autodelete: bool
    created: str
    starttime: str | None = None
