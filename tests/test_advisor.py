"""Task 3's isolated tests: python3 -m unittest discover -s tests -p test_advisor.py."""
import copy
import json
import unittest
from fixtures.advisor import geometry as geo
from fixtures.advisor.inputs import observation, config, rect
from save_the_whales.advisor import make_decider, make_validator, weather_exposure
from save_the_whales.advisor.weather import weather_details
from save_the_whales.advisor.risk import relative_cpa, fresh_reports, predict_risk
from save_the_whales.advisor.candidates import generate_candidates, action_for
from save_the_whales.advisor._common import settings, utc, stamp, distance, timed_path


class RiskTests(unittest.TestCase):
    def test_known_cpa(self):
        self.assertEqual(relative_cpa((100, 20), (-10, 0), 30), (10, 20))

    def test_zero_relative_speed_and_interval_clamp(self):
        self.assertEqual(relative_cpa((3, 4), (0, 0), 20), (0, 5))
        self.assertEqual(relative_cpa((100, 0), (-10, 0), 5), (5, 50))

    def test_duplicate_stale_and_future_reports(self):
        obs, cfg = observation(), settings(config())
        report = obs["whale_detections"][0]
        obs["whale_detections"] += [copy.deepcopy(report), dict(report, detection_id="future", reported_at="2026-07-01T13:00:00Z"),
            dict(report, detection_id="old", observed_at="2026-06-30T12:00:00Z")]
        self.assertEqual(len(fresh_reports(obs, cfg)), 1)
        obs["whale_detections"][0]["position_error_m"] = float("nan")
        self.assertEqual(len(fresh_reports(obs, cfg)), 1)

    def test_retained_evidence_cap(self):
        obs, cfg = observation(), settings(config(max_reports=500))
        report = obs["whale_detections"][0]
        obs["whale_detections"] = [dict(report, detection_id=str(i)) for i in range(80)]
        self.assertEqual(len(fresh_reports(obs, cfg)), 50)

    def test_later_leg_advances_mean_and_uncertainty(self):
        obs, cfg = observation(), settings(config(drift_mps=1))
        report = dict(obs["whale_detections"][0], lat=34.2, lon=-119.75, velocity_mps=dict(x=10, y=0), position_error_m=0, age_s=0)
        leg = dict(start=geo.unproject(1000, 100), end=geo.unproject(1200, 100), start_s=100, end_s=120, speed_knots=18)
        prediction = predict_risk([leg], [report], 0, 18, 0, cfg, geo)
        self.assertAlmostEqual(prediction["closest_approach"]["clearance_m"], 100, places=5)
        self.assertEqual(prediction["closest_approach"]["uncertainty_m"], 100)

    def test_age_grows_uncertainty_and_duplicate_does_not_raise_risk(self):
        cfg = settings(config(drift_mps=1.5))
        report = dict(observation()["whale_detections"][0], age_s=0)
        leg = dict(start=dict(lat=34.205, lon=-120.07), end=dict(lat=34.205, lon=-120.06), start_s=0, end_s=100, speed_knots=18)
        fresh = predict_risk([leg], [report], 0, 18, 0, cfg, geo)
        duplicate = predict_risk([leg], [report, report], 0, 18, 0, cfg, geo)
        aged = predict_risk([leg], [dict(report, age_s=600)], 0, 18, 0, cfg, geo)
        self.assertEqual(fresh, duplicate)
        self.assertGreater(aged["closest_approach"]["uncertainty_m"], fresh["closest_approach"]["uncertainty_m"])


