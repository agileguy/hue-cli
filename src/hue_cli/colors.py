"""Color and brightness conversions (SRD §5.6, §10.6, FR-27..38).

This module is the single home for every operator-facing → wire-unit conversion
the ``set`` verb performs. The verb stays a thin orchestrator; the math lives
here so ``tests/test_colors.py`` can exercise it without Click or aiohue.

Conversions provided:

* :func:`percent_to_bri`     — FR-27, ``--brightness 0-100`` → wire ``bri 1-254``
* :func:`kelvin_to_mireds`   — FR-28, Kelvin → mireds
* :func:`mireds_clamp`       — FR-28 device-range clamp helper
* :func:`hex_to_xy`          — FR-31, sRGB hex → CIE 1931 (x, y) with optional gamut clamp
* :func:`hsv_to_xy`          — FR-33, HSV → RGB → ``hex_to_xy`` path
* :func:`named_colors`       — FR-32, the 12 built-in name → xy entries

The named-color values come from D65 chromaticity tables — `warm-white` at
≈2700K, `cool-white` at ≈6500K (the D65 white point itself), `daylight` at
≈5000K, plus eight saturated hues. Built-in (not config-supplied) so behavior
is identical across machines, mirroring `kasa-cli` FR-19a/19b.

sRGB → XYZ uses the standard D65 matrix; the inverse gamma is the piecewise
``< 0.04045 ? c/12.92 : ((c+0.055)/1.055)**2.4`` curve. xy is the projective
chromaticity ``(X/(X+Y+Z), Y/(X+Y+Z))``. When a 3-vertex gamut triangle is
supplied, results outside the triangle are clamped to the closest point on a
triangle edge — the bridge does the final clamp on its end too, but doing it
here lets ``info`` previews show the value the bridge will actually store.
"""

from __future__ import annotations

from collections.abc import Mapping

from hue_cli.errors import UsageError

# --- Named-color table (FR-32) ----------------------------------------------

# CIE 1931 (x, y) coordinates. The whites come from black-body / D-illuminant
# chromaticity tables (warm-white ≈2700K, cool-white = D65, daylight ≈5000K =
# D50ish). The saturated hues are conventional sRGB primary/secondary values
# converted via the same matrix this module uses for ``--hex``; checked into
# the table directly so ``--color red`` is byte-identical regardless of the
# host's float precision.
_NAMED_COLORS: Mapping[str, tuple[float, float]] = {
    "warm-white": (0.4596, 0.4105),
    "cool-white": (0.3127, 0.3290),
    "daylight": (0.3457, 0.3585),
    "red": (0.6750, 0.3220),
    "orange": (0.6116, 0.3621),
    "yellow": (0.4317, 0.5009),
    "green": (0.4091, 0.5180),
    "cyan": (0.1655, 0.3275),
    "blue": (0.1500, 0.0600),
    "purple": (0.2725, 0.1096),
    "magenta": (0.3787, 0.1724),
    "pink": (0.4029, 0.2731),
}


def named_colors() -> dict[str, tuple[float, float]]:
    """Return a fresh dict copy of the FR-32 built-in name → xy table.

    The internal table is module-private; this returns a defensive copy so
    callers can mutate freely without poisoning subsequent lookups.
    """
    return dict(_NAMED_COLORS)


# --- Brightness (FR-27) ------------------------------------------------------


def percent_to_bri(percent: int) -> int:
    """Translate ``--brightness 0-100`` to a wire ``bri`` value in ``1-254``.

    FR-27 + §10.6: linear scale with ``bri = round(p/100 * 253) + 1`` for
    ``p > 0``. ``p == 0`` returns ``1`` (the minimum representable wire
    value); the **verb layer** translates ``--brightness 0`` to ``on=False``
    and never emits ``bri=0`` to the wire — see the FR-27 note in §5.6.

    Out-of-range input clamps to ``[0, 100]`` first; the bridge would clamp
    a too-large ``bri`` server-side anyway, but doing it here keeps stderr
    silent for the merely-typoed ``--brightness 110``.
    """
    p = max(0, min(100, int(percent)))
    if p <= 0:
        # The verb interprets this as ``on=False`` and won't emit ``bri``;
        # the floor value here exists only so a caller that does pass it
        # through gets a wire-valid number instead of a 0 the bridge rejects.
        return 1
    bri = round((p / 100.0) * 253) + 1
    return max(1, min(254, bri))


# --- Color temperature (FR-28, FR-29) ----------------------------------------


