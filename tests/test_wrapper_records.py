"""Real-shape record-materialization tests for wrapper._*_to_record helpers.

These tests build fakes that match aiohue's ACTUAL v1 model layout (state /
config as dicts via ``@property``; ``Group.class`` and ``Scene.group`` only in
``raw``; ``Config`` exposing only ``bridgeid``/``name``/``mac``/``modelid``/
``swversion``/``apiversion`` as properties with everything else in ``raw``).

This is the gap PR #1 review caught: the previous tests passed pre-shaped
record dicts to the verbs, so a wrapper bug that hollowed out fields on a
real bridge could not surface. These tests directly exercise the wrapper's
materialization helpers against aiohue-shaped fakes.
"""

from __future__ import annotations

from typing import Any

from hue_cli import wrapper as wrapper_mod


# ---------------------------------------------------------------------------
# aiohue-shape fakes
# ---------------------------------------------------------------------------


class _RawLightFake:
    """Mirrors aiohue.v1.lights.Light: ``state`` and ``controlcapabilities``
    are ``@property`` accessors over ``raw``."""

    def __init__(self, light_id: str, raw: dict[str, Any]) -> None:
        self.id = light_id
        self.raw = raw

    @property
    def name(self) -> str:
        return self.raw["name"]

    @property
    def modelid(self) -> Any:
        return self.raw.get("modelid")

    @property
    def manufacturername(self) -> Any:
        return self.raw.get("manufacturername")

    @property
    def productname(self) -> Any:
        return self.raw.get("productname")

    @property
    def type(self) -> Any:
        return self.raw.get("type")

    @property
    def swversion(self) -> Any:
        return self.raw.get("swversion")

    @property
    def uniqueid(self) -> Any:
        return self.raw.get("uniqueid")

    @property
    def state(self) -> dict[str, Any]:
        # Plain dict — exactly what aiohue returns.
        return self.raw["state"]  # type: ignore[no-any-return]

    @property
    def controlcapabilities(self) -> dict[str, Any]:
        return self.raw.get("capabilities", {}).get("control", {})  # type: ignore[no-any-return]


class _RawGroupFake:
    """Mirrors aiohue.v1.groups.Group: ``state`` is a TypedDict (dict subclass);
    ``class`` is NOT a property and lives only in ``raw``."""

    def __init__(self, group_id: str, raw: dict[str, Any]) -> None:
        self.id = group_id
        self.raw = raw

    @property
    def name(self) -> str:
        return self.raw["name"]

    @property
    def type(self) -> Any:
        return self.raw["type"]

    @property
    def state(self) -> dict[str, Any]:
        return self.raw["state"]  # type: ignore[no-any-return]

    @property
    def lights(self) -> list[Any]:
        return self.raw.get("lights", [])  # type: ignore[no-any-return]

    @property
    def sensors(self) -> list[Any]:
        return self.raw.get("sensors", [])  # type: ignore[no-any-return]


class _RawSceneFake:
    """Mirrors aiohue.v1.scenes.Scene: ``lights`` IS a property, ``group`` is NOT.

    The bridge keys group-scoped scenes by an integer-string in ``raw["group"]``
    that the wrapper has to read from raw."""

    def __init__(self, scene_id: str, raw: dict[str, Any]) -> None:
        self.id = scene_id
        self.raw = raw

    @property
    def name(self) -> str:
        return self.raw["name"]

    @property
    def lights(self) -> list[Any]:
        return self.raw.get("lights", [])  # type: ignore[no-any-return]

    @property
    def lastupdated(self) -> Any:
        return self.raw.get("lastupdated")

    @property
    def recycle(self) -> bool:
        return bool(self.raw.get("recycle", False))

    @property
    def locked(self) -> bool:
        return bool(self.raw.get("locked", False))