class WeatherTests(unittest.TestCase):
    def setUp(self):
        self.field = dict(source="SIMULATED", coverage_start_s=0, coverage_end_s=1000, zones=[])
        self.leg = dict(start=geo.unproject(0, 0), end=geo.unproject(1000, 0), start_s=0, end_s=100, speed_knots=18)

    def zone(self, severity, x0=0, x1=1000, start=0, until=1000, blocked=False):
        return dict(zone_id=str(severity), polygon=rect(x0, -100, x1, 100), severity=severity,
                    active_from_s=start, active_until_s=until, blocked=blocked)

    def test_clear_and_spatial_fraction(self):
        self.assertEqual(weather_exposure([self.leg], self.field, geometry=geo), 0)
        self.field["zones"] = [self.zone(.8, 250, 750)]
        self.assertAlmostEqual(weather_exposure([self.leg], self.field, geometry=geo), 40, places=5)

    def test_overlaps_use_maximum_and_time_windows(self):
        self.field["zones"] = [self.zone(.4), self.zone(.9, start=20, until=70)]
        self.assertAlmostEqual(weather_exposure([self.leg], self.field, geometry=geo), 65, places=5)

    def test_arrival_times_not_leg_relative(self):
        self.field["zones"] = [self.zone(1, start=0, until=100)]
        later = dict(self.leg, start_s=200, end_s=300)
        self.assertEqual(weather_exposure([later], self.field, geometry=geo), 0)

    def test_unknown_and_malformed_weather_fail_explicitly(self):
        for field in [None, {}, dict(self.field, coverage_end_s=50), dict(self.field, zones=[self.zone(float("nan"))])]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "UNKNOWN_WEATHER"):
                weather_exposure([self.leg], field, geometry=geo)

    def test_stationary_interval_and_half_open_end(self):
        self.field["zones"] = [self.zone(.5, until=50, blocked=True)]
        leg = dict(self.leg, start=geo.unproject(500, 0), end=geo.unproject(500, 0), start_s=10, end_s=30)
        value, blocked = weather_details([leg], self.field, geometry=geo)
        self.assertEqual(value, 10)
        self.assertTrue(blocked)
        self.assertEqual(weather_details([dict(leg, start_s=50, end_s=70)], self.field, geometry=geo), (0, False))

    def test_touching_block_is_hard_even_at_zero_severity(self):
        self.field["zones"] = [self.zone(0, x0=1000, x1=1100, blocked=True)]
        value, blocked = weather_details([self.leg], self.field, geometry=geo)
        self.assertEqual(value, 0)
        self.assertTrue(blocked)


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.obs, self.cfg = observation(), config(drift_mps=0)
        self.ship = self.obs["ships"][0]
        self.validate = make_validator(self.cfg, geo)
        self.action = make_decider(self.cfg, geo)(self.obs)["actions"][0]
        self.assertEqual(self.action["command"], "DETOUR")

    def assertReject(self, action=None, obs=None, purpose="ISSUE", code=None):
        result = self.validate(action if action is not None else self.action, obs if obs is not None else self.obs, purpose=purpose)
        self.assertFalse(result["ok"], result)
        if code:
            self.assertIn(code, result["reasons"])
        return result

    def test_exact_resolved_rejoin_and_immutability(self):
        before = copy.deepcopy((self.obs, self.action))
        result = self.validate(self.action, self.obs)
        self.assertTrue(result["ok"], result)
        resolved = result["validated_action"]
        self.assertEqual(resolved["remaining_waypoints"], self.action["detour_waypoints"]+self.ship["original_route"][self.action["rejoin_route_index"]:])
        self.assertEqual(before, (self.obs, self.action))
        json.dumps(result, allow_nan=False)

    def test_revision_rules_issue_apply_and_superseded(self):
        self.ship["latest_revision"] = self.action["revision"]
        self.assertReject(code="SUPERSEDED")
        self.assertTrue(self.validate(self.action, self.obs, purpose="APPLY")["ok"])
        self.ship["latest_revision"] += 1
        self.assertReject(purpose="APPLY", code="SUPERSEDED")

    def test_malformed_nonfinite_unknown_and_unsupported(self):
        for updates, code in [({"command": "HOLD"}, "INVALID_ACTION"), ({"target_speed_knots": float("nan")}, "INVALID_SPEED"),
            ({"ship_id": "missing"}, "UNKNOWN_SHIP"), ({"revision": True}, "INVALID_ACTION"),
            ({"target_speed_knots": 19}, "INVALID_SPEED"), ({"detour_waypoints": [dict(lat=34.2, lon=float("inf"))]}, "INVALID_ACTION")]:
            with self.subTest(updates=updates):
                self.assertReject(dict(self.action, **updates), code=code)

    def test_expired_and_future_actions(self):
        self.assertReject(dict(self.action, issued_at="2026-07-01T11:00:00Z", valid_until=self.obs["now"]), code="EXPIRED_ACTION")
        self.assertReject(dict(self.action, issued_at="2026-07-01T12:00:01Z"), code="INVALID_ACTION")

    def test_map_missing_empty_and_region_boundary(self):
        for land in [{}, dict(self.obs["static"]["land"], rings=[]), dict(self.obs["static"]["land"], validated=False)]:
            obs = copy.deepcopy(self.obs)
            obs["static"]["land"] = land
            self.assertReject(obs=obs, code="INVALID_MAP")
        self.assertReject(dict(self.action, detour_waypoints=[dict(lat=35, lon=-120)]), code="OUT_OF_BOUNDS")

    def test_forward_rejoin_duplicates_excess_points_and_backtracking(self):
        self.assertReject(dict(self.action, rejoin_route_index=0), code="INVALID_REJOIN")
        endpoint = self.ship["original_route"][self.action["rejoin_route_index"]]
        self.assertReject(dict(self.action, detour_waypoints=[endpoint]), code="INVALID_ACTION")
        self.assertReject(dict(self.action, detour_waypoints=self.action["detour_waypoints"]*5), code="INVALID_ACTION")
        pts = [dict(lat=34.21, lon=-120.04), dict(lat=34.21, lon=-120.08)]
        self.assertReject(dict(self.action, detour_waypoints=pts), code="BACKTRACKING")

    def test_full_continuation_is_checked(self):
        # Rejoin earlier than final point; the original continuation crosses land.
        action = dict(self.action, rejoin_route_index=2)
        x, _ = geo.project(self.ship["original_route"][2])
        self.obs["static"]["land"]["rings"] = [rect(x+400, -100, x+800, 100)]
        self.assertReject(action, code="LAND_CLEARANCE")

    def test_water_only_waypoints_can_cross_land(self):
        a, b = self.action["detour_waypoints"]
        x, y = geo.project(a)
        u, v = geo.project(b)
        self.obs["static"]["land"]["rings"] = [rect((x+u)/2-20, (y+v)/2-20, (x+u)/2+20, (y+v)/2+20)]
        self.assertReject(code="LAND_CLEARANCE")

    def test_incident_allowances_never_multiply(self):
        resolved = self.validate(self.action, self.obs)["validated_action"]
        self.ship["incident_budget"] = resolved["incident_budget"]
        self.ship["incident_budget"]["accumulated_distance_m"] = self.ship["incident_budget"]["max_distance_m"]-1
        self.assertReject(code="DISTANCE_BUDGET")
        self.ship["incident_budget"]["accumulated_distance_m"] = 0
        self.ship["incident_budget"]["accumulated_time_s"] = 3000
        self.assertReject(code="DELAY_BUDGET")

    def test_apply_lag_and_materially_stale_geometry(self):
        self.ship["latest_revision"] = self.action["revision"]
        from save_the_whales.advisor._common import advance
        pos, pending, _ = advance(self.ship, self.ship["current_route"], 18, 30, geo)
        self.obs["now"] = "2026-07-01T12:00:30Z"
        self.obs["sim_time_s"] = 30
        self.ship.update(pos, current_route=pending)
        self.assertTrue(self.validate(self.action, self.obs, purpose="APPLY")["ok"])
        self.ship["lat"] += .01
        self.assertReject(purpose="APPLY", code="STALE_ACTION")

    def test_expiry_uses_unpassed_route_and_service_speed(self):
        resolved = self.validate(self.action, self.obs)["validated_action"]
        self.ship["latest_revision"] = self.action["revision"]
        self.ship["incident_budget"] = resolved["incident_budget"]
        self.ship["current_route"] = resolved["remaining_waypoints"][1:]
        self.ship.update(resolved["remaining_waypoints"][0])
        self.obs.update(now="2026-07-01T12:16:00Z", sim_time_s=960)
        result = self.validate(self.action, self.obs, purpose="EXPIRY_FALLBACK")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["validated_action"]["remaining_waypoints"], self.ship["current_route"])
        self.assertEqual(result["validated_action"]["target_speed_knots"], 18)
        self.assertNotIn(resolved["remaining_waypoints"][0], result["validated_action"]["remaining_waypoints"])

    def test_expiry_rechecks_weather_and_newer_revision(self):
        resolved = self.validate(self.action, self.obs)["validated_action"]
        self.ship.update(latest_revision=self.action["revision"], incident_budget=resolved["incident_budget"], current_route=resolved["remaining_waypoints"])
        self.obs["static"]["weather"]["zones"] = [dict(zone_id="block", polygon=rect(-40000, -2000, -20000, 2000), severity=0,
            active_from_s=0, active_until_s=20000, blocked=True)]
        self.assertReject(purpose="EXPIRY_FALLBACK", code="BLOCKED_WEATHER")
        self.ship["latest_revision"] += 1
        self.assertReject(purpose="EXPIRY_FALLBACK", code="SUPERSEDED")


