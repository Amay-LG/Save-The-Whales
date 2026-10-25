"""Seeded, ship-independent whales: piecewise-linear movement plus a surface/deep exposure schedule.

Each whale's whole trajectory is fixed at initialization from its scenario spec and a private named
random stream, using physical-time schedules. Whales never read vessel positions, so every policy
sees the same whales. Private IDs, positions and exposure stay inside the simulator; only sensors and
truth metrics read them.

Scenario whale spec (all fields optional except ``whale_id`` and ``start``)::

    {"whale_id": "w-cross", "start": {"lat", "lon"}, "species": "blue",
     # movement, one of:
     "waypoints": [{"lat", "lon"}, ...], "speed_mps": 1.0,            # follow, then hold position
     "heading_deg": 180, "speed_mps": 1.0,                           # compass heading (0 = north)
         "turns": [{"at_s": 900, "heading_deg": 90, "speed_mps": 0.5}],
     "random_walk": true, "area": BBox,                              # seeded turns, reflect at area
     # exposure (strike-relevant surface time), one of:
     "exposure": [{"from_s": 0, "until_s": 600, "exposed": true}],    # gaps are not exposed
     "surface_s": 120, "deep_s": 480, "phase_s": 0}                  # repeating cycle

Defaults for speed, turn cadence and the dive cycle come from ``config["simulation"]``.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from typing import Any, Callable, Dict, List, Optional, Tuple

from save_the_whales.shared import contracts as C
from save_the_whales.simulation.world import Frame, compass_deg, named_stream

INF = math.inf
EPS = 1e-9


class WhalePiece:
    __slots__ = ("ta", "tb", "x", "y", "vx", "vy", "exposed")

    def __init__(self, ta, tb, x, y, vx, vy, exposed):
        self.ta, self.tb, self.x, self.y, self.vx, self.vy, self.exposed = ta, tb, x, y, vx, vy, exposed


def _vel(heading_deg: float, speed: float) -> Tuple[float, float]:
    h = math.radians(heading_deg)
    return speed * math.sin(h), speed * math.cos(h)


class Whale:
    def __init__(self, spec: Dict[str, Any], seed: int, horizon_s: float, frame: Frame, sim_cfg: Dict[str, Any]):
        self.whale_id = str(spec["whale_id"])
        self.species = spec.get("species")
        rng = named_stream(seed, "whale", self.whale_id)
        x, y = frame.xy(spec["start"])
        self._horizon = float(horizon_s)
        self.segs: List[Tuple[float, float, float, float, float, float]] = []   # t0, t1, x0, y0, vx, vy
        self._area = None
        area = spec.get("area") or (C.REGION_BBOX if spec.get("random_walk") else None)
        if area:
            (ax0, ay0), (ax1, ay1) = (frame.xy({"lat": area["lat_min"], "lon": area["lon_min"]}),
                                      frame.xy({"lat": area["lat_max"], "lon": area["lon_max"]}))
            box = (min(ax0, ax1), max(ax0, ax1), min(ay0, ay1), max(ay0, ay1))
            if box[0] <= x <= box[1] and box[2] <= y <= box[3]:
                self._area = box
        if spec.get("waypoints"):
            self._build_waypoints(x, y, spec, frame, float(spec.get("speed_mps", sim_cfg.get("whale_speed_mps", 1.0))))
        else:
            self._build_heading(x, y, spec, rng, horizon_s, sim_cfg)
        self._seg_t0 = [s[0] for s in self.segs]
        self.exps = self._build_exposure(spec, rng, horizon_s, sim_cfg)
        self._exp_t0 = [e[0] for e in self.exps]

    # --- movement ----------------------------------------------------------------------------------
    def _build_waypoints(self, x: float, y: float, spec: Dict[str, Any], frame: Frame, speed: float) -> None:
        t = 0.0
        for p in spec["waypoints"]:
            nx, ny = frame.xy(p)
            d = math.hypot(nx - x, ny - y)
            if d <= EPS or speed <= 0:
                continue
            dt = d / speed
            self.segs.append((t, t + dt, x, y, (nx - x) / dt, (ny - y) / dt))
            t, x, y = t + dt, nx, ny
        self.segs.append((t, INF, x, y, 0.0, 0.0))

    def _build_heading(self, x: float, y: float, spec: Dict[str, Any], rng, horizon_s: float,
                       cfg: Dict[str, Any]) -> None:
        if spec.get("random_walk"):
            lo, hi = cfg.get("whale_speed_range_mps", [0.5, 2.0])
            speed = float(spec.get("speed_mps", rng.uniform(lo, hi)))
            heading = float(spec.get("heading_deg", rng.uniform(0.0, 360.0)))
            every = float(cfg.get("whale_turn_every_s", 600))
            sd = float(cfg.get("whale_turn_sd_deg", 30))
            turns, t = [], every
            while t < horizon_s:
                turns.append({"at_s": t, "turn_deg": rng.gauss(0.0, sd), "speed_mps": rng.uniform(lo, hi)})
                t += every
        else:
            speed = float(spec.get("speed_mps", 0.0))
            heading = float(spec.get("heading_deg", 0.0))
            turns = sorted((dict(tr) for tr in spec.get("turns") or []), key=lambda tr: float(tr["at_s"]))
        vx, vy = _vel(heading, speed)
        t = 0.0
        for tr in turns:
            tc = max(t, float(tr["at_s"]))
            if tc > t:
                x, y, vx, vy = self._run(t, tc, x, y, vx, vy)
                t = tc
            heading = compass_deg(vx, vy) if (vx or vy) else heading
            heading = heading + float(tr["turn_deg"]) if "turn_deg" in tr else float(tr.get("heading_deg", heading))
            speed = float(tr.get("speed_mps", math.hypot(vx, vy)))
            vx, vy = _vel(heading, speed)
        end = max(t, self._horizon)
        x, y, vx, vy = self._run(t, end, x, y, vx, vy)
        self.segs.append((end, INF, x, y, 0.0, 0.0))   # nothing is evaluated past the horizon

    def _run(self, ta: float, tb: float, x: float, y: float, vx: float, vy: float):
        """Append straight segments over finite [ta, tb], reflecting at the area box. Returns the end state."""
        t = ta
        for _ in range(10000):
            hit = INF
            if self._area and (vx or vy):
                x0, x1, y0, y1 = self._area
                for pos, vel, lo, hi in ((x, vx, x0, x1), (y, vy, y0, y1)):
                    if vel > 0:
                        hit = min(hit, (hi - pos) / vel)
                    elif vel < 0:
                        hit = min(hit, (lo - pos) / vel)
                hit = max(hit, 0.0)
            if t + hit >= tb:
                if tb > t:
                    self.segs.append((t, tb, x, y, vx, vy))
                return x + vx * (tb - t), y + vy * (tb - t), vx, vy
            if hit > EPS:
                self.segs.append((t, t + hit, x, y, vx, vy))
            x, y, t = x + vx * hit, y + vy * hit, t + hit
            x0, x1, y0, y1 = self._area
            if (x >= x1 - EPS and vx > 0) or (x <= x0 + EPS and vx < 0):
                vx = -vx
            if (y >= y1 - EPS and vy > 0) or (y <= y0 + EPS and vy < 0):
                vy = -vy
        self.segs.append((t, tb, x, y, 0.0, 0.0))   # pathological bouncing: hold position
        return x, y, 0.0, 0.0

    # --- exposure ----------------------------------------------------------------------------------
    @staticmethod
    def _build_exposure(spec: Dict[str, Any], rng, horizon_s: float, cfg: Dict[str, Any]
                        ) -> List[Tuple[float, float, bool]]:
        if spec.get("exposure") is not None:
            out: List[Tuple[float, float, bool]] = []
            t = 0.0
            for e in sorted(spec["exposure"], key=lambda e: float(e["from_s"])):
                a, b = max(t, float(e["from_s"])), float(e["until_s"])
                if a > t:
                    out.append((t, a, False))
                if b > a:
                    out.append((a, b, bool(e["exposed"])))
                    t = b
            out.append((t, INF, False))
            return out
        surface = float(spec.get("surface_s", cfg.get("whale_surface_s", 120)))
        deep = float(spec.get("deep_s", cfg.get("whale_deep_s", 480)))
        if surface <= 0 or deep <= 0:
            return [(0.0, INF, surface > 0)]
        cycle = surface + deep
        phase = float(spec["phase_s"]) % cycle if "phase_s" in spec else rng.uniform(0.0, cycle)
        out, t = [], 0.0
        exposed = phase < surface
        nxt = (surface - phase) if exposed else (cycle - phase)
        while t < horizon_s:
            out.append((t, t + nxt, exposed))
            t += nxt
            exposed = not exposed
            nxt = surface if exposed else deep
        out.append((t, INF, exposed))
        return out

    # --- queries -----------------------------------------------------------------------------------
    def state_at(self, t: float) -> Tuple[float, float, bool]:
        s = self.segs[max(0, bisect_right(self._seg_t0, t) - 1)]
        e = self.exps[max(0, bisect_right(self._exp_t0, t) - 1)]
        return s[2] + s[4] * (t - s[0]), s[3] + s[5] * (t - s[0]), e[2]

    def pieces(self, t0: float, t1: float) -> List[WhalePiece]:
        """Linear pieces over [t0, t1], split at turns and exposure changes."""
        out: List[WhalePiece] = []
        si = max(0, bisect_right(self._seg_t0, t0) - 1)
        ei = max(0, bisect_right(self._exp_t0, t0) - 1)
        t = t0
        while t < t1 - EPS:
            s, e = self.segs[si], self.exps[ei]
            tb = min(t1, s[1], e[1])
            out.append(WhalePiece(t, tb, s[2] + s[4] * (t - s[0]), s[3] + s[5] * (t - s[0]), s[4], s[5], e[2]))
            t = tb
            if t >= s[1] - EPS and si + 1 < len(self.segs):
                si += 1
            if t >= e[1] - EPS and ei + 1 < len(self.exps):
                ei += 1
        return out

    def analyst_truth(self, t: float, frame: Frame) -> Dict[str, Any]:
        x, y, exposed = self.state_at(t)
        p = frame.point(x, y)
        return {"whale_id": self.whale_id, "lat": p["lat"], "lon": p["lon"], "exposed": exposed}


def make_background_whales(count: int, seed: int, area: Dict[str, float], *,
                           is_water: Optional[Callable[[Dict[str, float]], bool]] = None,
                           prefix: str = "bg", max_tries: int = 100) -> List[Dict[str, Any]]:
    """Seeded random-walk whale specs inside ``area`` (a BBox). ``is_water`` can reject land starts,
    e.g. ``lambda p: geo.check_water_path([p], land, 0.0)["ok"]``."""
    out = []
    for i in range(count):
        wid = f"{prefix}-{i:02d}"
        rng = named_stream(seed, "background-whale", wid)
        start = None
        for _ in range(max_tries):
            p = {"lat": rng.uniform(area["lat_min"], area["lat_max"]),
                 "lon": rng.uniform(area["lon_min"], area["lon_max"])}
            if is_water is None or is_water(p):
                start = p
                break
        if start is not None:
            out.append({"whale_id": wid, "start": start, "random_walk": True, "area": dict(area)})
    return out
