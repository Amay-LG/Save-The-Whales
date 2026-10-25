"""Truth-based outcome metrics: swept contacts, encounter episodes, a smooth exposure proxy, and voyage
delay/distance. These read hidden whale truth and never feed the advisor.

Geometry (declared conservative model): the ship is its enclosing circle, radius 0.5*hypot(L, B); the
whale is a circle of ``whale_body_radius_m``. Body clearance = centre distance - both radii.

- Episodes (fixed rules): enter when body clearance <= ``encounter_enter_m`` (500); exit only after
  clearance > ``encounter_exit_m`` (750) continuously for ``encounter_exit_dwell_s`` (120) physical
  seconds. Each entry is one close approach.
- Contact: body clearance <= 0 while the whale is exposed (surface). At most one per episode.
- Both are evaluated continuously on every linear sub-interval between motion breakpoints (waypoints,
  exposure changes, turns, commands): the relative position is r(s) = r0 + v*s, so the times when
  |r| <= R solve a quadratic and an encounter between tick endpoints is never missed. A contact on a
  shared sub-interval boundary is counted once.
- Exposure proxy (smooth diagnostic, not expected deaths): integral of
  exp(-d^2 / (2 sigma^2)) * |v_rel| / sigma * (speed / service_speed)^2 dt while exposed, with
  ``sigma_eval_m`` (300) independent of sensor error. Closed form per sub-interval via erf.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Tuple

EPS = 1e-9
SQRT_HALF_PI = math.sqrt(math.pi / 2.0)


def inside_interval(rx: float, ry: float, vx: float, vy: float, T: float, R: float
                    ) -> Optional[Tuple[float, float]]:
    """Sub-interval of [0, T] where |r0 + v*s| <= R (a single interval: |r|^2 is convex), or None."""
    a = vx * vx + vy * vy
    c = rx * rx + ry * ry - R * R
    if a < 1e-18:
        return (0.0, T) if c <= EPS else None
    b = 2.0 * (rx * vx + ry * vy)
    disc = b * b - 4.0 * a * c
    if disc < 0:
        return None
    sq = math.sqrt(disc)
    lo, hi = max(0.0, (-b - sq) / (2.0 * a)), min(T, (-b + sq) / (2.0 * a))
    return (lo, hi) if lo <= hi + EPS else None


def gaussian_proxy(rx: float, ry: float, vx: float, vy: float, T: float, sigma: float) -> float:
    """Integral over [0, T] of exp(-|r0 + v s|^2 / (2 sigma^2)) * |v| / sigma ds (dimensionless)."""
    speed2 = vx * vx + vy * vy
    if speed2 < 1e-18 or T <= 0:
        return 0.0
    speed = math.sqrt(speed2)
    s_star = -(rx * vx + ry * vy) / speed2
    dmin2 = max(0.0, rx * rx + ry * ry - (rx * vx + ry * vy) ** 2 / speed2)
    k = speed / (sigma * math.sqrt(2.0))
    return math.exp(-dmin2 / (2.0 * sigma * sigma)) * SQRT_HALF_PI * (math.erf(k * (T - s_star)) - math.erf(k * -s_star))


class _Episode:
    __slots__ = ("inside", "contact", "outside_since", "entered_s")

    def __init__(self):
        self.inside, self.contact, self.outside_since, self.entered_s = False, False, None, None


class EncounterTracker:
    def __init__(self, cfg: Dict[str, Any], emit: Callable[[float, str, Optional[str], Dict[str, Any]], None]):
        self.enter_m = float(cfg.get("encounter_enter_m", 500))
        self.exit_m = float(cfg.get("encounter_exit_m", 750))
        self.dwell_s = float(cfg.get("encounter_exit_dwell_s", 120))
        self.whale_r = float(cfg.get("whale_body_radius_m", 12.5))
        self.sigma = float(cfg.get("sigma_eval_m", 300))
        self.emit = emit
        self.episodes: Dict[Tuple[str, str], _Episode] = {}
        self.close_approaches = 0
        self.contacts = 0
        self.proxy_total = 0.0
        self.proxy_by_ship: Dict[str, float] = {}
        self.contacts_by_ship: Dict[str, int] = {}
        self.approaches_by_ship: Dict[str, int] = {}

    def evaluate(self, ship, ship_pieces: List[Any], whale_pieces: List[Tuple[Any, List[Any]]]) -> None:
        """Evaluate one ship against every whale from merged linear pieces. ``whale_pieces`` holds
        (whale, pieces) covering the same window, which starts at ``ship_pieces[0].ta``."""
        if not ship_pieces:
            return
        r_sum = 0.5 * math.hypot(ship.length_m, ship.beam_m) + self.whale_r
        far = max(r_sum + self.exit_m, 8.0 * self.sigma)
        service = ship.service_speed_knots
        for whale, wps in whale_pieces:
            key = (ship.ship_id, whale.whale_id)
            ep = self.episodes.get(key)
            if (ep is None or not ep.inside) and self._far_apart(ship_pieces, wps, far):
                continue
            if ep is None:
                ep = self.episodes[key] = _Episode()
            i = j = 0
            while i < len(ship_pieces) and j < len(wps):
                sp, wp = ship_pieces[i], wps[j]
                ta, tb = max(sp.ta, wp.ta), min(sp.tb, wp.tb)
                if tb > ta + EPS:
                    sx, sy = sp.x + sp.vx * (ta - sp.ta), sp.y + sp.vy * (ta - sp.ta)
                    wx, wy = wp.x + wp.vx * (ta - wp.ta), wp.y + wp.vy * (ta - wp.ta)
                    rx, ry, vx, vy = wx - sx, wy - sy, wp.vx - sp.vx, wp.vy - sp.vy
                    if wp.exposed and service > 0:
                        val = (sp.speed_knots / service) ** 2 * gaussian_proxy(rx, ry, vx, vy, tb - ta, self.sigma)
                        self.proxy_total += val
                        self.proxy_by_ship[ship.ship_id] = self.proxy_by_ship.get(ship.ship_id, 0.0) + val
                    self._episode_step(ep, ship.ship_id, ta, tb, rx, ry, vx, vy, wp.exposed, r_sum)
                if sp.tb <= wp.tb:
                    i += 1
                else:
                    j += 1

    @staticmethod
    def _far_apart(ship_pieces, wps, far: float) -> bool:
        """Conservative prefilter: a lower bound on the distance over the window exceeds ``far``."""
        if not wps:
            return True
        sp0, wp0 = ship_pieces[0], wps[0]
        T = ship_pieces[-1].tb - sp0.ta
        d0 = math.hypot(wp0.x - sp0.x, wp0.y - sp0.y)
        vmax = max(math.hypot(p.vx, p.vy) for p in ship_pieces) + max(math.hypot(p.vx, p.vy) for p in wps)
        return d0 - vmax * T > far

    def _episode_step(self, ep: _Episode, ship_id: str, ta: float, tb: float, rx: float, ry: float,
                      vx: float, vy: float, exposed: bool, r_sum: float) -> None:
        """Advance one episode over a linear sub-interval. Each pass returns or changes state; within
        one sub-interval at most enter/exit/enter/exit can occur, so the loop is bounded."""
        t = ta
        for _ in range(8):
            ox, oy, T = rx + vx * (t - ta), ry + vy * (t - ta), tb - t
            if not ep.inside:
                iv = inside_interval(ox, oy, vx, vy, T, r_sum + self.enter_m)
                if iv is None:
                    return
                t += iv[0]
                ep.inside, ep.contact, ep.outside_since, ep.entered_s = True, False, None, t
                self.close_approaches += 1
                self.approaches_by_ship[ship_id] = self.approaches_by_ship.get(ship_id, 0) + 1
                self.emit(t, "ENCOUNTER_START", ship_id, {"enter_body_clearance_m": self.enter_m})
                continue
            # Inside an episode: find the exit time (if any), then look for a contact before it.
            ive = inside_interval(ox, oy, vx, vy, T, r_sum + self.exit_m)
            t_exit = None
            if ive is None:                                   # beyond the exit radius throughout
                since = t if ep.outside_since is None else ep.outside_since
                if tb - since >= self.dwell_s - EPS:
                    t_exit = since + self.dwell_s
                else:
                    ep.outside_since = since
            else:
                a, b = t + ive[0], t + ive[1]
                if a > t + EPS:                               # outside on [t, a)
                    since = t if ep.outside_since is None else ep.outside_since
                    if a - since >= self.dwell_s - EPS:
                        t_exit = since + self.dwell_s
                if t_exit is None:                            # back inside on [a, b]
                    ep.outside_since = b if b < tb - EPS else None
                    if ep.outside_since is not None and tb - b >= self.dwell_s - EPS:
                        t_exit = b + self.dwell_s
            if exposed and not ep.contact:
                horizon = (t_exit if t_exit is not None else tb) - t
                ivc = inside_interval(ox, oy, vx, vy, max(0.0, horizon), r_sum)
                if ivc is not None:
                    ep.contact = True
                    self.contacts += 1
                    self.contacts_by_ship[ship_id] = self.contacts_by_ship.get(ship_id, 0) + 1
                    self.emit(t + ivc[0], "CONTACT", ship_id, {"model": "enclosing_circles"})
            if t_exit is None:
                return
            ep.inside, ep.outside_since = False, None
            self.emit(t_exit, "ENCOUNTER_END", ship_id, {"exit_dwell_s": self.dwell_s})
            t = t_exit
            if t >= tb - EPS:
                return

    def close_ship(self, ship_id: str, t: float) -> int:
        """Censor open episodes for a ship leaving the world (complete/failed). Returns the count."""
        n = 0
        for (sid, _), ep in self.episodes.items():
            if sid == ship_id and ep.inside:
                ep.inside = False
                n += 1
        if n:
            self.emit(t, "ENCOUNTER_CENSORED", ship_id, {"open_episodes": n})
        return n

    def open_episodes(self) -> int:
        return sum(1 for ep in self.episodes.values() if ep.inside)


def voyage_metrics(ship, weather_s: Optional[float], proxy: float, contacts: int, approaches: int,
                   knots_to_mps: Callable[[float], float]) -> Dict[str, Any]:
    """Per-voyage diagnostics against the matched original-route reference (baseline at service
    speed). Deltas are null unless the voyage completed."""
    ref_d = ship.reference_distance_m
    ref_t = ref_d / knots_to_mps(ship.service_speed_knots) if ship.service_speed_knots > 0 else None
    done = ship.status == "COMPLETE"
    duration = (ship.completed_s - ship.entry_s) if done else None
    return {
        "voyage_id": ship.voyage_id, "ship_id": ship.ship_id, "route_id": ship.route_id,
        "source_mmsi": ship.source_mmsi, "status": ship.status, "fail_reason": ship.fail_reason,
        "entry_s": ship.entry_s, "completed_s": ship.completed_s,
        "reference_distance_m": ref_d, "reference_duration_s": ref_t,
        "distance_m": ship.distance_m, "duration_s": duration,
        "added_delay_s": (duration - ref_t) if done and ref_t is not None else None,
        "extra_distance_m": (ship.distance_m - ref_d) if done else None,
        "censored": not done,
        "baseline_progress_index": ship.progress,
        "weather_exposure_s": weather_s,
        "whale_exposure_proxy": proxy, "modeled_contacts": contacts, "close_approaches": approaches,
    }
