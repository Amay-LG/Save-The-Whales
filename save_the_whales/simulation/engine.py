"""Main simulation loop: run_scenario execution boundary for Person 2."""
import copy
import time
import json
from typing import Dict, Any, List, Optional, Callable
from save_the_whales.shared.contracts import RunResult, RunSummary, Decision, TimedLeg
from save_the_whales.shared.geo import unproject, distance_m, KNOTS_TO_MPS
from save_the_whales.simulation.world import SimWorld
from save_the_whales.simulation.sensors import SensorSuite
from save_the_whales.simulation.commands import CommandManager
from save_the_whales.simulation.metrics import TruthEvaluator

def run_scenario(inputs: Dict[str, Any],
                 decide: Callable[[Dict[str, Any]], Dict[str, Any]],
                 validate_action: Callable[..., Dict[str, Any]],
                 weather_exposure: Callable[[List[Dict[str, Any]], Dict[str, Any]], float],
                 *,
                 project: Optional[Callable[[Dict[str, float]], tuple]] = None,
                 on_frame: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    """Execute scenario with bounded wall-clock watchdog and drain period.
    
    Architecture §§3, 7, 9-12 and Tasks §2.
    """
    t_wall_start = time.perf_counter()

    # Deep copy inputs to ensure immutability
    run_inputs = copy.deepcopy(inputs)
    cfg = run_inputs.get("config", {})
    sim_cfg = cfg.get("simulation", {})
    scenario = run_inputs.get("scenario", {})
    seed = scenario.get("seed", 42)

    dt_s = float(sim_cfg.get("dt_s", 30.0))
    decision_dt_s = float(sim_cfg.get("decision_dt_s", 60.0))
    duration_s = float(scenario.get("duration_s", sim_cfg.get("duration_s", 7200.0)))
    drain_max_s = float(sim_cfg.get("drain_max_s", 1800.0))
    wall_limit_s = float(sim_cfg.get("wall_limit_s", 30.0))

    # Initialize subsystems
    world = SimWorld(run_inputs)
    sensors = SensorSuite(cfg, seed)
    cmd_manager = CommandManager(cfg, seed)
    evaluator = TruthEvaluator(cfg)

    events: List[Dict[str, Any]] = []
    frames: List[Dict[str, Any]] = []
    decisions_log: List[Dict[str, Any]] = []

    total_voyages = len(scenario.get("voyages", []))
    total_alerts = 0
    total_route_changes = 0
    total_rejected = 0

    run_status = "COMPLETE"
    sim_time_s = 0.0
    next_decision_s = 0.0
    decision_counter = 0

    # Max frames cap to prevent memory bloat
    MAX_FRAMES = 500

    # Main time loop
    while True:
        # Check wall-clock watchdog
        elapsed_wall = time.perf_counter() - t_wall_start
        if elapsed_wall >= wall_limit_s:
            run_status = "INCOMPLETE"
            events.append({
                "sim_time_s": sim_time_s,
                "type": "WALL_LIMIT_EXCEEDED",
                "details": {"elapsed_wall_s": elapsed_wall}
            })
            break

        # Check termination:
        # End early if no pending voyages and no active ships
        if not world.pending_voyages and not world.active_ships:
            break

        # Check max simulation duration + drain
        if sim_time_s >= duration_s + drain_max_s:
            if world.active_ships:
                run_status = "INCOMPLETE"
                events.append({
                    "sim_time_s": sim_time_s,
                    "type": "DRAIN_PERIOD_EXCEEDED",
                    "details": {"active_ships_count": len(world.active_ships)}
                })
            break

        world.sim_time_s = sim_time_s
        now_iso = world.get_current_iso()

        # 1. Spawn due voyages
        if sim_time_s < duration_s:
            newly_spawned = world.spawn_due()
            for s in newly_spawned:
                events.append({
                    "sim_time_s": sim_time_s,
                    "type": "SHIP_SPAWNED",
                    "ship_id": s.ship_id,
                    "details": {"voyage_id": s.voyage_id}
                })

        # 2. Sensors sample and queue detections; fetch reported
        sensors.observe(sim_time_s, world.whales.whales, list(world.active_ships.values()), now_iso)
        reported_detections = sensors.get_reported(sim_time_s)

        # 3. Decision loop (if due and active ships exist)
        if sim_time_s >= next_decision_s and world.active_ships:
            next_decision_s = sim_time_s + decision_dt_s
            decision_counter += 1
            dec_id = f"dec_{decision_counter:04d}"

            # Observation does NOT pass hidden world state
            obs = world.build_observation(reported_detections, dec_id, decision_dt_s / 60.0)

            # Call decider (wrapped in try/except so advisor errors don't crash sim)
            try:
                dec_result = decide(obs) or {}
            except Exception as e:
                events.append({
                    "sim_time_s": sim_time_s,
                    "type": "ADVISOR_ERROR",
                    "details": {"error": str(e)}
                })
                dec_result = {}

            if dec_result:
                decisions_log.append(dec_result)

            actions = dec_result.get("actions", [])
            assessments = dec_result.get("assessments", [])

            # Check assessments for NO_FEASIBLE_ROUTE
            for ass in assessments:
                if ass.get("status") == "NO_FEASIBLE_ROUTE":
                    run_status = "INCOMPLETE"
                    events.append({
                        "sim_time_s": sim_time_s,
                        "type": "NO_FEASIBLE_ROUTE",
                        "ship_id": ass.get("ship_id"),
                        "details": ass.get("reasons", [])
                    })

            for act in actions:
                total_alerts += 1
                if act.get("command") == "DETOUR":
                    total_route_changes += 1

                # Validate and queue with response lag
                queued = cmd_manager.queue_action(
                    action=act,
                    sim_time_s=sim_time_s,
                    validate_fn=validate_action,
                    observation=obs,
                    ships_by_id=world.active_ships,
                    events_log=events
                )
                if not queued:
                    total_rejected += 1

        # 4. Apply due commands & check expiries
        current_obs = world.build_observation(reported_detections, "apply_check")
        cmd_manager.apply_due_commands(
            sim_time_s=sim_time_s,
            validate_fn=validate_action,
            observation=current_obs,
            ships_by_id=world.active_ships,
            events_log=events
        )

        failed_expiries = cmd_manager.check_expiries(
            sim_time_s=sim_time_s,
            validate_fn=validate_action,
            observation=current_obs,
            ships_by_id=world.active_ships,
            events_log=events
        )
        if failed_expiries:
            run_status = "INCOMPLETE"

        # 5. Advance motion
        # Step whales independently
        whale_movements = world.whales.step(dt_s)

        # Step ships and accumulate metrics
        timed_legs_for_weather: List[Dict[str, Any]] = []
        arrived_ship_ids = []

        for s_id, ship in list(world.active_ships.items()):
            sub_segs = ship.step_motion(dt_s)
            if sub_segs:
                evaluator.evaluate_motion_step(ship, sub_segs, whale_movements, dt_s)
                for p0, p1, spd, t0f, t1f in sub_segs:
                    p0_pt = unproject(p0[0], p0[1])
                    p1_pt = unproject(p1[0], p1[1])
                    timed_legs_for_weather.append({
                        "start": p0_pt,
                        "end": p1_pt,
                        "start_s": sim_time_s + t0f * dt_s,
                        "end_s": sim_time_s + t1f * dt_s,
                        "speed_knots": spd
                    })

            if ship.arrived:
                arrived_ship_ids.append(s_id)
                ref_time_s = (ship.distance_traveled_m / (ship.service_speed_knots * KNOTS_TO_MPS)) if ship.service_speed_knots > 0 else 0.0
                evaluator.record_voyage_completion(
                    voyage_id=ship.voyage_id,
                    actual_time_s=sim_time_s - ship.entry_s,
                    ref_time_s=ref_time_s,
                    actual_dist_m=ship.distance_traveled_m,
                    ref_dist_m=ship.distance_traveled_m
                )
                events.append({
                    "sim_time_s": sim_time_s,
                    "type": "SHIP_ARRIVED",
                    "ship_id": s_id,
                    "details": {"distance_m": ship.distance_traveled_m}
                })

        # Remove arrived ships from active
        for a_id in arrived_ship_ids:
            world.arrived_ships.append(world.active_ships.pop(a_id))

        # Accumulate weather exposure
        if timed_legs_for_weather and weather_exposure:
            try:
                w_exp = weather_exposure(timed_legs_for_weather, world.weather)
                evaluator.weather_exposure_s += w_exp
            except ValueError as ve:
                if str(ve) == "UNKNOWN_WEATHER":
                    run_status = "INCOMPLETE"
                    events.append({
                        "sim_time_s": sim_time_s,
                        "type": "UNKNOWN_WEATHER_EXPOSURE",
                        "details": {"error": "UNKNOWN_WEATHER"}
                    })

        # Record frame
        if len(frames) < MAX_FRAMES:
            obs_ships = [s.to_observable() for s in world.active_ships.values()]
            frame = {
                "sim_time_s": sim_time_s,
                "now": now_iso,
                "ships": obs_ships,
                "whale_detections": list(reported_detections),
                "current_alerts": [s.active_command for s in world.active_ships.values() if s.active_command]
            }
            frames.append(frame)
            if on_frame:
                on_frame(frame)

        sim_time_s += dt_s

    # Finalize summary
    elapsed_total_wall = time.perf_counter() - t_wall_start
    summary = evaluator.compute_summary(
        status=run_status,
        elapsed_wall_s=elapsed_total_wall,
        simulated_duration_s=sim_time_s,
        total_voyages=total_voyages,
        alerts_count=total_alerts,
        route_changes_count=total_route_changes,
        rejected_actions_count=total_rejected
    )

    manifest = {
        "run_id": run_inputs.get("run_id", "run_001"),
        "seed": seed,
        "fixture_only": run_inputs.get("fixture_only", False),
        "status": run_status,
        "config_hash": hash(json.dumps(cfg, sort_keys=True, default=str))
    }

    return {
        "manifest": manifest,
        "summary": summary,
        "frames": frames,
        "events": events,
        "decisions": decisions_log
    }