class _RawConfigFake:
    """Mirrors aiohue.v1.config.Config: only bridgeid/name/mac/modelid/swversion/
    apiversion are real properties; everything else (ipaddress/gateway/netmask/
    timezone/zigbeechannel/whitelist) lives in ``raw``."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw

    @property
    def bridgeid(self) -> str:
        return self.raw["bridgeid"]  # type: ignore[no-any-return]

    @property
    def name(self) -> str:
        return self.raw["name"]  # type: ignore[no-any-return]

    @property
    def mac(self) -> str:
        return self.raw["mac"]  # type: ignore[no-any-return]

    @property
    def modelid(self) -> str:
        return self.raw["modelid"]  # type: ignore[no-any-return]

    @property
    def swversion(self) -> str:
        return self.raw["swversion"]  # type: ignore[no-any-return]

    @property
    def apiversion(self) -> str:
        return self.raw["apiversion"]  # type: ignore[no-any-return]


class _RawBridgeFake:
    """Mirrors aiohue's HueBridgeV1: holds a Config-shaped ``config`` attr."""

    def __init__(self, config: _RawConfigFake) -> None:
        self.config = config


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_light_record_reads_dict_state_correctly() -> None:
    """ISC-23: with aiohue's real ``state``-is-dict shape, _light_to_record
    must populate on/reachable/brightness etc. (would fail before the fix
    because ``getattr(state_dict, "on", False)`` always returns False)."""

    light = _RawLightFake(
        "1",
        {
            "name": "Kitchen pendant",
            "modelid": "LCT015",
            "manufacturername": "Signify Netherlands B.V.",
            "productname": "Hue color lamp",
            "type": "Extended color light",
            "swversion": "1.108.6",
            "uniqueid": "00:17:88:01:02:03:04:05-0b",
            "state": {
                "on": True,
                "reachable": True,
                "bri": 200,
                "colormode": "ct",
                "ct": 350,
                "alert": "none",
                "effect": "none",
            },
            "capabilities": {"control": {"ct": {"min": 153, "max": 500}}},
        },
    )

    record = wrapper_mod._light_to_record(light)

    assert record["id"] == "1"
    assert record["name"] == "Kitchen pendant"
    assert record["model_id"] == "LCT015"
    assert record["product_name"] == "Hue color lamp"
    assert record["type"] == "Extended color light"
    assert record["state"]["on"] is True
    assert record["state"]["reachable"] is True
    assert record["state"]["brightness"] == 200
    # (200 - 1) / 253 * 100 ≈ 78.66 → rounds to 79
    assert record["state"]["brightness_percent"] == 79
    assert record["state"]["color_mode"] == "ct"
    assert record["state"]["ct_mireds"] == 350
    assert record["control_capabilities"]["ct_min_mireds"] == 153
    assert record["control_capabilities"]["ct_max_mireds"] == 500


def test_light_record_unreachable_and_off_state() -> None:
    """ISC-23: an unreachable, off light must surface those flags accurately."""

    light = _RawLightFake(
        "7",
        {
            "name": "Hallway",
            "modelid": "LWB006",
            "type": "Dimmable light",
            "state": {
                "on": False,
                "reachable": False,
                "bri": 1,
                "alert": "none",
                "effect": "none",
            },
            "capabilities": {},
        },
    )

    record = wrapper_mod._light_to_record(light)

    assert record["state"]["on"] is False
    assert record["state"]["reachable"] is False
    assert record["state"]["brightness"] == 1
    # brightness 1 clamps to 0% per FR-11
    assert record["state"]["brightness_percent"] == 0


def test_group_record_reads_class_from_raw() -> None:
    """ISC-23: ``Group.class`` is a Python keyword — aiohue does not expose
    a Python property for it, so it lives only in ``group.raw['class']``."""

    group = _RawGroupFake(
        "2",
        {
            "name": "Bedroom",
            "type": "Room",
            "class": "Bedroom",
            "lights": ["3", "4"],
            "sensors": [],
            "state": {"any_on": True, "all_on": False},
        },
    )

    record = wrapper_mod._group_to_record(group)

    assert record["id"] == "2"
    assert record["name"] == "Bedroom"
    assert record["type"] == "Room"
    assert record["class"] == "Bedroom"
    assert record["light_ids"] == ["3", "4"]
    assert record["state"] == {"any_on": True, "all_on": False}


