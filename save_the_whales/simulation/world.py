"""Simulation world state, virtual clock, scenario initialization for Person 2."""
import copy
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta
from save_the_whales.shared.contracts import RunInputs, Point, ObservableShip, WhaleDetection, Observation
from save_the_whales.shared.geo import project, unproject, distance_m, KNOTS_TO_MPS
from save_the_whales.simulation.ships import SimShip
from save_the_whales.simulation.whales import WhalePopulation

class SimWorld:
    def __init__(self, run_inputs: Dict[str, Any]):
        # Deep-copy mutable inputs to prevent leaking outside state
        self.inputs = copy.deepcopy(run_inputs)
        self.config = self.inputs.get("config", {})
        self.scenario = self.inputs.get("scenario", {})
        self.route_bundle = self.inputs.get("route_bundle", {})
        self.land = self.inputs.get("land", {})
        self.weather = self.scenario.get("weather", {})

        # Setup virtual clock
        start_at_iso = self.scenario.get("start_at", "2026-07-01T12:00:00Z")
        self.start_dt = datetime.fromisoformat(start_at_iso.replace("Z", "+00:00"))
        self.sim_time_s = 0.0

        # Index routes in route bundle by route_id
        self.routes_by_id: Dict[str, Dict[str, Any]] = {}
        for r in self.route_bundle.get("routes", []):
            self.routes_by_id[r["route_id"]] = r

        # Initialize whales
        self.whales = WhalePopulation(self.scenario.get("whales", []), self.scenario.get("seed", 42))

        # Voyages scheduled to spawn: list of dict
        self.pending_voyages: List[Dict[str, Any]] = list(self.scenario.get("voyages", []))
        self.active_ships: Dict[str, SimShip] = {}
        self.arrived_ships: List[SimShip] = []

    def get_current_iso(self) -> str:
        cur_dt = self.start_dt + timedelta(seconds=self.sim_time_s)
        return cur_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def spawn_due(self) -> List[SimShip]:
        """Spawn voyages due at current sim_time_s."""
        newly_spawned = []
        still_pending = []
        for v in self.pending_voyages:
            entry_s = v.get("entry_s", 0.0)
            if self.sim_time_s >= entry_s:
                route_id = v.get("route_id")
                if route_id in self.routes_by_id:
                    route_data = self.routes_by_id[route_id]
                    ship = SimShip(
                        voyage_id=v.get("voyage_id", f"v_{len(self.active_ships)}"),
                        route=route_data,
                        entry_s=entry_s
                    )
                    self.active_ships[ship.ship_id] = ship
                    newly_spawned.append(ship)
            else:
                still_pending.append(v)
        self.pending_voyages = still_pending
        return newly_spawned

    def build_observation(self,
                          reported_detections: List[Dict[str, Any]],
                          decision_id: str,
                          decision_interval_min: float = 1.0) -> Dict[str, Any]:
        """Assemble native 2.0 contract Observation dictionary for advisor.
        Does NOT expose hidden world, true whale state, or future detections.
        """
        observable_ships = [s.to_observable() for s in self.active_ships.values() if not s.arrived]
        bbox = self.config.get("bbox", {
            "lat_min": 33.8, "lat_max": 34.6, "lon_min": -120.5, "lon_max": -119.0
        })

        return {
            "schema_version": "2.0",
            "decision_id": decision_id,
            "now": self.get_current_iso(),
            "sim_time_s": self.sim_time_s,
            "decision_interval_min": decision_interval_min,
            "bbox": bbox,
            "static": {
                "land": self.land,
                "weather": self.weather
            },
            "ships": observable_ships,
            "whale_detections": reported_detections
        }

