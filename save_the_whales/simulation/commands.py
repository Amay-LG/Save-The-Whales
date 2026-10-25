"""Command management, validation dispatch, execution queues, and safe rejoin logic for Person 2."""
import math
from typing import Dict, Any, List, Optional, Tuple
from save_the_whales.shared.contracts import SLOWDOWN, DETOUR, RESUME, HOLD
from save_the_whales.shared.geo import project, unproject, distance_m

class CommandManager:
    def __init__(self, config: Dict[str, Any], seed: int):
        resp_cfg = config.get("response", {})
        self.response_lag_s = resp_cfg.get("response_lag_s", 0.0)
        self.compliance_rate = resp_cfg.get("compliance", 1.0)
        self.default_ttl_s = config.get("commands", {}).get("default_ttl_min", 60) * 60.0
        self.max_ttl_s = config.get("commands", {}).get("max_ttl_min", 360) * 60.0
        self.seed = seed

        # Queues:
        # pending_actions: list of (apply_at_s, ship_id, action_dict)
        self.pending_actions: List[Tuple[float, str, Dict[str, Any]]] = []
        # active_leases: dict of ship_id -> expiry_sim_time_s
        self.active_leases: Dict[str, float] = {}
        # compliance cache: voyage_id / ship_id -> bool
        self.compliance_map: Dict[str, bool] = {}

    def get_compliance(self, ship_id: str) -> bool:
        if ship_id not in self.compliance_map:
            # Deterministic keyed compliance per voyage/ship
            import random
            rng = random.Random(f"{self.seed}:compliance:{ship_id}")
            self.compliance_map[ship_id] = (rng.random() <= self.compliance_rate)
        return self.compliance_map[ship_id]

    def queue_action(self,
                     action: Dict[str, Any],
                     sim_time_s: float,
                     validate_fn: Any,
                     observation: Dict[str, Any],
                     ships_by_id: Dict[str, Any],
                     events_log: List[Dict[str, Any]]) -> bool:
        """Validate with purpose='ISSUE' and queue action with response lag."""
        ship_id = action.get("ship_id")
        if not ship_id or ship_id not in ships_by_id:
            events_log.append({
                "sim_time_s": sim_time_s,
                "type": "ACTION_REJECTED",
                "ship_id": ship_id,
                "details": {"reason": "UNKNOWN_SHIP"}
            })
            return False

        ship = ships_by_id[ship_id]

        # Call validator with purpose="ISSUE"
        val_res = validate_fn(action, observation, purpose="ISSUE")
        if not val_res.get("ok", False):
            events_log.append({
                "sim_time_s": sim_time_s,
                "type": "ACTION_REJECTED",
                "ship_id": ship_id,
                "details": {"reasons": val_res.get("reasons", [])}
            })
            return False

        validated_action = val_res.get("validated_action") or action

        # Check compliance
        if not self.get_compliance(ship_id):
            events_log.append({
                "sim_time_s": sim_time_s,
                "type": "ACTION_IGNORED_NONCOMPLIANCE",
                "ship_id": ship_id,
                "details": {"action_id": action.get("action_id")}
            })
            return False

        apply_at_s = sim_time_s + self.response_lag_s
        # Replace or queue
        self.pending_actions.append((apply_at_s, ship_id, validated_action))
        events_log.append({
            "sim_time_s": sim_time_s,
            "type": "ACTION_QUEUED",
            "ship_id": ship_id,
            "details": {"apply_at_s": apply_at_s, "action_id": action.get("action_id")}
        })
        return True

    def apply_due_commands(self,
                           sim_time_s: float,
                           validate_fn: Any,
                           observation: Dict[str, Any],
                           ships_by_id: Dict[str, Any],
                           events_log: List[Dict[str, Any]]):
        """Apply actions due at sim_time_s using purpose='APPLY'."""
        remaining_queue = []
        for apply_at_s, ship_id, action in self.pending_actions:
            if apply_at_s <= sim_time_s:
                if ship_id not in ships_by_id:
                    continue
                ship = ships_by_id[ship_id]

                # Check if action revision is still relevant
                rev = action.get("revision", 1)
                if rev < ship.latest_revision:
                    events_log.append({
                        "sim_time_s": sim_time_s,
                        "type": "ACTION_SUPERSEDED",
                        "ship_id": ship_id,
                        "details": {"action_id": action.get("action_id"), "revision": rev}
                    })
                    continue

                # Validate with purpose="APPLY"
                val_res = validate_fn(action, observation, purpose="APPLY")
                if not val_res.get("ok", False):
                    events_log.append({
                        "sim_time_s": sim_time_s,
                        "type": "ACTION_APPLY_FAILED",
                        "ship_id": ship_id,
                        "details": {"reasons": val_res.get("reasons", [])}
                    })
                    continue

                validated_act = val_res.get("validated_action") or action
                self._execute_action(ship, validated_act, sim_time_s, events_log)
            else:
                remaining_queue.append((apply_at_s, ship_id, action))

        self.pending_actions = remaining_queue

    def _execute_action(self,
                        ship: Any,
                        action: Dict[str, Any],
                        sim_time_s: float,
                        events_log: List[Dict[str, Any]]):
        """Apply validated command to the ship."""
        cmd = action.get("command")
        if cmd == HOLD:
            cmd = RESUME

        # Parse valid_until or set default TTL
        # We store expiry_time_s in self.active_leases
        ttl_s = self.default_ttl_s
        self.active_leases[ship.ship_id] = sim_time_s + ttl_s

        ship.active_command = action
        ship.latest_revision = action.get("revision", ship.latest_revision + 1)
        ship.last_action_at = action.get("issued_at")

        if action.get("incident_budget"):
            ship.incident_budget = action.get("incident_budget")

        if cmd == SLOWDOWN:
            target_spd = action.get("target_speed_knots")
            if target_spd is not None:
                ship.current_speed_knots = float(target_spd)
            events_log.append({
                "sim_time_s": sim_time_s,
                "type": "COMMAND_APPLIED",
                "ship_id": ship.ship_id,
                "details": {"command": SLOWDOWN, "speed_knots": ship.current_speed_knots}
            })

        elif cmd == DETOUR:
            # Update target speed if specified
            target_spd = action.get("target_speed_knots")
            if target_spd is not None:
                ship.current_speed_knots = float(target_spd)

            # Detour convention:
            # detour_waypoints excludes current position and baseline rejoin.
            # rejoin_route_index points into original_route.
            # resolved remaining path: detour points + original_route[rejoin_route_index:]
            rejoin_idx = action.get("rejoin_route_index")
            detour_wps = action.get("detour_waypoints") or []

            new_route_pts: List[Dict[str, float]] = []
            for wp in detour_wps:
                new_route_pts.append({"lat": float(wp["lat"]), "lon": float(wp["lon"])})

            if rejoin_idx is not None and 0 <= rejoin_idx < len(ship.original_route):
                # Append rejoin point and remaining original route
                new_route_pts.extend(ship.original_route[rejoin_idx:])
            elif ship.current_route:
                # Fallback to remaining current_route if no rejoin given
                new_route_pts.extend(ship.current_route)

            ship.current_route = new_route_pts
            ship.current_route_m = [project(p) for p in new_route_pts]

            events_log.append({
                "sim_time_s": sim_time_s,
                "type": "COMMAND_APPLIED",
                "ship_id": ship.ship_id,
                "details": {
                    "command": DETOUR,
                    "speed_knots": ship.current_speed_knots,
                    "rejoin_route_index": rejoin_idx,
                    "waypoints_count": len(detour_wps)
                }
            })

        elif cmd == RESUME:
            ship.current_speed_knots = ship.service_speed_knots
            # If a forward rejoin is specified, route directly to original_route[rejoin_idx:]
            rejoin_idx = action.get("rejoin_route_index")
            if rejoin_idx is not None and 0 <= rejoin_idx < len(ship.original_route):
                new_route_pts = list(ship.original_route[rejoin_idx:])
                ship.current_route = new_route_pts
                ship.current_route_m = [project(p) for p in new_route_pts]

            events_log.append({
                "sim_time_s": sim_time_s,
                "type": "COMMAND_APPLIED",
                "ship_id": ship.ship_id,
                "details": {"command": RESUME, "speed_knots": ship.current_speed_knots}
            })

    def check_expiries(self,
                       sim_time_s: float,
                       validate_fn: Any,
                       observation: Dict[str, Any],
                       ships_by_id: Dict[str, Any],
                       events_log: List[Dict[str, Any]]) -> List[str]:
        """Check for command expiry.
        Returns list of ship_ids that experienced fallback failure.
        """
        failed_ships = []
        expired_ships = []

        for ship_id, expiry_s in self.active_leases.items():
            if sim_time_s >= expiry_s:
                expired_ships.append(ship_id)

        for ship_id in expired_ships:
            del self.active_leases[ship_id]
            if ship_id not in ships_by_id:
                continue
            ship = ships_by_id[ship_id]
            active_cmd = ship.active_command
            if not active_cmd:
                continue

            # Architecture §9 & Tasks §2:
            # Expiry cannot teleport: restore declared speed while following remaining validated detour to forward rejoin.
            # Validate with purpose="EXPIRY_FALLBACK"
            fallback_action = dict(active_cmd)
            fallback_action["command"] = RESUME
            fallback_action["target_speed_knots"] = ship.service_speed_knots

            val_res = validate_fn(fallback_action, observation, purpose="EXPIRY_FALLBACK")
            if not val_res.get("ok", False):
                events_log.append({
                    "sim_time_s": sim_time_s,
                    "type": "EXPIRY_FALLBACK_FAILED",
                    "ship_id": ship_id,
                    "details": {"reasons": val_res.get("reasons", [])}
                })
                failed_ships.append(ship_id)
            else:
                # Restore service speed, retain forward remaining route to rejoin
                ship.current_speed_knots = ship.service_speed_knots
                ship.active_command = None
                events_log.append({
                    "sim_time_s": sim_time_s,
                    "type": "COMMAND_EXPIRED",
                    "ship_id": ship_id,
                    "details": {"speed_restored": ship.service_speed_knots}
                })

        return failed_ships

