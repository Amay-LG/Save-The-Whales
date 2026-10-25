"""Explicit synthetic unit inputs; do not satisfy real AIS/land acceptance."""
from .geometry import project, unproject


def rect(x0, y0, x1, y1):
    return [unproject(x0, y0), unproject(x1, y0), unproject(x1, y1), unproject(x0, y1)]


def observation():
    route = [dict(lat=34.2, lon=x) for x in [-120.1, -120.075, -120.05, -120.025]]
    return dict(schema_version="2.0", now="2026-07-01T12:00:00Z", sim_time_s=0,
        decision_id="fixture:0", decision_interval_min=1,
        bbox=dict(lat_min=33.8, lat_max=34.6, lon_min=-120.5, lon_max=-119),
        static=dict(land=dict(map_id="TOY", source="SYNTHETIC_FIXTURE", sha256="fixture",
            validated=True, fixture_only=True, rings=[rect(-5000, 10000, -4000, 11000)], issues=[]),
            weather=dict(source="SIMULATED", coverage_start_s=0, coverage_end_s=20000, zones=[])),
        ships=[dict(ship_id="sim:999000001:test", lat=route[0]["lat"], lon=route[0]["lon"],
            sog_knots=18, cog_deg=90, length_m=300, beam_m=40, service_speed_knots=18,
            original_route=route, current_route=route[1:], active_command=None,
            baseline_progress_index=0, latest_revision=0, last_action_at=None)],
        whale_detections=[dict(detection_id="report:0", lat=34.2, lon=-120.065,
            position_error_m=50, observed_at="2026-07-01T12:00:00Z",
            reported_at="2026-07-01T12:00:00Z", source="SYNTHETIC_FIXTURE")])


def config(**planner):
    return dict(planner=planner, response=dict(response_lag_s=0))
