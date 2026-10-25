"""Deterministic, uncertainty-aware CPA proxies using reported evidence only."""
import json
from math import hypot, exp
from ._common import finite, point, utc


def relative_cpa(relative_position, relative_velocity, duration_s):
    rx, ry = relative_position
    vx, vy = relative_velocity
    vv = vx*vx+vy*vy
    t = 0.0 if vv <= 1e-18 else max(0.0, min(duration_s, -(rx*vx+ry*vy)/vv))
    return t, hypot(rx+vx*t, ry+vy*t)


def fresh_reports(observation, cfg):
    now, reports = utc(observation["now"]), {}
    # Deterministic winner for conflicting duplicates, independent of input order.
    for r in observation.get("whale_detections", []):
        try:
            if not isinstance(r.get("detection_id"), str) or not r["detection_id"] or not point(r):
                continue
            observed, reported = utc(r["observed_at"]), utc(r["reported_at"])
            age = (now-observed).total_seconds()
            if not 0 <= age <= cfg["max_detection_age_s"] or reported > now or reported < observed:
                continue
            if not finite(r.get("position_error_m")) or not 0 <= r["position_error_m"] <= 100000:
                continue
            item = dict(r, age_s=age)
            key = (observed, json.dumps(r, sort_keys=True, allow_nan=False))
            if r["detection_id"] not in reports or key > reports[r["detection_id"]][0]:
                reports[r["detection_id"]] = (key, item)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    ordered = sorted(reports.values(), key=lambda pair: pair[0], reverse=True)
    return [item for _, item in ordered[:cfg["max_reports"]]]


def predict_risk(legs, reports, now_s, service_speed, hull_radius, cfg, geometry):
    risk, watched, closest = 0.0, False, None
    for leg in legs:
        duration = leg["end_s"]-leg["start_s"]
        sx, sy = geometry.project(leg["start"])
        ex, ey = geometry.project(leg["end"])
        svx, svy = ((ex-sx)/duration, (ey-sy)/duration) if duration > 0 else (0, 0)
        for report in reports:
            # Native sightings do not contain velocity/animal identity: stationary mean.
            # A provider may explicitly supply a projected velocity in m/s.
            vel = report.get("velocity_mps", {})
            wx, wy = vel.get("x", 0), vel.get("y", 0)
            if not finite(wx) or not finite(wy) or abs(wx) > 100 or abs(wy) > 100:
                wx, wy = 0, 0
            age_at_start = report["age_s"]+leg["start_s"]-now_s
            px, py = geometry.project(report)
            px, py = px+wx*age_at_start, py+wy*age_at_start
            t, d = relative_cpa((px-sx, py-sy), (wx-svx, wy-svy), duration)
            sigma = max(50, hypot(report["position_error_m"], cfg["drift_mps"]*(age_at_start+t)))
            clearance = max(0, d-hull_radius)
            value = exp(-.5*(clearance/sigma)**2)*(leg["speed_knots"]/service_speed)**2
            risk = max(risk, value)
            watched |= clearance <= cfg["watch_distance_m"]+sigma and leg["start_s"]-now_s+t <= cfg["lookahead_s"]
            if closest is None or clearance < closest["clearance_m"]:
                closest = dict(clearance_m=clearance, cpa_sim_time_s=leg["start_s"]+t,
                               uncertainty_m=sigma, report_id=report["detection_id"])
    return dict(whale_risk=risk, watched=watched, closest_approach=closest,
                assumptions=["Stationary whale mean unless reported velocity is explicit",
                             "Age/lookahead drift is uncertainty, not animal identity or calibrated probability",
                             "Risk is max evidence/leg Gaussian proximity times squared speed ratio"])