class PlannerTests(unittest.TestCase):
    def test_fixture_produces_detour_and_valid_forward_rejoin(self):
        obs, cfg = observation(), config(drift_mps=0)
        result = make_decider(cfg, geo)(obs)
        self.assertEqual(result["actions"][0]["command"], "DETOUR")
        self.assertTrue(make_validator(cfg, geo)(result["actions"][0], obs)["ok"])
        self.assertLessEqual(result["assessments"][0]["candidate_count"], 12)

    def test_distance_constrained_case_produces_slowdown(self):
        result = make_decider(config(max_path_ratio=1.001), geo)(observation())
        self.assertEqual(result["actions"][0]["command"], "SLOWDOWN")
        self.assertLess(result["actions"][0]["target_speed_knots"], 18)

    def test_deterministic_pure_and_hidden_state_irrelevant(self):
        obs, cfg = observation(), config(drift_mps=0)
        before = copy.deepcopy(obs)
        decide = make_decider(cfg, geo)
        expected = decide(obs)
        self.assertEqual(expected, decide(copy.deepcopy(obs)))
        self.assertEqual(obs, before)
        obs["analyst_truth"] = {"whales": [{"private_id": "not-observable"}]}
        obs["source_capture_updates"] = [1, 2, 3]
        self.assertEqual(expected, decide(obs))
        json.dumps(expected, allow_nan=False)

    def test_missing_evidence_is_not_clear_ocean(self):
        obs = observation()
        obs["whale_detections"] = []
        result = make_decider(config(), geo)(obs)
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["assessments"][0]["status"], "INSUFFICIENT_FRESH_EVIDENCE")

    def test_unknown_weather_and_invalid_map_have_distinct_status(self):
        for field, expected in [("weather", "UNKNOWN_WEATHER"), ("land", "INVALID_MAP")]:
            obs = observation()
            obs["static"][field] = {}
            result = make_decider(config(), geo)(obs)
            self.assertEqual(result["actions"], [])
            self.assertEqual(result["assessments"][0]["status"], expected)

    def test_land_blocked_side_rejects_and_chooses_other_side(self):
        obs, cfg = observation(), config(drift_mps=0)
        obs["static"]["land"]["rings"] = [[dict(lat=lat, lon=lon) for lat, lon in
            [(34.204,-120.075),(34.204,-120.055),(34.216,-120.055),(34.216,-120.075)]]]
        result = make_decider(cfg, geo)(obs)
        action = result["actions"][0]
        self.assertEqual(action["command"], "DETOUR")
        self.assertTrue(all(p["lat"] < 34.2 for p in action["detour_waypoints"]))
        self.assertTrue(any("LAND_CLEARANCE" in r["reason_codes"] for r in result["assessments"][0]["rejected_candidates"]))

    def test_no_feasible_route_does_not_retain_invalid_plan(self):
        obs = observation()
        obs["static"]["land"]["rings"] = [rect(-40000, -10000, -10000, 10000)]
        result = make_decider(config(), geo)(obs)
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["assessments"][0]["status"], "NO_FEASIBLE_ROUTE")

    def test_equivalent_stormy_side_ranks_below_clear_side(self):
        obs, cfg = observation(), config(drift_mps=0)
        obs["static"]["weather"]["zones"] = [dict(zone_id="north-storm", polygon=rect(-40000, 50, -10000, 1500),
            severity=1, active_from_s=0, active_until_s=20000, blocked=False)]
        action = make_decider(cfg, geo)(obs)["actions"][0]
        self.assertTrue(all(p["lat"] < 34.2 for p in action["detour_waypoints"]))
        self.assertEqual(action["metadata"]["costs"]["weather_exposure_s"], 0)

    def test_weather_only_reroute_and_block(self):
        obs, cfg = observation(), config(drift_mps=0)
        obs["whale_detections"] = []
        x, y = geo.project(dict(lat=34.2, lon=-120.065))
        obs["static"]["weather"]["zones"] = [dict(zone_id="storm", polygon=rect(x-500, -100, x+500, 100),
            severity=1, active_from_s=0, active_until_s=20000, blocked=True)]
        result = make_decider(cfg, geo)(obs)
        self.assertEqual(result["actions"][0]["command"], "DETOUR")
        self.assertTrue(make_validator(cfg, geo)(result["actions"][0], obs)["ok"])

    def test_candidate_and_point_caps_and_cooldown(self):
        obs, cfg = observation(), config(drift_mps=0)
        candidates = generate_candidates(obs["ships"][0], obs, settings(cfg), geo, obs["whale_detections"][0])
        self.assertEqual(len(candidates), 12)
        self.assertTrue(all(len(c["action"]["detour_waypoints"]) <= 8 for c in candidates))
        obs["ships"][0]["last_action_at"] = obs["now"]
        result = make_decider(cfg, geo)(obs)
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["assessments"][0]["status"], "COOLDOWN")

    def test_response_lag_is_scored_and_validated(self):
        obs, cfg = observation(), config(drift_mps=0)
        cfg["response"]["response_lag_s"] = 30
        result = make_decider(cfg, geo)(obs)
        self.assertTrue(result["actions"])
        valid = make_validator(cfg, geo)(result["actions"][0], obs)
        self.assertTrue(valid["ok"], valid)
        self.assertEqual(valid["validated_action"]["timed_legs"][0]["end_s"], 30)

    def test_resume_is_freshly_validated_and_does_not_rewind(self):
        obs, cfg = observation(), config(drift_mps=0)
        ship = obs["ships"][0]
        ship["sog_knots"] = 8
        ship["active_command"] = dict(command="SLOWDOWN", valid_until="2026-07-01T12:15:00Z")
        obs["whale_detections"][0].update(lat=34.5, lon=-119.1)
        result = make_decider(cfg, geo)(obs)
        self.assertTrue(result["actions"], result)
        self.assertEqual(result["actions"][0]["command"], "RESUME")
        self.assertEqual(make_validator(cfg, geo)(result["actions"][0], obs)["validated_action"]["remaining_waypoints"], ship["current_route"])

    def test_duplicate_ship_ids_and_compute_limits(self):
        for count, expected in [(2, "INVALID_OBSERVATION"), (11, "NOT_EVALUATED_BUDGET")]:
            obs = observation()
            obs["ships"] *= count
            result = make_decider(config(), geo)(obs)
            self.assertEqual(result["actions"], [])
            self.assertEqual(result["assessments"][0]["status"], expected)

    def test_lease_boundary_splits_and_restores_speed(self):
        path = [geo.unproject(0, 0), geo.unproject(1000, 0)]
        legs = timed_path(path, 100, 8, 18, 60, geo)
        self.assertEqual(legs[0]["start_s"], 100)
        self.assertEqual(legs[0]["end_s"], 160)
        self.assertEqual(legs[0]["speed_knots"], 8)
        self.assertEqual(legs[1]["speed_knots"], 18)

    def test_lost_evidence_does_not_itself_authorize_resume(self):
        obs = observation()
        obs["whale_detections"] = []
        obs["ships"][0].update(sog_knots=8, active_command=dict(command="SLOWDOWN", valid_until="2026-07-01T12:15:00Z"))
        result = make_decider(config(), geo)(obs)
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["assessments"][0]["status"], "INSUFFICIENT_FRESH_EVIDENCE")

    def test_active_detour_reserves_return_slot(self):
        obs, cfg = observation(), config(drift_mps=0)
        action = make_decider(cfg, geo)(obs)["actions"][0]
        resolved = make_validator(cfg, geo)(action, obs)["validated_action"]
        ship = obs["ships"][0]
        ship.update(current_route=resolved["remaining_waypoints"], active_command=action, incident_budget=resolved["incident_budget"])
        candidates = generate_candidates(ship, obs, settings(cfg), geo)
        self.assertLessEqual(len(candidates), 12)
        self.assertTrue(any(c["action"]["command"] == "RESUME" for c in candidates))
        self.assertFalse(any(c["name"].startswith("larger_") for c in candidates))
        for c in candidates:
            self.assertEqual(c["action"]["rejoin_route_index"], action["rejoin_route_index"])

    def test_geometry_cached_between_speed_variants(self):
        class CountingGeometry:
            calls = 0
            def __getattr__(self, name):
                return getattr(geo, name)
            def check_water_path(self, *args):
                self.calls += 1
                return geo.check_water_path(*args)
        facade = CountingGeometry()
        result = make_decider(config(drift_mps=0), facade)(observation())
        self.assertEqual(result["assessments"][0]["candidate_count"], 12)
        self.assertLessEqual(facade.calls, 4)

    def test_no_action_does_not_search_full_matrix(self):
        obs = observation()
        obs["whale_detections"][0].update(lat=34.5, lon=-119.1)
        result = make_decider(config(), geo)(obs)
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["assessments"][0]["candidate_count"], 1)

    def test_curved_baseline_resume_keeps_every_bend(self):
        obs, cfg = observation(), config(drift_mps=0)
        ship = obs["ships"][0]
        ship["original_route"][1]["lat"] = 34.21
        ship["current_route"] = copy.deepcopy(ship["original_route"][1:])
        ship.update(sog_knots=8, active_command=dict(command="SLOWDOWN", valid_until="2026-07-01T12:15:00Z"))
        obs["whale_detections"][0].update(lat=34.5, lon=-119.1)
        result = make_decider(cfg, geo)(obs)
        action = result["actions"][0]
        self.assertEqual(action["command"], "RESUME")
        self.assertEqual(make_validator(cfg, geo)(action, obs)["validated_action"]["remaining_waypoints"], ship["current_route"])

    def test_revisioned_renewal_extends_a_lease_without_reusing_identity(self):
        obs, cfg = observation(), config(drift_mps=0, max_path_ratio=1.001, lease_s=300)
        ship = obs["ships"][0]
        ship.update(sog_knots=8, latest_revision=4, active_command=dict(command="SLOWDOWN", valid_until="2026-07-01T12:00:10Z"))
        obs["whale_detections"][0]["lon"] = -120.092
        result = make_decider(cfg, geo)(obs)
        self.assertTrue(result["actions"], result)
        action = result["actions"][0]
        self.assertEqual(action["revision"], 5)
        self.assertGreater(utc(action["valid_until"]), utc(ship["active_command"]["valid_until"]))
        self.assertNotEqual(action["action_id"], "pending")

    def test_lag_forecast_honors_existing_lease_expiry(self):
        obs, cfg = observation(), config(drift_mps=0)
        cfg["response"]["response_lag_s"] = 60
        ship = obs["ships"][0]
        ship.update(sog_knots=8, active_command=dict(command="SLOWDOWN", valid_until="2026-07-01T12:00:10Z"))
        result = make_decider(cfg, geo)(obs)
        self.assertTrue(result["actions"], result)
        action = result["actions"][0]
        validate = make_validator(cfg, geo)
        issued = validate(action, obs)
        self.assertTrue(issued["ok"], issued)
        from save_the_whales.advisor.constraints import _prefix_for_delay
        prefix, pending, _ = _prefix_for_delay(ship, obs, 60, geo)
        ship.update(prefix[-1], current_route=pending, latest_revision=action["revision"])
        obs.update(now="2026-07-01T12:01:00Z", sim_time_s=60)
        self.assertTrue(validate(action, obs, purpose="APPLY")["ok"])

    def test_unknown_coverage_in_baseline_continuation_is_not_ignored(self):
        obs = observation()
        obs["static"]["weather"]["coverage_end_s"] = 100
        result = make_decider(config(drift_mps=0), geo)(obs)
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["assessments"][0]["status"], "UNKNOWN_WEATHER")

    def test_unsupported_budgets_fail_before_search(self):
        for planner in [dict(max_rejoin_s=5000), dict(w_whale=float("nan")), dict(offsets_m=[100000]), dict(max_reports=0)]:
            with self.subTest(planner=planner), self.assertRaisesRegex(ValueError, "INVALID_CONFIG"):
                make_decider(config(**planner), geo)

    def test_manual_command_handoff_traverses_detour_and_baseline_without_reset(self):
        from save_the_whales.advisor._common import advance
        obs, cfg = observation(), config(drift_mps=0)
        ship = obs["ships"][0]
        original = copy.deepcopy(ship["original_route"])
        action = make_decider(cfg, geo)(obs)["actions"][0]
        validate = make_validator(cfg, geo)
        issued = validate(action, obs)["validated_action"]
        ship.update(latest_revision=action["revision"], incident_budget=issued["incident_budget"])
        applied = validate(action, obs, purpose="APPLY")
        self.assertTrue(applied["ok"], applied)
        remaining = applied["validated_action"]["remaining_waypoints"]
        ship["active_command"] = action
        start = dict(lat=ship["lat"], lon=ship["lon"])
        for _ in range(60):
            pos, remaining, travelled = advance(ship, remaining, action["target_speed_knots"], 30, geo)
            ship.update(pos, current_route=remaining)
            ship["incident_budget"]["accumulated_distance_m"] += travelled
            ship["incident_budget"]["accumulated_time_s"] += 30
            self.assertEqual(ship["original_route"], original)
            if not remaining:
                break
        self.assertEqual(remaining, [])
        self.assertAlmostEqual(ship["lat"], original[-1]["lat"])
        self.assertAlmostEqual(ship["lon"], original[-1]["lon"])
        self.assertGreater(distance(start, ship, geo), 1000)

    def test_factory_freezes_configuration(self):
        cfg, obs = config(drift_mps=0, offsets_m=[500, 1000]), observation()
        decide = make_decider(cfg, geo)
        expected = decide(obs)
        cfg["planner"]["offsets_m"][0] = 500000
        cfg["planner"]["drift_mps"] = 50
        self.assertEqual(expected, decide(obs))

    def test_snapshot_does_not_recursively_copy_command_history(self):
        obs, cfg = observation(), config(drift_mps=0)
        action = make_decider(cfg, geo)(obs)["actions"][0]
        obs["ships"][0]["active_command"] = action
        candidates = generate_candidates(obs["ships"][0], obs, settings(cfg), geo)
        for candidate in candidates:
            lease = candidate["action"]["source_snapshot"]["active_command"]
            self.assertEqual(set(lease), {"command", "valid_until"})

    def test_missing_dimensions_are_conservative_and_declared(self):
        obs, cfg = observation(), config(drift_mps=0)
        del obs["ships"][0]["length_m"]
        result = make_decider(cfg, geo)(obs)
        self.assertTrue(result["actions"], result)
        resolved = make_validator(cfg, geo)(result["actions"][0], obs)["validated_action"]
        self.assertGreater(resolved["clearance_radius_m"], geo.ship_clearance_m(300, 40, 100))
        notes = result["actions"][0]["metadata"]["assumptions"]
        self.assertEqual(sum("Missing dimensions" in s for s in notes), 1)

    def test_malformed_ship_list_is_a_structured_failure(self):
        obs = observation()
        obs["ships"] = None
        result = make_decider(config(), geo)(obs)
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["assessments"][0]["status"], "NOT_EVALUATED_BUDGET")


if __name__ == "__main__":
    unittest.main()
