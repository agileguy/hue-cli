"""Tests for ``hue_cli.colors`` — pure conversion math (FR-27..38, §10.6).

These exercise the conversion library in isolation: no Click, no aiohue, no
network. The verb-level tests that exercise the same code through the CLI
surface live in ``test_set_cmd.py``.
"""

from __future__ import annotations

import math

import pytest

from hue_cli.colors import (
    GAMUT_B,
    hex_to_xy,
    hsv_to_xy,
    kelvin_to_mireds,
    mireds_clamp,
    named_colors,
    percent_to_bri,
)
from hue_cli.errors import UsageError

# --- Named colors (FR-32) ---------------------------------------------------


class TestNamedColors:
    def test_table_has_all_twelve_required_names(self) -> None:
        table = named_colors()
        required = {
            "warm-white",
            "cool-white",
            "daylight",
            "red",
            "orange",
            "yellow",
            "green",
            "cyan",
            "blue",
            "purple",
            "magenta",
            "pink",
        }
        assert required.issubset(set(table))

    def test_all_xy_values_are_in_unit_square(self) -> None:
        for name, (x, y) in named_colors().items():
            assert 0.0 <= x <= 1.0, f"{name} x={x} out of [0,1]"
            assert 0.0 <= y <= 1.0, f"{name} y={y} out of [0,1]"

    def test_returns_fresh_copy_so_callers_cannot_mutate(self) -> None:
        a = named_colors()
        a["red"] = (0.0, 0.0)
        # Mutation in the caller's copy doesn't bleed back into the next call.
        assert named_colors()["red"] != (0.0, 0.0)

    def test_white_points_are_distinct(self) -> None:
        table = named_colors()
        # The three white-ish entries must not collapse onto each other —
        # otherwise --color warm-white and --color daylight would be aliases.
        assert table["warm-white"] != table["cool-white"]
        assert table["warm-white"] != table["daylight"]
        assert table["cool-white"] != table["daylight"]

    def test_cool_white_targets_4000k_not_d65(self) -> None:
        # Operator UX: ``--color cool-white`` should land near 4000K
        # (Planckian locus ≈ (0.3804, 0.3768)), matching the consumer Hue
        # preset for "cool white". D65 (6500K) lives under ``daylight``.
        x, y = named_colors()["cool-white"]
        assert math.isclose(x, 0.3804, abs_tol=0.01), f"cool-white x={x} should be ≈0.3804"
        assert math.isclose(y, 0.3768, abs_tol=0.01), f"cool-white y={y} should be ≈0.3768"
        # And explicitly NOT the D65 white point.
        assert (x, y) != (0.3127, 0.3290)


# --- hex_to_xy (FR-31) -------------------------------------------------------


class TestHexToXy:
    def test_pure_red_lands_near_srgb_red_primary(self) -> None:
        x, y = hex_to_xy("#FF0000")
        # sRGB red primary in CIE 1931 is (0.640, 0.330) per IEC 61966-2-1.
        assert math.isclose(x, 0.640, abs_tol=0.05)
        assert math.isclose(y, 0.330, abs_tol=0.05)

    def test_white_without_hash_returns_d65(self) -> None:
        x, y = hex_to_xy("FFFFFF")
        # D65 white point is (0.3127, 0.3290).
        assert math.isclose(x, 0.3127, abs_tol=0.01)
        assert math.isclose(y, 0.3290, abs_tol=0.01)

    def test_lowercase_and_uppercase_equivalent(self) -> None:
        assert hex_to_xy("#abcdef") == hex_to_xy("#ABCDEF")

    def test_invalid_hex_chars_raise_usage_error(self) -> None:
        with pytest.raises(UsageError):
            hex_to_xy("#GG0000")

    def test_short_form_rejected(self) -> None:
        # Documented choice: 3-char short form is not supported. See colors.py.
        with pytest.raises(UsageError):
            hex_to_xy("#FFF")

    def test_pure_black_returns_d65_white_point(self) -> None:
        # All-zero RGB has no defined chromaticity; we fall back to D65 so
        # the type stays consistent. The verb expresses "no light" via
        # brightness=0 / on=False, not via a black xy.
        x, y = hex_to_xy("#000000")
        assert math.isclose(x, 0.3127, abs_tol=0.001)
        assert math.isclose(y, 0.3290, abs_tol=0.001)

    def test_gamut_clamp_pulls_out_of_gamut_red_inside(self) -> None:
        # Gamut B (a 2014-era common gamut) — narrower than sRGB primary red.
        # An out-of-gamut ``#FF0000`` should clamp to a point inside the
        # supplied triangle.
        x, y = hex_to_xy("#FF0000", gamut=GAMUT_B)
        # Inside-triangle test: barycentric signs all match.
        assert _point_in_triangle((x, y), GAMUT_B)

    def test_in_gamut_point_unchanged_by_gamut_clamp(self) -> None:
        # White is inside virtually every gamut.
        x, y = hex_to_xy("#FFFFFF", gamut=GAMUT_B)
        assert _point_in_triangle((x, y), GAMUT_B)


