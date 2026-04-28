"""``set`` verb — change brightness, color temperature, color, effects (FR-27..38).

Dispatch shape (mirrors ``onoff_cmd`` FR-22 pattern):

* light target  → ``Light.set_state(...)``  via the wrapper helper
* room/zone     → ``Group.set_action(...)`` via the wrapper helper
* literal ``all`` → ``Groups.get_all_lights_group()`` then ``set_action(...)``

The verb is a thin orchestrator. All numeric conversions live in
:mod:`hue_cli.colors`; this file only

1. parses the CLI flags and enforces the FR-35 mutual-exclusion groups,
2. checks FR-36 device-capability gating for **light** targets (group dispatch
   skips the check — the bridge applies state to whichever members support
   each operation),
3. assembles the wire kwargs (``bri``, ``ct``, ``xy``, ``effect``, ``alert``,
   ``transitiontime``) and dispatches via the wrapper.

The whole resolve+dispatch pair runs inside ``async with wrapper:`` so the
underlying aiohttp ``ClientSession`` stays alive between the resolve call
(which returns a Light/Group with a session-bound ``_request``) and the
``set_state``/``set_action`` call. Same pattern as ``onoff_cmd``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import click

from hue_cli.colors import (
    hex_to_xy,
    hsv_to_xy,
    kelvin_to_mireds,
    mireds_clamp,
    named_colors,
    percent_to_bri,
)
from hue_cli.errors import HueCliError, UnsupportedError, UsageError, emit_structured_error

if TYPE_CHECKING:
    from hue_cli._protocols import GroupProto, HueWrapperProto, LightProto


_VALID_EFFECTS = ("none", "colorloop")
_VALID_ALERTS = ("none", "select", "lselect")


# --- Flag parsing helpers ---------------------------------------------------


def _parse_xy(raw: str) -> tuple[float, float]:
    """Parse ``--xy x,y`` (FR-30) → ``(x, y)`` floats. Strict; no whitespace pairs."""
    parts = raw.split(",")
    if len(parts) != 2:
        raise UsageError(f"--xy must be 'x,y' (got {raw!r})")
    try:
        x = float(parts[0])
        y = float(parts[1])
    except ValueError as exc:
        raise UsageError(f"--xy values must be floats (got {raw!r})") from exc
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        raise UsageError(f"--xy values must be in [0,1] (got {raw!r})")
    return (x, y)


def _parse_hsv(raw: str) -> tuple[float, float, float]:
    """Parse ``--hsv h,s,v`` (FR-33) → ``(h, s, v)``."""
    parts = raw.split(",")
    if len(parts) != 3:
        raise UsageError(f"--hsv must be 'h,s,v' (got {raw!r})")
    try:
        h = float(parts[0])
        s = float(parts[1])
        v = float(parts[2])
    except ValueError as exc:
        raise UsageError(f"--hsv values must be numeric (got {raw!r})") from exc
    if not (0.0 <= h <= 360.0):
        raise UsageError(f"--hsv hue must be 0-360 (got {h!r})")
    if not (0.0 <= s <= 100.0 and 0.0 <= v <= 100.0):
        raise UsageError(f"--hsv s and v must be 0-100 (got {raw!r})")
    return (h, s, v)


def _resolve_named_color(name: str) -> tuple[float, float]:
    """FR-32 lookup. Unknown name → :class:`UsageError` listing the valid set."""
    table = named_colors()
    key = name.strip().lower()
    if key in table:
        return table[key]
    valid = ", ".join(sorted(table))
    raise UsageError(f"unknown color {name!r}; valid: {valid}")


def _check_mutex(
    *,
    kelvin: int | None,
    mireds: int | None,
    xy: str | None,
    hex_: str | None,
    color: str | None,
    hsv: str | None,
) -> None:
    """Enforce the FR-35 mutex groups.

    Two groups, mutually exclusive **across** groups and **within**:

    * color-temp: ``--kelvin`` XOR ``--mireds``
    * color-spec: ``--xy`` XOR ``--hex`` XOR ``--color`` XOR ``--hsv``

    Any cross-group overlap (e.g., ``--kelvin 2700 --color red``) → exit 64.
    """
    ct_flags = [
        ("--kelvin", kelvin is not None),
        ("--mireds", mireds is not None),
    ]
    color_flags = [
        ("--xy", xy is not None),
        ("--hex", hex_ is not None),
        ("--color", color is not None),
        ("--hsv", hsv is not None),
    ]

    ct_used = [name for name, present in ct_flags if present]
    if len(ct_used) > 1:
        raise UsageError(f"color-temperature flags are mutually exclusive: {' and '.join(ct_used)}")

    color_used = [name for name, present in color_flags if present]
    if len(color_used) > 1:
        raise UsageError(f"color-spec flags are mutually exclusive: {' and '.join(color_used)}")

    if ct_used and color_used:
        raise UsageError(
            "color-temperature and color-spec flags are mutually exclusive: "
            f"{ct_used[0]} and {color_used[0]}"
        )


# --- Capability gating (FR-36) ----------------------------------------------


def _light_capabilities(light_obj: Any) -> dict[str, Any]:
    """Read ``controlcapabilities`` off a light object.

    aiohue exposes this as a property returning a dict keyed
    ``{'mindimlevel', 'maxlumen', 'colorgamuttype', 'colorgamut', 'ct': {...}}``.
    Treat anything non-dict (or missing) as "no advertised capabilities" — the
    verb will then refuse color/CT operations on that light.
    """
    caps = getattr(light_obj, "controlcapabilities", None)
    if not isinstance(caps, dict):
        return {}
    return caps


def _supports_color(caps: dict[str, Any]) -> bool:
    """True if the light advertises a non-empty color gamut."""
    gamut = caps.get("colorgamut")
    if gamut:
        return True
    gamut_type = caps.get("colorgamuttype")
    return bool(gamut_type) and str(gamut_type).lower() != "none"


def _supports_ct(caps: dict[str, Any]) -> bool:
    """True if the light advertises a ``ct`` (color-temperature) range."""
    ct = caps.get("ct")
    return isinstance(ct, dict) and bool(ct)


def _enforce_light_caps(
    light_obj: Any,
    *,
    target: str,
    wants_ct: bool,
    wants_color: bool,
) -> None:
    """Raise :class:`UnsupportedError` (exit 5) for FR-36 mismatches.

    Group dispatch never reaches this — FR-36 is light-scoped per the SRD.
    """
    if not (wants_ct or wants_color):
        return
    caps = _light_capabilities(light_obj)
    if wants_ct and not _supports_ct(caps):
        raise UnsupportedError(
            f"light {target!r} does not support color temperature; "
            f"advertised capabilities: {sorted(caps)}",
        )
    if wants_color and not _supports_color(caps):
        raise UnsupportedError(
            f"light {target!r} does not support color; advertised capabilities: {sorted(caps)}",
        )


# --- Wire-kwarg assembly ----------------------------------------------------


def _assemble_state(
    *,
    brightness: int | None,
    kelvin: int | None,
    mireds: int | None,
    xy: tuple[float, float] | None,
    hex_: str | None,
    color_name: str | None,
    hsv: tuple[float, float, float] | None,
    transition: int | None,
    effect: str | None,
    alert: str | None,
    light_caps: dict[str, Any] | None,
    light_gamut: list[list[float]] | None,
) -> dict[str, Any]:
    """Build the kwargs to pass to ``set_state`` / ``set_action``.

    Brightness 0 → ``on=False`` and **no** ``bri`` field (FR-27, Decision 3).
    Brightness >0 + no ``--effect colorloop`` → just ``bri``; the bridge will
    leave ``on`` alone (and a 254 sent to an off light still turns it on,
    which is what the operator wants).

    ``--effect colorloop`` implies ``on=True`` (FR-37).

    ``--transition <ms>`` → ``transitiontime = round(ms/100)`` deciseconds
    (FR-34). The bridge default (4 ds = 400 ms) applies when the operator
    omits the flag.
    """
    state: dict[str, Any] = {}

    if brightness is not None:
        if brightness == 0:
            state["on"] = False
        else:
            state["bri"] = percent_to_bri(brightness)

    # Color temperature.
    if kelvin is not None:
        m = kelvin_to_mireds(kelvin)
        state["ct"] = _maybe_clamp_ct(m, light_caps)
    elif mireds is not None:
        state["ct"] = _maybe_clamp_ct(mireds, light_caps)

    # Color spec — produce xy from whichever flag the operator gave.
    if xy is not None:
        state["xy"] = xy
    elif hex_ is not None:
        state["xy"] = hex_to_xy(hex_, gamut=light_gamut)
    elif color_name is not None:
        state["xy"] = _resolve_named_color(color_name)
    elif hsv is not None:
        h, s, v = hsv
        state["xy"] = hsv_to_xy(h, s, v, gamut=light_gamut)
        # HSV's V drives bri unless the operator already passed --brightness.
        if brightness is None and v > 0:
            state["bri"] = percent_to_bri(int(v))
        elif brightness is None and v == 0:
            state["on"] = False

    if effect is not None:
        state["effect"] = effect
        if effect == "colorloop":
            # FR-37: colorloop implies on=True. Don't overwrite an explicit
            # brightness=0 → on=False, but in that pathological combination
            # the operator gets the latter — colorloop without light isn't
            # meaningful.
            state.setdefault("on", True)

    if alert is not None:
        state["alert"] = alert

    if transition is not None:
        # Round to 0 deciseconds is "as fast as the bridge can"; the bridge
        # accepts 0 and treats it as immediate.
        state["transitiontime"] = max(0, round(transition / 100))

    return state


def _maybe_clamp_ct(mireds: int, caps: dict[str, Any] | None) -> int:
    """Clamp mireds to the device's advertised ct range when known.

    For group dispatch the verb passes ``caps=None`` and we leave the value
    alone — the bridge will clamp per-bulb on its own.
    """
    if not caps:
        return int(mireds)
    ct = caps.get("ct")
    if not isinstance(ct, dict):
        return int(mireds)
    min_m = ct.get("min")
    max_m = ct.get("max")
    if not isinstance(min_m, int) or not isinstance(max_m, int):
        return int(mireds)
    return mireds_clamp(int(mireds), min_m, max_m)


# --- Apply (async, after argparse) ------------------------------------------


def _get_wrapper(ctx: click.Context) -> HueWrapperProto:
    """Pull the wrapper out of ``ctx.obj`` (same pattern as onoff_cmd)."""
    obj = ctx.obj or {}
    wrapper = obj.get("wrapper") if isinstance(obj, dict) else None
    if wrapper is None:
        raise click.ClickException("No active bridge wrapper. Run `hue-cli bridge pair` first.")
    return wrapper  # type: ignore[no-any-return]


async def _apply_set(
    wrapper: HueWrapperProto,
    target: str,
    *,
    brightness: int | None,
    kelvin: int | None,
    mireds: int | None,
    xy: tuple[float, float] | None,
    hex_: str | None,
    color_name: str | None,
    hsv: tuple[float, float, float] | None,
    transition: int | None,
    effect: str | None,
    alert: str | None,
) -> dict[str, Any]:
    """Resolve target + dispatch ``set_state`` / ``set_action`` per FR-22.

    Returns a small result dict for downstream JSON / JSONL emission.

    Group/all dispatch passes ``None`` for capability info — FR-36 is
    light-scoped per the SRD, so the bridge handles per-bulb fan-out.
    """
    wants_ct = (kelvin is not None) or (mireds is not None)
    wants_color = any(v is not None for v in (xy, hex_, color_name, hsv))

    async with wrapper:
        if target == "all":
            group = await wrapper.get_all_lights_group()
            state = _assemble_state(
                brightness=brightness,
                kelvin=kelvin,
                mireds=mireds,
                xy=xy,
                hex_=hex_,
                color_name=color_name,
                hsv=hsv,
                transition=transition,
                effect=effect,
                alert=alert,
                light_caps=None,
                light_gamut=None,
            )
            await wrapper.group_set_action(group, **state)
            return {"target": "all", "kind": "group", "state": state}

        resolved = await wrapper.resolve_target(target)
        kind = resolved.get("kind")
        obj = resolved.get("object")

        if kind == "light":
            _enforce_light_caps(
                obj,
                target=target,
                wants_ct=wants_ct,
                wants_color=wants_color,
            )
            caps = _light_capabilities(obj)
            gamut = caps.get("colorgamut") if isinstance(caps, dict) else None
            state = _assemble_state(
                brightness=brightness,
                kelvin=kelvin,
                mireds=mireds,
                xy=xy,
                hex_=hex_,
                color_name=color_name,
                hsv=hsv,
                transition=transition,
                effect=effect,
                alert=alert,
                light_caps=caps,
                light_gamut=gamut if isinstance(gamut, list) else None,
            )
            await wrapper.light_set_state(cast("LightProto", obj), **state)
            return {"target": target, "kind": "light", "state": state}

        if kind in ("room", "zone"):
            state = _assemble_state(
                brightness=brightness,
                kelvin=kelvin,
                mireds=mireds,
                xy=xy,
                hex_=hex_,
                color_name=color_name,
                hsv=hsv,
                transition=transition,
                effect=effect,
                alert=alert,
                light_caps=None,
                light_gamut=None,
            )
            await wrapper.group_set_action(cast("GroupProto", obj), **state)
            return {"target": target, "kind": kind, "state": state}

        raise click.ClickException(f"target {target!r} is not settable (kind={kind!r})")


# --- Click command ----------------------------------------------------------


@click.command(name="set")
@click.argument("target")
@click.option(
    "--brightness",
    type=click.IntRange(0, 100),
    default=None,
    help="Brightness 0-100 (FR-27). 0 implies off.",
)
@click.option(
    "--kelvin",
    type=int,
    default=None,
    help="Color temperature in Kelvin (FR-28).",
)
@click.option(
    "--mireds",
    type=int,
    default=None,
    help="Color temperature in mireds (FR-29). Hue's native unit.",
)
@click.option("--xy", "xy_raw", default=None, help="CIE 1931 chromaticity 'x,y' (FR-30).")
@click.option("--hex", "hex_value", default=None, help="sRGB hex '#rrggbb' (FR-31).")
@click.option("--color", "color_name", default=None, help="Named color (FR-32).")
@click.option("--hsv", "hsv_raw", default=None, help="HSV 'h,s,v' (FR-33).")
@click.option(
    "--transition",
    type=int,
    default=None,
    help="Fade duration in milliseconds (FR-34).",
)
@click.option(
    "--effect",
    type=click.Choice(_VALID_EFFECTS),
    default=None,
    help="Bridge effect (FR-37). 'colorloop' implies on=True.",
)
@click.option(
    "--alert",
    type=click.Choice(_VALID_ALERTS),
    default=None,
    help="Alert effect (FR-38). 'select' = 1 flash, 'lselect' = 15 sec.",
)
@click.pass_context
def set_cmd(
    ctx: click.Context,
    target: str,
    brightness: int | None,
    kelvin: int | None,
    mireds: int | None,
    xy_raw: str | None,
    hex_value: str | None,
    color_name: str | None,
    hsv_raw: str | None,
    transition: int | None,
    effect: str | None,
    alert: str | None,
) -> None:
    """Set state on TARGET (FR-27..38).

    Examples:

      hue-cli set @kitchen --brightness 50

      hue-cli set "Desk lamp" --color warm-white --brightness 75

      hue-cli set all --kelvin 2700

      hue-cli set "Reading" --hex #ffaa00 --transition 2000
    """
    obj = ctx.obj or {}
    json_mode = bool(obj.get("format") and getattr(obj["format"], "name", "") == "JSON")

    try:
        _check_mutex(
            kelvin=kelvin,
            mireds=mireds,
            xy=xy_raw,
            hex_=hex_value,
            color=color_name,
            hsv=hsv_raw,
        )
        xy = _parse_xy(xy_raw) if xy_raw is not None else None
        hsv = _parse_hsv(hsv_raw) if hsv_raw is not None else None

        # Eagerly validate hex / named-color so we exit 64 before opening a
        # bridge connection on bad input. The actual conversion happens
        # again inside ``_assemble_state`` once we know the device's gamut.
        if hex_value is not None:
            hex_to_xy(hex_value)
        if color_name is not None:
            _resolve_named_color(color_name)

        # No-op early exit: nothing to change. Match onoff's "exit 0 on
        # idempotent operation" stance — but here it would be ambiguous, so
        # return a usage error instead.
        if all(
            v is None
            for v in (
                brightness,
                kelvin,
                mireds,
                xy_raw,
                hex_value,
                color_name,
                hsv_raw,
                transition,
                effect,
                alert,
            )
        ):
            raise UsageError(
                "set requires at least one of --brightness/--kelvin/--mireds/"
                "--xy/--hex/--color/--hsv/--transition/--effect/--alert"
            )
    except HueCliError as exc:
        emit_structured_error(exc, target=target, json_mode=json_mode)
        raise SystemExit(exc.exit_code) from exc

    wrapper = _get_wrapper(ctx)
    try:
        asyncio.run(
            _apply_set(
                wrapper,
                target,
                brightness=brightness,
                kelvin=kelvin,
                mireds=mireds,
                xy=xy,
                hex_=hex_value,
                color_name=color_name,
                hsv=hsv,
                transition=transition,
                effect=effect,
                alert=alert,
            )
        )
    except HueCliError as exc:
        emit_structured_error(exc, target=target, json_mode=json_mode)
        raise SystemExit(exc.exit_code) from exc
