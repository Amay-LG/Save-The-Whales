"""Imperfect whale sightings, the due-report queue, and the advisor's observation snapshot.

Sensors sample surfaced (exposed) whales at fixed virtual times. Detection, position noise and report
delay are keyed draws on (seed, sensor, entity, sample index, whale), so a policy that changes ship
paths cannot shift unrelated draws. Reports get opaque IDs: they identify reports, not animals.

``config["sensors"]``::

    {"sample_dt_s": 60, "max_reports": 50, "max_detection_age_s": 1800,
     "observers": [
       {"sensor_id": "lookout", "kind": "SHIP_LOOKOUT", "range_m": 4000, "p_detect": 0.6,
        "position_error_m": 150, "delay_min_s": 0, "delay_max_s": 60},
       {"sensor_id": "survey", "kind": "AREA", "area": BBox, "p_detect": 0.2,
        "position_error_m": 300, "delay_min_s": 120, "delay_max_s": 300}]}

``SHIP_LOOKOUT`` runs on every active simulated ship; ``AREA`` covers a fixed box (default: region).
"""
from __future__ import annotations

import copy
import math
from typing import Any, Dict, List, Optional, Tuple

from save_the_whales.shared import contracts as C
from save_the_whales.simulation.world import Frame, iso_at, opaque_id, stable_normal, stable_uniform

SHIP_LOOKOUT, AREA = "SHIP_LOOKOUT", "AREA"
SOURCE_BY_KIND = {SHIP_LOOKOUT: "SIMULATED_SHIP_LOOKOUT", AREA: "SIMULATED_AREA_SURVEY"}


class SensorModel:
    def __init__(self, cfg: Dict[str, Any], seed: int, frame: Frame, start_dt):
        self.cfg, self.seed, self.frame, self.start_dt = cfg, seed, frame, start_dt
        self.sample_dt_s = float(cfg.get("sample_dt_s", 60))
        self.max_reports = int(cfg.get("max_reports", 50))
        self.max_age_s = float(cfg.get("max_detection_age_s", 1800))
        self.observers = [dict(o) for o in cfg.get("observers") or []]
        for o in self.observers:
            if o.get("kind") == AREA:
                a = o.get("area") or C.REGION_BBOX
                (x0, y0), (x1, y1) = (frame.xy({"lat": a["lat_min"], "lon": a["lon_min"]}),
                                      frame.xy({"lat": a["lat_max"], "lon": a["lon_max"]}))
                o["_box"] = (min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1))
        self.pending: List[Tuple[float, str, float, Dict[str, Any]]] = []   # (reported_s, id, observed_s, det)
        self.retained: List[Tuple[float, str, Dict[str, Any]]] = []         # (observed_s, id, det)
        self.counters = {"reports_generated": 0, "reports_released": 0, "dropped_stale": 0,
                         "dropped_cap": 0}

    def next_sample_s(self, t: float) -> float:
        return (math.floor(t / self.sample_dt_s + 1e-9) + 1) * self.sample_dt_s

    def is_sample_time(self, t: float) -> bool:
        k = round(t / self.sample_dt_s)
        return abs(k * self.sample_dt_s - t) < 1e-6

    def sample(self, t: float, ships: List[Any], whales: List[Any]) -> None:
        k = int(round(t / self.sample_dt_s))
        states = [(w, w.state_at(t)) for w in whales]
        for o in self.observers:
            sid, kind = str(o.get("sensor_id", o.get("kind"))), o.get("kind")
            if kind == SHIP_LOOKOUT:
                rng = float(o.get("range_m", 0))
                for ship in ships:
                    for w, (x, y, exposed) in states:
                        if exposed and math.hypot(x - ship.xy[0], y - ship.xy[1]) <= rng:
                            self._maybe_report(o, sid, ship.voyage_id, k, t, w, x, y)
            elif kind == AREA:
                x0, x1, y0, y1 = o["_box"]
                for w, (x, y, exposed) in states:
                    if exposed and x0 <= x <= x1 and y0 <= y <= y1:
                        self._maybe_report(o, sid, "area", k, t, w, x, y)

    def _maybe_report(self, o, sid, entity, k, t, whale, x, y) -> None:
        keys = (self.seed, "sensor", sid, entity, k, whale.whale_id)
        if stable_uniform(*keys, "detect") >= float(o.get("p_detect", 0)):
            return
        sigma = float(o.get("position_error_m", 0))
        nx, ny = x + sigma * stable_normal(*keys, "nx"), y + sigma * stable_normal(*keys, "ny")
        lo, hi = float(o.get("delay_min_s", 0)), float(o.get("delay_max_s", 0))
        reported_s = t + lo + (hi - lo) * stable_uniform(*keys, "delay")
        p = self.frame.point(nx, ny)
        det = {"detection_id": opaque_id("rpt", *keys), "lat": p["lat"], "lon": p["lon"],
               "position_error_m": sigma, "observed_at": iso_at(self.start_dt, t),
               "reported_at": iso_at(self.start_dt, reported_s),
               "source": SOURCE_BY_KIND.get(o.get("kind"), "SIMULATED")}
        if whale.species:
            det["species"] = whale.species
        self.pending.append((reported_s, det["detection_id"], t, det))
        self.counters["reports_generated"] += 1

    def next_release_s(self) -> Optional[float]:
        return min((r[0] for r in self.pending), default=None)

    def release(self, t: float) -> None:
        """Move reports whose ``reported_at`` is due into the retained evidence."""
        due = sorted(r for r in self.pending if r[0] <= t + 1e-9)
        if not due:
            return
        self.pending = [r for r in self.pending if r[0] > t + 1e-9]
        for _, rid, observed_s, det in due:
            self.retained.append((observed_s, rid, det))
            self.counters["reports_released"] += 1

    def evidence(self, t: float) -> List[Dict[str, Any]]:
        """Due reports within the age limit, newest ``max_reports`` kept (older ones dropped for good)."""
        fresh = [r for r in self.retained if t - r[0] <= self.max_age_s]
        self.counters["dropped_stale"] += len(self.retained) - len(fresh)
        fresh.sort(key=lambda r: (r[0], r[1]))
        if len(fresh) > self.max_reports:
            self.counters["dropped_cap"] += len(fresh) - self.max_reports
            fresh = fresh[-self.max_reports:]
        self.retained = fresh
        return [copy.deepcopy(r[2]) for r in fresh]


def build_observation(*, now: str, sim_time_s: float, decision_id: str, decision_dt_s: float,
                      bbox: Dict[str, float], static: Dict[str, Any], ships: List[Any],
                      detections: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A fresh, JSON-only v2 observation. No World handle, whale truth or future reports."""
    return {
        "schema_version": C.SCHEMA_VERSION,
        "now": now,
        "decision_interval_min": decision_dt_s / 60.0,
        "bbox": dict(bbox),
        "static": copy.deepcopy(static),
        "ships": [s.observable() for s in ships],
        "whale_detections": detections,
        "decision_id": decision_id,
        "sim_time_s": sim_time_s,
    }