def kelvin_to_mireds(k: int) -> int:
    """Convert Kelvin → mireds via ``round(1_000_000 / K)`` (FR-28, §10.6).

    Hue's wire unit is mireds (reciprocal megakelvin). 2700K → 370, 6500K →
    154, 5000K → 200. Negative or zero K is a usage error — the bridge would
    accept neither and the conversion is undefined.
    """
    if k <= 0:
        raise UsageError(f"--kelvin must be positive (got {k!r})")
    return round(1_000_000 / k)


def mireds_clamp(mireds: int, min_m: int, max_m: int) -> int:
    """Clamp ``mireds`` to ``[min_m, max_m]``.

    Used by the ``set`` verb after ``kelvin_to_mireds`` to honor the device's
    advertised ``controlcapabilities['ct']`` range (FR-28). When the device
    doesn't advertise a range, the verb skips the clamp and passes the
    converted mireds through directly.
    """
    if min_m > max_m:
        raise UsageError(
            f"mireds_clamp: min ({min_m}) must be <= max ({max_m})",
        )
    return max(min_m, min(max_m, int(mireds)))


# --- sRGB → CIE xy (FR-31) ---------------------------------------------------


def _srgb_to_linear(c: float) -> float:
    """Apply the inverse sRGB gamma (piecewise standard).

    Below the 0.04045 knee the curve is linear (``c/12.92``); above it the
    ``((c + 0.055) / 1.055) ** 2.4`` power. This is the standard D65 sRGB
    transfer function; the same one the Hue mobile app uses.
    """
    if c <= 0.04045:
        return c / 12.92
    return float(((c + 0.055) / 1.055) ** 2.4)


def _parse_hex(hex_str: str) -> tuple[int, int, int]:
    """Parse ``#rrggbb`` or ``rrggbb`` (case-insensitive) → ``(r, g, b)``.

    The 3-character short form (``#rgb``) is **not** accepted. Hue's xy
    output is per-bulb and 8-bit precision is the operator's expectation;
    silently expanding ``#f00`` to ``#ff0000`` would obscure an honest typo
    (``#fff`` for ``#ffffff`` etc.) more often than it would help.
    """
    s = hex_str.strip()
    if s.startswith("#"):
        s = s[1:]
    if len(s) != 6:
        raise UsageError(
            f"--hex value must be #rrggbb (got {hex_str!r}); 3-char short form not supported"
        )
    try:
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
    except ValueError as exc:
        raise UsageError(f"--hex value {hex_str!r} is not valid hex") from exc
    return r, g, b


def hex_to_xy(
    hex_str: str,
    gamut: list[list[float]] | None = None,
) -> tuple[float, float]:
    """Convert ``#rrggbb`` → CIE 1931 ``(x, y)`` using D65 illuminant (FR-31).

    Pipeline: parse → normalize 0-255 → 0-1 → inverse-gamma → multiply by the
    sRGB→XYZ matrix (D65 row coefficients from the IEC 61966-2-1 standard) →
    project to xy. If ``gamut`` is the device's 3-vertex triangle (a list of
    three ``[x, y]`` pairs in R/G/B order, exactly as ``light.controlcapabilities
    ['colorgamut']`` returns it), the final point is clamped into the
    triangle so the value matches what the bridge will actually store.

    Invalid hex → :class:`UsageError` (exit 64). All-zero RGB (``#000000``)
    returns the D65 white point — there is no sensible chromaticity for
    pure black, and the verb's ``--brightness`` flag is the right knob for
    "darker" anyway.
    """
    r, g, b = _parse_hex(hex_str)
    rl = _srgb_to_linear(r / 255.0)
    gl = _srgb_to_linear(g / 255.0)
    bl = _srgb_to_linear(b / 255.0)

    # sRGB D65 → XYZ matrix (IEC 61966-2-1).
    x = 0.4124 * rl + 0.3576 * gl + 0.1805 * bl
    y = 0.2126 * rl + 0.7152 * gl + 0.0722 * bl
    z = 0.0193 * rl + 0.1192 * gl + 0.9505 * bl

    total = x + y + z
    if total <= 0:
        # Pure black. Fall back to the D65 white point so callers always get
        # a valid xy pair; brightness=0 (off) is the right way to express "no
        # output."
        cx, cy = 0.3127, 0.3290
    else:
        cx = x / total
        cy = y / total

    if gamut is not None:
        cx, cy = _clamp_to_gamut(cx, cy, gamut)

    return (round(cx, 4), round(cy, 4))