def test_scene_record_reads_group_from_raw() -> None:
    """ISC-23: ``Scene.group`` is NOT a Python property on aiohue's Scene;
    it lives only in ``scene.raw['group']``. Verify the wrapper sources it
    from raw and validates against the live group set for staleness."""

    scene = _RawSceneFake(
        "abc123",
        {
            "name": "Energize",
            "lights": ["1", "2"],
            "group": "5",
            "lastupdated": "2026-04-27T14:00:00",
            "recycle": False,
            "locked": False,
        },
    )

    record = wrapper_mod._scene_to_record(scene, valid_group_ids={"5", "1"})

    assert record["id"] == "abc123"
    assert record["name"] == "Energize"
    assert record["group_id"] == "5"
    assert record["light_ids"] == ["1", "2"]
    assert record["stale"] is False


def test_scene_record_marks_stale_group_id_as_none() -> None:
    """A scene whose ``raw['group']`` no longer matches a live group is stale."""

    scene = _RawSceneFake(
        "def456",
        {
            "name": "Old Scene",
            "lights": ["9"],
            "group": "99",
            "lastupdated": None,
            "recycle": False,
            "locked": False,
        },
    )

    record = wrapper_mod._scene_to_record(scene, valid_group_ids={"1", "2"})

    assert record["group_id"] is None  # stale → null per FR-14
    assert record["stale"] is True


def test_bridge_record_reads_network_fields_from_raw() -> None:
    """ISC-24: gateway/netmask/timezone/zigbeechannel/whitelist live only in
    ``config.raw`` on aiohue's Config — not as Python properties. The wrapper
    must source them from raw or the user-visible bridge record will have
    those fields silently set to None on a real bridge."""

    config = _RawConfigFake(
        {
            "bridgeid": "001788FFFE6ABCAF",
            "name": "Hue Bridge",
            "mac": "00:17:88:6a:bc:af",
            "modelid": "BSB002",
            "swversion": "1976154040",
            "apiversion": "1.76.0",
            "ipaddress": "192.168.86.62",
            "gateway": "192.168.86.1",
            "netmask": "255.255.255.0",
            "timezone": "America/Toronto",
            "zigbeechannel": 25,
            "whitelist": {
                "appkey1": {
                    "name": "hue-cli#dans-mbp",
                    "last use date": "2026-04-27T14:00:00",
                    "create date": "2026-04-01T00:00:00",
                }
            },
        }
    )
    bridge = _RawBridgeFake(config)

    record = wrapper_mod._bridge_to_record(bridge)

    # FR-2: canonical 12-char id form
    assert record["id"] == "0017886abcaf"
    assert record["name"] == "Hue Bridge"
    assert record["host"] == "192.168.86.62"
    assert record["mac"] == "00:17:88:6a:bc:af"
    assert record["model_id"] == "BSB002"
    assert record["api_version"] == "1.76.0"
    assert record["swversion"] == "1976154040"
    # FR-21 §10.1: gateway/netmask/timezone/zigbee_channel come from raw
    assert record["gateway"] == "192.168.86.1"
    assert record["netmask"] == "255.255.255.0"
    assert record["timezone"] == "America/Toronto"
    assert record["zigbee_channel"] == 25
    # whitelist normalized: list of {id, name, last_use_date, create_date}
    assert len(record["whitelist"]) == 1
    entry = record["whitelist"][0]
    assert entry["id"] == "appkey1"
    assert entry["name"] == "hue-cli#dans-mbp"
    assert entry["last_use_date"] == "2026-04-27T14:00:00"
    assert entry["create_date"] == "2026-04-01T00:00:00"
    # supports_v2 is a discovery-time capability, not present on Config —
    # we surface ``None`` rather than silently faking a default of False.
    assert record["supports_v2"] is None
