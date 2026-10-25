"""Seeded whale movement and exposure states for Person 2."""
import math
import random
from typing import Dict, Any, List, Optional, Tuple
from save_the_whales.shared.geo import project, unproject, distance_m, KNOTS_TO_MPS

class WhaleAgent:
    def __init__(self,
                 whale_id: str,
                 initial_pos_m: Tuple[float, float],
                 seed: int,
                 speed_knots: float = 2.5,
                 heading_deg: float = 0.0,
                 waypoints_m: Optional[List[Tuple[float, float]]] = None,
                 exposure_schedule: Optional[List[Dict[str, Any]]] = None):
        self.whale_id = whale_id
        self.pos_m = initial_pos_m
        self.rng = random.Random(f"{seed}:whale:{whale_id}")
        self.speed_mps = speed_knots * KNOTS_TO_MPS
        self.heading_rad = math.radians(heading_deg)
        self.waypoints_m = waypoints_m or []
        self.waypoint_idx = 0
        self.exposure_schedule = exposure_schedule or []
        
        # State: exposure in [0, 1] (1.0 = surface, 0.5 = shallow, 0.0 = deep)
        self.exposure = 1.0
        # If no explicit schedule, use a semi-Markov dive cycle:
        # e.g., 180s surface (1.0), 120s shallow (0.5), 600s deep (0.0)
        self.dive_cycle = [
            (180.0, 1.0),
            (120.0, 0.5),
            (600.0, 0.0),
        ]
        self.dive_phase_idx = self.rng.randint(0, len(self.dive_cycle) - 1)
        self.phase_time_remaining = self.rng.uniform(10.0, self.dive_cycle[self.dive_phase_idx][0])

    def step(self, dt_s: float) -> Tuple[Tuple[float, float], Tuple[float, float], float]:
        """Step whale motion and exposure for dt_s.
        Returns: (p0_m, p1_m, exposure)
        """
        p0 = self.pos_m
        
        # Update dive exposure
        self.phase_time_remaining -= dt_s
        if self.phase_time_remaining <= 0:
            self.dive_phase_idx = (self.dive_phase_idx + 1) % len(self.dive_cycle)
            duration, exp_val = self.dive_cycle[self.dive_phase_idx]
            self.phase_time_remaining = duration + self.rng.uniform(-30.0, 30.0)
            self.exposure = exp_val
        else:
            self.exposure = self.dive_cycle[self.dive_phase_idx][1]

        # Motion update
        if self.waypoints_m and self.waypoint_idx < len(self.waypoints_m):
            target = self.waypoints_m[self.waypoint_idx]
            d = distance_m(self.pos_m, target)
            step_dist = self.speed_mps * dt_s
            if step_dist >= d:
                self.pos_m = target
                self.waypoint_idx += 1
            else:
                ratio = step_dist / d
                new_x = self.pos_m[0] + (target[0] - self.pos_m[0]) * ratio
                new_y = self.pos_m[1] + (target[1] - self.pos_m[1]) * ratio
                self.pos_m = (new_x, new_y)
        else:
            # Correlated random walk or fixed heading with small noise
            heading_jitter = self.rng.gauss(0.0, 0.05)
            self.heading_rad += heading_jitter
            dx = self.speed_mps * dt_s * math.sin(self.heading_rad)
            dy = self.speed_mps * dt_s * math.cos(self.heading_rad)
            self.pos_m = (self.pos_m[0] + dx, self.pos_m[1] + dy)

        p1 = self.pos_m
        return p0, p1, self.exposure

class WhalePopulation:
    def __init__(self, scenario_whales: List[Dict[str, Any]], seed: int):
        self.whales: List[WhaleAgent] = []
        for idx, w in enumerate(scenario_whales):
            wid = w.get("whale_id", f"whale_{idx}")
            init_pt = w.get("initial_point", {"lat": 34.2, "lon": -119.75})
            pos_m = project(init_pt)
            spd = w.get("speed_knots", 2.5)
            hdg = w.get("heading_deg", 0.0)
            wps_dict = w.get("waypoints", [])
            wps_m = [project(p) for p in wps_dict] if wps_dict else []
            schedule = w.get("exposure_schedule", [])
            agent = WhaleAgent(
                whale_id=wid,
                initial_pos_m=pos_m,
                seed=seed + idx,
                speed_knots=spd,
                heading_deg=hdg,
                waypoints_m=wps_m,
                exposure_schedule=schedule
            )
            self.whales.append(agent)

    def step(self, dt_s: float) -> List[Tuple[str, Tuple[float, float], Tuple[float, float], float]]:
        """Advance all whales. Returns list of (whale_id, p0_m, p1_m, exposure)."""
        movements = []
        for w in self.whales:
            p0, p1, exp = w.step(dt_s)
            movements.append((w.whale_id, p0, p1, exp))
        return movements

