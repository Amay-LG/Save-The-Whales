"""Person 1: projection, units, land clearance and zone-interval primitives (no network)."""
import json
import math
from pathlib import Path

import pytest

from save_the_whales.shared import contracts as C
from save_the_whales.shared import geo

FIX = Path(__file__).parent / "fixtures" / "common"
P = lambda lat, lon: {"lat": lat, "lon": lon}  # noqa: E731
CLEAR = geo.ship_clearance_m(300, 40, 100)     # recipe ship: ~251.3 m

# Toy land rectangle from the Tasks.md recipe (fixture only).
BLOCK = [P(34.204, -120.075), P(34.204, -120.055), P(34.216, -120.055), P(34.216, -120.075)]
LAND = {"map_id": "toy", "sha256": "toy", "validated": True, "fixture_only": True, "rings": [BLOCK]}


def xy_land(*rings_xy):
    """Land map built from rings given in projected meters."""
    rings = [[geo.unproject(x, y) for x, y in ring] for ring in rings_xy]
    return {"map_id": "xy", "sha256": str(rings_xy), "validated": True, "fixture_only": True, "rings": rings}


def xy_path(*pts):
    return [geo.unproject(x, y) for x, y in pts]


# --- projection and units ---------------------------------------------------------------------------
def test_projection_origin_and_formula():
    assert geo.project(P(34.2, -119.75)) == (0.0, 0.0)
    x, y = geo.project(P(34.6, -120.5))
    assert x == pytest.approx(6_371_000 * math.radians(-120.5 + 119.75) * math.cos(math.radians(34.2)))
    assert y == pytest.approx(6_371_000 * math.radians(34.6 - 34.2))


@pytest.mark.parametrize("pt", [P(34.2, -120.0000001), P(34.2, -119.9999999), P(33.8, -120.5), P(34.6, -119.0)])
def test_round_trip_including_across_minus_120(pt):
    back = geo.unproject(*geo.project(pt))
    assert back["lat"] == pytest.approx(pt["lat"], abs=1e-12)
    assert back["lon"] == pytest.approx(pt["lon"], abs=1e-12)


def test_no_seam_at_minus_120():
    west, east = P(34.2, -120.0001), P(34.2, -119.9999)
    expected = 6_371_000 * math.radians(0.0002) * math.cos(math.radians(34.2))
    assert geo.distance_m(west, east) == pytest.approx(expected)
    assert geo.project(west)[0] < geo.project(east)[0]


def test_units_and_clearance_formula():
    assert geo.knots_to_mps(10) == pytest.approx(10 * 1852 / 3600)
    assert geo.mps_to_knots(geo.knots_to_mps(13.7)) == pytest.approx(13.7)
    assert geo.ship_clearance_m(300, 40, 100) == pytest.approx(0.5 * math.hypot(300, 40) + 100)


# --- check_water_path --------------------------------------------------------------------------------
def test_valid_distant_paths_pass():
    for path in ([P(34.19, -120.1), P(34.19, -120.02)], [P(34.2, -120.1), P(34.2, -120.025)], [P(34.3, -120.0)]):
        res = geo.check_water_path(path, LAND, CLEAR)
        assert res["ok"] and res["reasons"] == [] and res["min_clearance_m"] > CLEAR


@pytest.mark.parametrize("path,code", [
    ([P(34.21, -120.1), P(34.21, -120.02)], C.LAND_CROSSING),                     # crosses through
    ([P(34.194, -120.085), P(34.204, -120.075)], C.LAND_CONTACT),                 # touches a corner
    ([P(34.204, -120.09), P(34.204, -120.06)], C.LAND_CONTACT),                   # collinear along an edge
    ([P(34.2031, -120.1), P(34.2031, -120.02)], C.INSUFFICIENT_CLEARANCE),        # ~100 m off the coast
    ([P(34.21, -120.07), P(34.21, -120.06)], C.POINT_ON_LAND),                    # inside land
    ([P(34.21, -120.065)], C.POINT_ON_LAND),                                      # stationary on land
])
def test_rejections_even_when_waypoints_are_in_water(path, code):
    res = geo.check_water_path(path, LAND, CLEAR)
    assert not res["ok"] and code in res["reasons"]


def test_collinear_overlap_and_touch_are_contact_not_inside():
    res = geo.check_water_path([P(34.204, -120.09), P(34.204, -120.06)], LAND, CLEAR)
    assert res["reasons"] == [C.LAND_CONTACT] and res["min_clearance_m"] == 0.0


def test_only_the_return_leg_fails():
    path = [P(34.19, -120.1), P(34.19, -120.06), P(34.21, -120.06), P(34.19, -120.05)]
    res = geo.check_water_path(path, LAND, CLEAR)
    assert not res["ok"] and res["worst_leg_index"] in (1, 2) and C.POINT_ON_LAND in res["reasons"]


