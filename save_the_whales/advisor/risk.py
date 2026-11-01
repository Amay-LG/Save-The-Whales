"""Whale evidence handling and timed-leg relative CPA risk (F2, F8).

Evidence: reports are deduplicated by ``detection_id`` (a repeated ID is the same report), aged in
virtual time from ``observation.now``, reports not yet due or older than ``max_detection_age_s`` are
dropped, and at most ``max_reports`` of the newest are kept. Missing evidence is not proof of absence.

Prediction (declared assumptions): whale motion is unknown, so its mean is stationary at the reported
position, with uncertainty ``sigma(t) = max(sigma_min_m, hypot(position_error_m, drift_mps*(t -
observed_at)))`` growing with age and lookahead.

Per leg (positions in meters, times absolute virtual seconds), with the whale mean advanced to the leg's
actual start time::

    r = whale - ship_at_leg_start;  v = whale_velocity - leg_velocity   (whale velocity = 0)
    t_cpa = clamp(-dot(r, v) / dot(v, v), 0, leg_duration)   (t_cpa = 0 when v = 0)
    d_cpa = |r + v * t_cpa|

Risk proxy (deterministic; not a calibrated collision probability): the Gaussian mass of the whale's
lateral position inside the ship's swept corridor at CPA, times the illustrative speed weight::

    sigma = sigma(t_start + t_cpa);  R = corridor half-width (hull clearance radius)
    risk  = (Phi((R - d_cpa) / sigma) - Phi((-R - d_cpa) / sigma)) * (speed / service)^2

Unlike exp(-d^2/2sigma^2), this falls as uncertainty spreads, so an old diffuse report does not look more
dangerous everywhere. Risk is 0 when ``d_cpa - R > watch_distance_m``. Path risk is the maximum over
legs and evidence, so repeated reports cannot inflate confidence.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple


def parse_utc(text: str) -> datetime:
    body = text[:-1] if isinstance(text, str) and text.endswith("Z") else None
    if body is None:
        raise ValueError(f"not a UTC 'Z' timestamp: {text!r}")
    fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in body else "%Y-%m-%dT%H:%M:%S"
    return datetime.strptime(body, fmt).replace(tzinfo=timezone.utc)


def select_evidence(observation: Dict[str, Any], cfg: Dict[str, Any], project
                    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    now_s = float(observation["sim_time_s"])
    now = parse_utc(observation["now"])
    max_age = float(cfg["max_detection_age_s"])
    stats = {"received": 0, "duplicates": 0, "not_due": 0, "stale": 0, "invalid": 0, "capped": 0, "used": 0}
    seen, kept = set(), []
    for d in observation.get("whale_detections") or []:
        stats["received"] += 1
        did = d.get("detection_id") if isinstance(d, dict) else None
        if did is None:
            stats["invalid"] += 1
            continue
        if did in seen:
            stats["duplicates"] += 1
            continue
        seen.add(did)
        try:
            lat, lon, err = float(d["lat"]), float(d["lon"]), float(d["position_error_m"])
            observed_s = now_s + (parse_utc(d["observed_at"]) - now).total_seconds()
            reported_s = now_s + (parse_utc(d["reported_at"]) - now).total_seconds()
        except (KeyError, TypeError, ValueError):
            stats["invalid"] += 1
            continue
        if not all(math.isfinite(v) for v in (lat, lon, err)) or err < 0:
            stats["invalid"] += 1
            continue
        if observed_s > now_s + 1e-6 or reported_s > now_s + 1e-6:
            stats["not_due"] += 1
            continue
        if now_s - observed_s > max_age:
            stats["stale"] += 1
            continue
        kept.append({"detection_id": did, "xy": tuple(project({"lat": lat, "lon": lon})), "position_error_m": err,
                     "observed_s": observed_s, "age_s": now_s - observed_s})
    kept.sort(key=lambda e: (-e["observed_s"], e["detection_id"]))
    cap = int(cfg["max_reports"])
    if len(kept) > cap:
        stats["capped"] = len(kept) - cap
        kept = kept[:cap]
    stats["used"] = len(kept)
    return kept, stats


def sigma_at(ev: Dict[str, Any], t_s: float, cfg: Dict[str, Any]) -> float:
    age = max(0.0, t_s - ev["observed_s"])
    return max(float(cfg["sigma_min_m"]), math.hypot(ev["position_error_m"], float(cfg["drift_mps"]) * age))


def leg_cpa(p0: Tuple[float, float], p1: Tuple[float, float], t0: float, t1: float,
            w: Tuple[float, float], wv: Tuple[float, float] = (0.0, 0.0)) -> Tuple[float, float]:
    """Absolute CPA time and distance between a ship leg p0->p1 over [t0, t1] and a whale at ``w``
    (its position at t0) moving with velocity ``wv``."""
    T = t1 - t0
    if T <= 0:
        return t0, math.hypot(w[0] - p0[0], w[1] - p0[1])
    sv = ((p1[0] - p0[0]) / T, (p1[1] - p0[1]) / T)
    rx, ry = w[0] - p0[0], w[1] - p0[1]
    vx, vy = wv[0] - sv[0], wv[1] - sv[1]
    vv = vx * vx + vy * vy
    tc = 0.0 if vv < 1e-12 else min(T, max(0.0, -(rx * vx + ry * vy) / vv))
    return t0 + tc, math.hypot(rx + vx * tc, ry + vy * tc)


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def corridor_mass(d: float, corridor_m: float, sigma: float) -> float:
    """P(|N(d, sigma^2)| <= corridor_m): mass of the whale's lateral position inside the corridor."""
    return max(0.0, _phi((corridor_m - d) / sigma) - _phi((-corridor_m - d) / sigma))


def path_risk(legs_xy: Sequence[Tuple[Tuple[float, float], Tuple[float, float], float, float, float]],
              evidence: Sequence[Dict[str, Any]], corridor_m: float, service_knots: float,
              cfg: Dict[str, Any], horizon_s: Optional[float] = None) -> Dict[str, Any]:
    """Worst risk over legs (p0, p1, t0, t1, speed_knots) and evidence, truncated at ``horizon_s``."""
    best = {"whale_risk": 0.0, "cpa_m": None, "cpa_time_s": None, "detection_id": None, "leg_index": None,
            "sigma_m": None}
    watch = float(cfg["watch_distance_m"])
    for i, (p0, p1, t0, t1, speed) in enumerate(legs_xy):
        if horizon_s is not None:
            if t0 >= horizon_s:
                break
            if t1 > horizon_s:
                f = (horizon_s - t0) / (t1 - t0)
                p1, t1 = (p0[0] + (p1[0] - p0[0]) * f, p0[1] + (p1[1] - p0[1]) * f), horizon_s
        weight = (speed / service_knots) ** 2 if service_knots > 0 else 0.0
        for ev in evidence:
            tc, d = leg_cpa(p0, p1, t0, t1, ev["xy"])
            if best["cpa_m"] is None or (d < best["cpa_m"] and best["whale_risk"] == 0.0):   # diagnostics
                best.update(cpa_m=d, cpa_time_s=tc, detection_id=ev["detection_id"], leg_index=i)
            if d - corridor_m > watch:
                continue
            sig = sigma_at(ev, tc, cfg)
            risk = corridor_mass(d, corridor_m, sig) * weight
            if risk > best["whale_risk"]:
                best = {"whale_risk": risk, "cpa_m": d, "cpa_time_s": tc, "detection_id": ev["detection_id"],
                        "leg_index": i, "sigma_m": sig}
    return best
