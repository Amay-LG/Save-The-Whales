"""Simulation ship kinematics, route progress, and voyage handling for Person 2."""
import math
from typing import Dict, Any, List, Optional, Tuple
from save_the_whales.shared.geo import project, unproject, distance_m, KNOTS_TO_MPS, MPS_TO_KNOTS

class SimShip:
    def __init__(self,
                 voyage_id: str,
                 route: Dict[str, Any],
                 entry_s: float,
                 complies: bool = True):
        self.voyage_id = voyage_id
        self.source_mmsi = route.get("source_mmsi", "999000001")
        self.ship_id = f"sim:{self.source_mmsi}:{voyage_id}"
        self.entry_s = entry_s
        self.complies = complies

        self.length_m = route.get("length_m", 300.0)
        self.beam_m = route.get("beam_m", 40.0)
        self.service_speed_knots = route.get("service_speed_knots", 18.0)
        self.current_speed_knots = self.service_speed_knots

        # Waypoints: original_route is immutable list of dict Point
        self.original_route: List[Dict[str, float]] = list(route.get("waypoints", []))
        self.original_route_m: List[Tuple[float, float]] = [project(p) for p in self.original_route]

        # Current planned route remaining (excluding current position)
        self.current_route: List[Dict[str, float]] = list(self.original_route[1:])
        self.current_route_m: List[Tuple[float, float]] = list(self.original_route_m[1:])

        # Current state
        self.pos_m = self.original_route_m[0] if self.original_route_m else (0.0, 0.0)
        self.cog_deg = 0.0
        self.baseline_progress_index = 0
        self.active_command: Optional[Dict[str, Any]] = None
        self.latest_revision = 0
        self.last_action_at: Optional[str] = None
        self.incident_budget: Optional[Dict[str, Any]] = None

        # Tracking metrics
        self.distance_traveled_m = 0.0
        self.arrived = False

    def step_motion(self, dt_s: float) -> List[Tuple[Tuple[float, float], Tuple[float, float], float, float, float]]:
        """Advance ship motion for dt_s along current_route_m.
        Splits at waypoints, carrying leftover distance across waypoints.
        Returns list of sub-segments:
        (p0_m, p1_m, speed_knots, t0_frac, t1_frac)
        """
        if self.arrived or not self.current_route_m:
            self.arrived = True
            return []

        sub_segments = []
        speed_mps = self.current_speed_knots * KNOTS_TO_MPS
        remaining_time = dt_s

        while remaining_time > 1e-6 and self.current_route_m:
            target_m = self.current_route_m[0]
            dist_to_target = distance_m(self.pos_m, target_m)

            if dist_to_target < 1e-3:
                # Advance waypoint
                self.pos_m = target_m
                self._pop_waypoint()
                continue

            max_travel = speed_mps * remaining_time
            t0_frac = (dt_s - remaining_time) / dt_s

            if max_travel >= dist_to_target:
                # Reaches and passes waypoint
                p0 = self.pos_m
                p1 = target_m
                time_taken = dist_to_target / speed_mps if speed_mps > 0 else remaining_time
                remaining_time -= time_taken
                t1_frac = (dt_s - remaining_time) / dt_s

                # Update heading
                dx = p1[0] - p0[0]
                dy = p1[1] - p0[1]
                self.cog_deg = math.degrees(math.atan2(dx, dy)) % 360.0

                self.pos_m = p1
                self.distance_traveled_m += dist_to_target
                sub_segments.append((p0, p1, self.current_speed_knots, t0_frac, t1_frac))
                self._pop_waypoint()
            else:
                # Moves partway towards target
                ratio = max_travel / dist_to_target
                p0 = self.pos_m
                p1 = (p0[0] + (target_m[0] - p0[0]) * ratio,
                      p0[1] + (target_m[1] - p0[1]) * ratio)

                # Update heading
                dx = p1[0] - p0[0]
                dy = p1[1] - p0[1]
                self.cog_deg = math.degrees(math.atan2(dx, dy)) % 360.0

                self.pos_m = p1
                self.distance_traveled_m += max_travel
                remaining_time = 0.0
                sub_segments.append((p0, p1, self.current_speed_knots, t0_frac, 1.0))

        if not self.current_route_m:
            self.arrived = True

        return sub_segments

    def _pop_waypoint(self):
        """Pop the first waypoint in current_route."""
        if self.current_route:
            popped_pt = self.current_route.pop(0)
            self.current_route_m.pop(0)
            # Update baseline_progress_index if popped point was on original_route
            for idx, orig_pt in enumerate(self.original_route):
                if idx > self.baseline_progress_index:
                    if abs(orig_pt["lat"] - popped_pt["lat"]) < 1e-6 and \
                       abs(orig_pt["lon"] - popped_pt["lon"]) < 1e-6:
                        self.baseline_progress_index = idx
                        break

    def to_observable(self) -> Dict[str, Any]:
        """Convert current state to ObservableShip dict."""
        pt = unproject(self.pos_m[0], self.pos_m[1])
        return {
            "ship_id": self.ship_id,
            "lat": pt["lat"],
            "lon": pt["lon"],
            "sog_knots": self.current_speed_knots,
            "cog_deg": self.cog_deg,
            "length_m": self.length_m,
            "beam_m": self.beam_m,
            "service_speed_knots": self.service_speed_knots,
            "original_route": list(self.original_route),
            "current_route": list(self.current_route),
            "active_command": self.active_command,
            "baseline_progress_index": self.baseline_progress_index,
            "latest_revision": self.latest_revision,
            "last_action_at": self.last_action_at,
            "incident_budget": self.incident_budget,
        }

