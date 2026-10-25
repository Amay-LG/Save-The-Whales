"""Unit and integration tests for Person 2 (Simulation module)."""
import unittest
from save_the_whales.shared.contracts import Point
from save_the_whales.shared.geo import project, unproject
from save_the_whales.simulation.world import SimWorld
from save_the_whales.simulation.ships import SimShip
from save_the_whales.simulation.whales import WhalePopulation
from save_the_whales.simulation.sensors import SensorSuite
from save_the_whales.simulation.commands import CommandManager
from save_the_whales.simulation.metrics import TruthEvaluator
from save_the_whales.simulation.engine import run_scenario

class TestSimulation(unittest.TestCase):
    def setUp(self):
        # Common fixture recipe from Tasks.md
        self.fixture_route = {
            "route_id": "r_001",
            "source_mmsi": "999000001",
            "service_speed_knots": 18.0,
            "length_m": 300.0,
            "beam_m": 40.0,
            "waypoints": [
                {"lat": 34.2, "lon": -120.1},
                {"lat": 34.2, "lon": -120.075},
                {"lat": 34.2, "lon": -120.05},
                {"lat": 34.2, "lon": -120.025}
            ]
        }
        self.inputs = {
            "run_id": "test_run_01",
            "fixture_only": True,
            "config": {
                "simulation": {
                    "dt_s": 30.0,
                    "decision_dt_s": 60.0,
                    "duration_s": 1200.0,
                    "drain_max_s": 600.0,
                    "wall_limit_s": 10.0
                },
                "sensors": {
                    "r_visual_km": 5.0,
                    "p_detect": 1.0,
                    "sigma_obs_m": 100.0,
                    "latency_max_min": 0
                },
                "response": {
                    "response_lag_s": 0.0,
                    "compliance": 1.0
                },
                "metrics": {
                    "sigma_eval_m": 300.0
                }
            },
            "route_bundle": {
                "schema_version": "2.0",
                "bundle_id": "b_001",
                "routes": [self.fixture_route]
            },
            "land": {
                "map_id": "toy_land",
                "rings": []
            },
            "scenario": {
                "scenario_id": "test_scenario",
                "seed": 42,
                "start_at": "2026-07-01T12:00:00Z",
                "duration_s": 1200.0,
                "voyages": [
                    {"voyage_id": "voyage_0", "route_id": "r_001", "entry_s": 0.0}
                ],
                "whales": [
                    {
                        "whale_id": "w_0",
                        "initial_point": {"lat": 34.2, "lon": -120.065},
                        "speed_knots": 0.0
                    }
                ],
                "weather": {
                    "source": "SIMULATED",
                    "coverage_start_s": 0.0,
                    "coverage_end_s": 12600.0,
                    "zones": []
                }
            }
        }

    def test_ship_waypoint_motion(self):
        ship = SimShip("voyage_0", self.fixture_route, 0.0)
        self.assertFalse(ship.arrived)
        # Advance 10 steps of 30 seconds = 300s
        for _ in range(10):
            ship.step_motion(30.0)
        self.assertGreater(ship.distance_traveled_m, 0.0)
        obs = ship.to_observable()
        self.assertEqual(obs["ship_id"], "sim:999000001:voyage_0")
        self.assertIn("lat", obs)
        self.assertIn("lon", obs)

    def test_whale_population(self):
        whales = WhalePopulation(self.inputs["scenario"]["whales"], seed=42)
        self.assertEqual(len(whales.whales), 1)
        movements = whales.step(30.0)
        self.assertEqual(len(movements), 1)
        wid, p0, p1, exposure = movements[0]
        self.assertEqual(wid, "w_0")
        self.assertGreaterEqual(exposure, 0.0)

    def test_scripted_detour_execution(self):
        """Test scripted advisor returning a detour at fixed time."""
        def scripted_decide(obs):
            sim_time = obs.get("sim_time_s", 0.0)
            if 60.0 <= sim_time < 120.0:
                return {
                    "decision_id": "dec_001",
                    "actions": [{
                        "ship_id": "sim:999000001:voyage_0",
                        "action_id": "act_001",
                        "revision": 1,
                        "command": "DETOUR",
                        "target_speed_knots": 10.0,
                        "detour_waypoints": [{"lat": 34.21, "lon": -120.07}],
                        "rejoin_route_index": 2,
                        "issued_at": obs["now"],
                        "valid_until": "2026-07-01T13:00:00Z"
                    }],
                    "assessments": []
                }
            return {"actions": [], "assessments": []}

        def strict_validator(action, obs, purpose="ISSUE"):
            return {"ok": True, "reasons": [], "validated_action": action}

        def mock_weather_exposure(legs, weather):
            return 0.0

        result = run_scenario(
            inputs=self.inputs,
            decide=scripted_decide,
            validate_action=strict_validator,
            weather_exposure=mock_weather_exposure
        )

        self.assertIn("summary", result)
        self.assertIn("frames", result)
        self.assertIn("events", result)
        summary = result["summary"]
        self.assertEqual(summary["status"], "COMPLETE")
        self.assertEqual(summary["voyages_completed"], 1)
        self.assertGreater(summary["route_changes"], 0)

    def test_reproducibility_across_identical_seeds(self):
        """Identical inputs produce identical summaries and events."""
        def no_action(obs):
            return {"actions": [], "assessments": []}

        def validator(action, obs, purpose="ISSUE"):
            return {"ok": True, "reasons": []}

        def mock_weather(legs, weather):
            return 0.0

        res1 = run_scenario(self.inputs, no_action, validator, mock_weather)
        res2 = run_scenario(self.inputs, no_action, validator, mock_weather)

        self.assertEqual(res1["summary"]["modeled_contacts"], res2["summary"]["modeled_contacts"])
        self.assertEqual(res1["summary"]["close_approaches"], res2["summary"]["close_approaches"])
        self.assertEqual(res1["summary"]["whale_exposure_proxy"], res2["summary"]["whale_exposure_proxy"])
        self.assertEqual(res1["summary"]["simulated_duration_s"], res2["summary"]["simulated_duration_s"])

if __name__ == "__main__":
    unittest.main()

