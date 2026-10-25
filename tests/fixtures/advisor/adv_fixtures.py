"""TEST-ONLY observation builders for Person 3 (recipe route, toy land, synthetic reports).

Observations are plain v2 dicts built without a simulator. ``obs_crossing.fixture.json`` is one of them,
frozen to disk by ``python tests/fixtures/advisor/adv_fixtures.py``.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from save_the_whales.shared import geo  # noqa: E402
from save_the_whales.sources import land as landmod  # noqa: E402

COMMON = ROOT / "tests" / "fixtures" / "common"
ROUTE = [{"lat": 34.2, "lon": -120.1}, {"lat": 34.2, "lon": -120.075}, {"lat": 34.2, "lon": -120.05},
         {"lat": 34.2, "lon": -120.025}]
START = "2026-07-01T12:00:00Z"
CLEAR = {"source": "SIMULATED", "coverage_start_s": 0, "coverage_end_s": 12600, "zones": []}
REGION = {"lat_min": 33.8, "lat_max": 34.6, "lon_min": -120.5, "lon_max": -119.0}


def config():
    cfg = json.loads((ROOT / "config" / "default.json").read_text())
    cfg.update(json.loads((HERE / "config.fixture.json").read_text()))
    return cfg


def load_land(name="land_far.fixture.geojson"):
    return landmod.load_land(COMMON / name, config())


def iso(t_s: float) -> str:
    m, s = divmod(int(round(t_s)), 60)
    h, m = divmod(m, 60)
    return f"2026-07-01T{12 + h:02d}:{m:02d}:{s:02d}Z"


def storm(blocked=False, south=True, active=(0, 3600), severity=0.8, zone_id="storm"):
    lat0, lat1 = (34.185, 34.197) if south else (34.203, 34.215)
    poly = [{"lat": lat0, "lon": -120.075}, {"lat": lat0, "lon": -120.055}, {"lat": lat1, "lon": -120.055},
            {"lat": lat1, "lon": -120.075}]
    return {"source": "SIMULATED", "coverage_start_s": 0, "coverage_end_s": 12600,
            "zones": [{"zone_id": zone_id, "polygon": poly, "severity": severity, "active_from_s": active[0],
                       "active_until_s": active[1], "blocked": blocked}]}


def report(lat=34.2, lon=-120.065, observed_s=0.0, reported_s=30.0, err=100.0, rid="rpt-fixture-0001"):
    return {"detection_id": rid, "lat": lat, "lon": lon, "position_error_m": err, "observed_at": iso(observed_s),
            "reported_at": iso(reported_s), "source": "SYNTHETIC_FIXTURE"}


def ship_on_route(t_s=60.0, speed=18.0, ship_id="sim:999000001:v1", route=ROUTE, **overrides):
    """A ship that left route[0] at t=0 and sails the baseline at ``speed`` (on-route state)."""
    x0, y0 = geo.project(route[0])
    d = geo.knots_to_mps(speed) * t_s
    arcs, prog = [0.0], 0
    for a, b in zip(route, route[1:]):
        arcs.append(arcs[-1] + geo.distance_m(a, b))
    while prog + 1 < len(route) - 1 and arcs[prog + 1] <= d:
        prog += 1
    (ax, ay), (bx, by) = geo.project(route[prog]), geo.project(route[prog + 1])
    f = (d - arcs[prog]) / (arcs[prog + 1] - arcs[prog])
    here = geo.unproject(ax + (bx - ax) * f, ay + (by - ay) * f)
    ship = {"ship_id": ship_id, "lat": here["lat"], "lon": here["lon"], "sog_knots": speed, "cog_deg": 90.0,
            "length_m": 300.0, "beam_m": 40.0, "service_speed_knots": 18.0,
            "original_route": copy.deepcopy(route), "current_route": copy.deepcopy(route[prog + 1:]),
            "active_command": None, "baseline_progress_index": prog, "latest_revision": 0,
            "last_action_at": None, "incident_budget": None}
    ship.update(overrides)
    return ship


def observation(t_s=60.0, *, land=None, weather=CLEAR, detections=None, ships=None, decision_id="fixture:d0002"):
    return {"schema_version": "2.0", "now": iso(t_s), "decision_interval_min": 1.0, "bbox": dict(REGION),
            "static": {"land": land if land is not None else load_land(), "weather": copy.deepcopy(weather)},
            "ships": ships if ships is not None else [ship_on_route(t_s)],
            "whale_detections": [report()] if detections is None else detections,
            "decision_id": decision_id, "sim_time_s": float(t_s)}


if __name__ == "__main__":
    (HERE / "obs_crossing.fixture.json").write_text(json.dumps(observation(), indent=1) + "\n")