# --- HSV → xy (FR-33) --------------------------------------------------------


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    """Standard HSV-to-RGB. h in [0, 360), s/v in [0, 100]; output 0-255 ints.

    Used by :func:`hsv_to_xy`; kept private because the verb wants the xy
    output directly. V is **only** used here for the chromaticity path; the
    verb takes V as the brightness percent and feeds it to
    :func:`percent_to_bri` separately (per §10.6).
    """
    h = h % 360.0
    s = max(0.0, min(100.0, s)) / 100.0
    v = max(0.0, min(100.0, v)) / 100.0

    c = v * s
    h_prime = h / 60.0
    x = c * (1 - abs((h_prime % 2) - 1))
    m = v - c

    if 0 <= h_prime < 1:
        r, g, b = c, x, 0.0
    elif 1 <= h_prime < 2:
        r, g, b = x, c, 0.0
    elif 2 <= h_prime < 3:
        r, g, b = 0.0, c, x
    elif 3 <= h_prime < 4:
        r, g, b = 0.0, x, c
    elif 4 <= h_prime < 5:
        r, g, b = x, 0.0, c
    else:
        r, g, b = c, 0.0, x

    return (
        round((r + m) * 255),
        round((g + m) * 255),
        round((b + m) * 255),
    )


def hsv_to_xy(
    h: float,
    s: float,
    v: float,
    gamut: list[list[float]] | None = None,
) -> tuple[float, float]:
    """Convert HSV (0-360, 0-100, 0-100) → CIE xy via the FR-31 path.

    V drives the *chromaticity* here (HSV→RGB requires it); the verb
    separately maps V to ``bri`` via :func:`percent_to_bri`. So a caller
    saying ``--hsv 240,100,50`` gets the **same xy** as ``--hsv 240,100,100``
    plus a ``bri`` reflecting the 50% lightness. v=0 collapses to D65 white,
    matching the ``#000000`` behavior in :func:`hex_to_xy`.
    """
    # Use a max-V copy for the chromaticity calculation so v-as-brightness
    # doesn't double-dip. The *actual* V the operator typed is consumed by
    # the verb to drive --brightness.
    if v <= 0:
        # All-dark: same fallback as #000000 in hex_to_xy. The verb will set
        # on=False from the brightness=0 mapping; xy here is irrelevant but
        # we still return a valid pair so the type stays consistent.
        return (0.3127, 0.3290)
    r, g, b = _hsv_to_rgb(h, s, 100.0)
    return hex_to_xy(f"#{r:02x}{g:02x}{b:02x}", gamut=gamut)


# --- Gamut clamping ---------------------------------------------------------


def _clamp_to_gamut(
    x: float,
    y: float,
    gamut: list[list[float]],
) -> tuple[float, float]:
    """Clamp ``(x, y)`` into the device's color-gamut triangle.

    ``gamut`` is ``[[Rx,Ry],[Gx,Gy],[Bx,By]]``, exactly the shape
    ``light.controlcapabilities['colorgamut']`` returns. If the point is
    inside the triangle (barycentric test) we return it unchanged; otherwise
    we project to the closest point on whichever edge is nearest.
    """
    if len(gamut) != 3:
        # Unknown gamut shape: pass through. The bridge will clamp anyway.
        return (x, y)
    try:
        r = (float(gamut[0][0]), float(gamut[0][1]))
        g = (float(gamut[1][0]), float(gamut[1][1]))
        b = (float(gamut[2][0]), float(gamut[2][1]))
    except (TypeError, ValueError, IndexError):
        return (x, y)

    if _point_in_triangle((x, y), r, g, b):
        return (x, y)

    # Project to closest edge.
    candidates = [
        _closest_point_on_segment((x, y), r, g),
        _closest_point_on_segment((x, y), g, b),
        _closest_point_on_segment((x, y), b, r),
    ]
    best = min(candidates, key=lambda p: (p[0] - x) ** 2 + (p[1] - y) ** 2)
    return best


def _point_in_triangle(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> bool:
    """Barycentric inside-triangle test (sign-based)."""

    def _sign(
        p1: tuple[float, float],
        p2: tuple[float, float],
        p3: tuple[float, float],
    ) -> float:
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])

    d1 = _sign(p, a, b)
    d2 = _sign(p, b, c)
    d3 = _sign(p, c, a)
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


def _closest_point_on_segment(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> tuple[float, float]:
    """Closest point on segment ``ab`` to ``p`` (clamped projection)."""
    ax, ay = a
    bx, by = b
    px, py = p
    abx = bx - ax
    aby = by - ay
    denom = abx * abx + aby * aby
    if denom == 0:
        return (ax, ay)
    t = ((px - ax) * abx + (py - ay) * aby) / denom
    t = max(0.0, min(1.0, t))
    return (ax + t * abx, ay + t * aby)