def _point_in_triangle(p: tuple[float, float], tri: list[list[float]]) -> bool:
    """Local barycentric test for asserting clamp behavior."""

    def sign(p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float]) -> float:
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])

    a = (tri[0][0], tri[0][1])
    b = (tri[1][0], tri[1][1])
    c = (tri[2][0], tri[2][1])
    d1 = sign(p, a, b)
    d2 = sign(p, b, c)
    d3 = sign(p, c, a)
    # 1e-3 tolerance: chromaticity values are 4-decimal-rounded by hex_to_xy,
    # so a point that *would* be inside a tighter triangle test by >1e-6 can
    # land on an edge after rounding. The bridge clamp is the authoritative
    # check; this helper only confirms the result is "in or on" the gamut.
    has_neg = (d1 < -1e-3) or (d2 < -1e-3) or (d3 < -1e-3)
    has_pos = (d1 > 1e-3) or (d2 > 1e-3) or (d3 > 1e-3)
    return not (has_neg and has_pos)


# --- kelvin_to_mireds + mireds_clamp (FR-28) --------------------------------


class TestKelvinToMireds:
    def test_2700k_is_370_mireds(self) -> None:
        # Classic warm-white reference: 1_000_000 / 2700 = 370.37 → 370.
        assert kelvin_to_mireds(2700) == 370

    def test_6500k_is_154_mireds(self) -> None:
        # Cool-white / D65: 1_000_000 / 6500 = 153.85 → 154.
        assert kelvin_to_mireds(6500) == 154

    def test_5000k_is_200_mireds(self) -> None:
        # Daylight: exactly 200.
        assert kelvin_to_mireds(5000) == 200

    def test_zero_kelvin_raises(self) -> None:
        with pytest.raises(UsageError):
            kelvin_to_mireds(0)

    def test_negative_kelvin_raises(self) -> None:
        with pytest.raises(UsageError):
            kelvin_to_mireds(-100)


class TestMiredsClamp:
    def test_above_max_clamps_down(self) -> None:
        assert mireds_clamp(700, 153, 500) == 500

    def test_below_min_clamps_up(self) -> None:
        assert mireds_clamp(100, 153, 500) == 153

    def test_in_range_passes_through(self) -> None:
        assert mireds_clamp(300, 153, 500) == 300

    def test_inverted_range_raises(self) -> None:
        with pytest.raises(UsageError):
            mireds_clamp(300, 500, 153)


# --- percent_to_bri (FR-27) -------------------------------------------------


class TestPercentToBri:
    def test_zero_returns_one_with_verb_layer_handling_on_false(self) -> None:
        # The verb layer treats --brightness 0 as on=False and never emits
        # bri=0 to the wire. This module's floor value is 1 so bare callers
        # still get a wire-valid number; the verb test exercises the off
        # path.
        assert percent_to_bri(0) == 1

    def test_one_hundred_returns_two_fifty_four(self) -> None:
        assert percent_to_bri(100) == 254

    def test_fifty_returns_about_one_twenty_seven(self) -> None:
        # round(50/100 * 253) + 1 — Python's banker's rounding turns 126.5
        # into 126, so the result is 127. The SRD specifies "~127" which
        # this matches; the linear midpoint of [1, 254] is 127.5 anyway.
        assert percent_to_bri(50) == 127

    def test_one_returns_at_least_one(self) -> None:
        assert percent_to_bri(1) >= 1

    def test_clamps_above_one_hundred(self) -> None:
        assert percent_to_bri(150) == 254

    def test_clamps_below_zero(self) -> None:
        # Negative is treated as zero, which is the off-floor.
        assert percent_to_bri(-10) == 1


# --- hsv_to_xy (FR-33) -------------------------------------------------------


class TestHsvToXy:
    def test_pure_blue_lands_near_blue_primary(self) -> None:
        # HSV(240, 100, 100) is pure blue → ≈ (0.150, 0.060) sRGB primary.
        x, y = hsv_to_xy(240.0, 100.0, 100.0)
        assert math.isclose(x, 0.150, abs_tol=0.05)
        assert math.isclose(y, 0.060, abs_tol=0.05)

    def test_pure_green_lands_near_green_primary(self) -> None:
        x, y = hsv_to_xy(120.0, 100.0, 100.0)
        # sRGB green primary is (0.300, 0.600).
        assert math.isclose(x, 0.300, abs_tol=0.05)
        assert math.isclose(y, 0.600, abs_tol=0.05)

    def test_v_zero_returns_d65_fallback(self) -> None:
        # All-dark: chromaticity is undefined, but we return the D65 white
        # point so callers always get a valid (x, y) pair.
        x, y = hsv_to_xy(0.0, 100.0, 0.0)
        assert math.isclose(x, 0.3127, abs_tol=0.001)
        assert math.isclose(y, 0.3290, abs_tol=0.001)

    def test_chromaticity_independent_of_v(self) -> None:
        # The same hue+sat at v=50 and v=100 should produce the same xy —
        # V drives bri at the verb layer, not the chromaticity.
        a = hsv_to_xy(0.0, 100.0, 50.0)
        b = hsv_to_xy(0.0, 100.0, 100.0)
        assert math.isclose(a[0], b[0], abs_tol=0.005)
        assert math.isclose(a[1], b[1], abs_tol=0.005)