def test_clearance_boundary_is_rejected_with_documented_epsilon():
    land = xy_land([(-1000, 0), (1000, 0), (1000, 500), (-1000, 500)])
    at = geo.check_water_path(xy_path((-500, -CLEAR), (500, -CLEAR)), land, CLEAR)
    above = geo.check_water_path(xy_path((-500, -CLEAR - 0.01), (500, -CLEAR - 0.01)), land, CLEAR)
    assert at["reasons"] == [C.INSUFFICIENT_CLEARANCE]
    assert above["ok"] and above["min_clearance_m"] == pytest.approx(CLEAR + 0.01, abs=1e-6)


def test_reported_minimum_clearance_is_actual_distance():
    land = xy_land([(-100, 1000), (100, 1000), (100, 1200), (-100, 1200)])
    res = geo.check_water_path(xy_path((-500, 0), (500, 0)), land, 50.0)
    assert res["ok"] and res["min_clearance_m"] == pytest.approx(1000.0, abs=1e-6)


@pytest.mark.parametrize("land", [None, {}, {"validated": False, "rings": [BLOCK]}, {"validated": True, "rings": []}])
def test_invalid_map(land):
    res = geo.check_water_path([P(34.19, -120.1), P(34.19, -120.02)], land, CLEAR)
    assert res == {"ok": False, "min_clearance_m": None, "reasons": [C.INVALID_MAP], "worst_leg_index": None}


def test_invalid_path_inputs():
    assert geo.check_water_path([], LAND, CLEAR)["reasons"] == [C.INVALID_PATH]
    assert geo.check_water_path([P(float("nan"), -120.0)], LAND, CLEAR)["reasons"] == [C.NON_FINITE_COORDINATE]
    with pytest.raises(ValueError):
        geo.check_water_path([P(34.19, -120.1)], LAND, -1)


def test_real_land_fixture_file_shape_used_by_teammates():
    gj = json.loads((FIX / "land_blocked_side.fixture.geojson").read_text())
    ring = [P(lat, lon) for lon, lat in gj["features"][0]["geometry"]["coordinates"][0]]
    land = dict(LAND, rings=[ring])
    assert geo.check_water_path([P(34.2, -120.1), P(34.2, -120.025)], land, CLEAR)["ok"]          # baseline
    assert not geo.check_water_path([P(34.2, -120.08), P(34.2045, -120.065), P(34.2, -120.05)],   # north dogleg
                                    land, CLEAR)["ok"]


# --- segment_zone_intervals --------------------------------------------------------------------------
ZONE = BLOCK


def _iv(a, b, zone=ZONE):
    return [(round(t0, 6), round(t1, 6)) for t0, t1 in geo.segment_zone_intervals(a, b, zone)]


def test_zone_outside_inside_crossing():
    assert _iv(P(34.19, -120.1), P(34.19, -120.02)) == []
    (t0, t1), = geo.segment_zone_intervals(P(34.21, -120.1), P(34.21, -120.02), ZONE)
    assert t0 == pytest.approx(0.3125) and t1 == pytest.approx(0.5625)
    assert _iv(P(34.21, -120.07), P(34.21, -120.06)) == [(0.0, 1.0)]


def test_zone_starts_or_ends_inside():
    (t0, t1), = geo.segment_zone_intervals(P(34.21, -120.065), P(34.21, -120.02), ZONE)
    assert t0 == 0.0 and t1 == pytest.approx(10 / 45)
    (t0, t1), = geo.segment_zone_intervals(P(34.21, -120.1), P(34.21, -120.065), ZONE)
    assert t0 == pytest.approx(25 / 35) and t1 == 1.0


def test_zone_boundary_contact_is_conservative():
    touch = geo.segment_zone_intervals(P(34.194, -120.085), P(34.204, -120.075), ZONE)
    assert touch == [(1.0, 1.0)]                                       # zero-length contact, not silent
    along = geo.segment_zone_intervals(P(34.204, -120.09), P(34.204, -120.06), ZONE)
    assert along[0][0] == pytest.approx(0.5) and along[0][1] == 1.0     # along an edge counts as inside


def test_zone_zero_length_segments():
    assert _iv(P(34.21, -120.065), P(34.21, -120.065)) == [(0.0, 1.0)]
    assert _iv(P(34.204, -120.065), P(34.204, -120.065)) == [(0.0, 1.0)]  # on boundary
    assert _iv(P(34.3, -120.065), P(34.3, -120.065)) == []


def test_zone_concave_polygon_gives_all_crossings():
    # U shape opening north: a west-east segment through both arms gives two intervals.
    u = [geo.unproject(x, y) for x, y in [(0, 0), (300, 0), (300, 300), (200, 300), (200, 100),
                                         (100, 100), (100, 300), (0, 300)]]
    a, b = geo.unproject(-100, 200), geo.unproject(400, 200)
    ivs = geo.segment_zone_intervals(a, b, u)
    assert len(ivs) == 2
    assert ivs[0] == pytest.approx((0.2, 0.4)) and ivs[1] == pytest.approx((0.6, 0.8))


def test_zone_invalid_polygon_raises():
    with pytest.raises(ValueError, match=C.INVALID_POLYGON):
        geo.segment_zone_intervals(P(34.2, -120.1), P(34.2, -120.0), [P(34.2, -120.1), P(34.21, -120.1)])
