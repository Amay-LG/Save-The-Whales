"""Truth-based encounter/exposure/delay metrics for Person 2."""
import math
from typing import Dict, Any, List, Optional, Tuple
from save_the_whales.shared.geo import distance_m, KNOTS_TO_MPS

# Constants from Tasks.md
# Encounter episode: enter at body clearance <= 500m; at most 1 contact per episode;
# exit after clearance > 750m continuously for 120s.
CLOSE_APPROACH_THRESHOLD_M = 500.0
EXIT_THRESHOLD_M = 750.0
EXIT_DWELL_TIME_S = 120.0
DEFAULT_SIGMA_EVAL_M = 300.0

def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

def p_path_gaussian(miss_dist_m: float, half_width_m: float, sigma_m: float) -> float:
    """Gaussian lateral jitter probability proxy."""
    if sigma_m <= 0:
        return 1.0 if abs(miss_dist_m) <= half_width_m else 0.0
    return _phi((half_width_m - miss_dist_m) / sigma_m) - _phi((-half_width_m - miss_dist_m) / sigma_m)

class EncounterEpisodeTracker:
    def __init__(self):
        # Key: (ship_id, whale_id) -> state dict
        # state: {"in_episode": bool, "contact_recorded": bool, "clearance_timer_s": float}
        self.episodes: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.modeled_contacts = 0
        self.close_approaches = 0

    def update(self,
               ship_id: str,
               whale_id: str,
               min_clearance_m: float,
               dt_s: float):
        """Update episode state based on min_clearance_m over interval dt_s."""
        pair = (ship_id, whale_id)
        if pair not in self.episodes:
            self.episodes[pair] = {
                "in_episode": False,
                "contact_recorded": False,
                "clearance_timer_s": 0.0
            }

        st = self.episodes[pair]

        if not st["in_episode"]:
            if min_clearance_m <= CLOSE_APPROACH_THRESHOLD_M:
                st["in_episode"] = True
                st["contact_recorded"] = False
                st["clearance_timer_s"] = 0.0
                self.close_approaches += 1
                # If contact occurs (hull clearance <= 0)
                if min_clearance_m <= 0:
                    st["contact_recorded"] = True
                    self.modeled_contacts += 1
        else:
            # Inside episode
            if min_clearance_m <= 0 and not st["contact_recorded"]:
                st["contact_recorded"] = True
                self.modeled_contacts += 1

            if min_clearance_m > EXIT_THRESHOLD_M:
                st["clearance_timer_s"] += dt_s
                if st["clearance_timer_s"] >= EXIT_DWELL_TIME_S:
                    # Episode ended
                    st["in_episode"] = False
                    st["clearance_timer_s"] = 0.0
            else:
                st["clearance_timer_s"] = 0.0

class TruthEvaluator:
    def __init__(self, config: Dict[str, Any]):
        met_cfg = config.get("metrics", {})
        self.sigma_eval_m = met_cfg.get("sigma_eval_m", DEFAULT_SIGMA_EVAL_M)
        self.whale_length_m = config.get("whales", {}).get("length_m", 25.0)

        self.episode_tracker = EncounterEpisodeTracker()
        self.whale_exposure_proxy = 0.0
        self.weather_exposure_s = 0.0

        # Transit and delay tracking: voyage_id -> { "actual_time_s", "ref_time_s", "actual_dist_m", "ref_dist_m" }
        self.voyage_transits: Dict[str, Dict[str, Any]] = {}

    def record_voyage_completion(self,
                                 voyage_id: str,
                                 actual_time_s: float,
                                 ref_time_s: float,
                                 actual_dist_m: float,
                                 ref_dist_m: float):
        self.voyage_transits[voyage_id] = {
            "actual_time_s": actual_time_s,
            "ref_time_s": ref_time_s,
            "actual_dist_m": actual_dist_m,
            "ref_dist_m": ref_dist_m,
            "completed": True
        }

    def record_voyage_failure(self, voyage_id: str):
        self.voyage_transits[voyage_id] = {
            "completed": False
        }

    def evaluate_motion_step(self,
                             ship: Any,
                             sub_segments: List[Tuple[Tuple[float, float], Tuple[float, float], float, float, float]],
                             whale_movements: List[Tuple[str, Tuple[float, float], Tuple[float, float], float]],
                             dt_s: float):
        """Evaluate swept relative motion between all ship subsegments and whale movements."""
        hull_half_w = ship.beam_m / 2.0
        hull_radius = 0.5 * math.hypot(ship.length_m, ship.beam_m)

        for s_p0, s_p1, speed_kn, s_t0_frac, s_t1_frac in sub_segments:
            seg_len = distance_m(s_p0, s_p1)
            seg_dt = (s_t1_frac - s_t0_frac) * dt_s
            speed_weight = (speed_kn / max(ship.service_speed_knots, 1.0)) ** 2

            for wid, w_p0, w_p1, w_exposure in whale_movements:
                # Interpolate whale positions to match sub-segment fractions
                wp0 = (w_p0[0] + (w_p1[0] - w_p0[0]) * s_t0_frac,
                       w_p0[1] + (w_p1[1] - w_p0[1]) * s_t0_frac)
                wp1 = (w_p0[0] + (w_p1[0] - w_p0[0]) * s_t1_frac,
                       w_p0[1] + (w_p1[1] - w_p0[1]) * s_t1_frac)

                # Relative CPA over segment
                r0 = (wp0[0] - s_p0[0], wp0[1] - s_p0[1])
                dr = ((wp1[0] - wp0[0]) - (s_p1[0] - s_p0[0]),
                      (wp1[1] - wp0[1]) - (s_p1[1] - s_p0[1]))

                dr_sq = dr[0] * dr[0] + dr[1] * dr[1]
                if dr_sq == 0:
                    tau = 0.0
                    cpa_dist = math.hypot(r0[0], r0[1])
                else:
                    tau = - (r0[0] * dr[0] + r0[1] * dr[1]) / dr_sq
                    tau = max(0.0, min(1.0, tau))
                    cpa_dist = math.hypot(r0[0] + dr[0] * tau, r0[1] + dr[1] * tau)

                # Body clearance: distance between bounding spheres
                clearance = cpa_dist - (hull_radius + self.whale_length_m / 2.0)
                self.episode_tracker.update(ship.ship_id, wid, clearance, seg_dt)

                # Smooth exposure accumulation
                # p_path * exposure * speed_weight * seg_dt
                eff_half_width = hull_half_w + (self.whale_length_m / 2.0)
                p_p = p_path_gaussian(cpa_dist, eff_half_width, self.sigma_eval_m)
                exposure_inc = p_p * w_exposure * speed_weight * (seg_dt / 60.0)
                self.whale_exposure_proxy += exposure_inc

    def compute_summary(self,
                        status: str,
                        elapsed_wall_s: float,
                        simulated_duration_s: float,
                        total_voyages: int,
                        alerts_count: int,
                        route_changes_count: int,
                        rejected_actions_count: int) -> Dict[str, Any]:
        """Compute final RunSummary dictionary."""
        completed_count = sum(1 for v in self.voyage_transits.values() if v.get("completed", False))
        failed_count = total_voyages - completed_count

        total_delay_s = 0.0
        total_extra_dist_m = 0.0
        all_completed = (completed_count == total_voyages and total_voyages > 0)

        if all_completed:
            for v in self.voyage_transits.values():
                total_delay_s += max(0.0, v["actual_time_s"] - v["ref_time_s"])
                total_extra_dist_m += max(0.0, v["actual_dist_m"] - v["ref_dist_m"])
            final_delay = total_delay_s
            final_dist = total_extra_dist_m
        else:
            final_delay = None
            final_dist = None

        return {
            "status": status,
            "elapsed_wall_s": elapsed_wall_s,
            "simulated_duration_s": simulated_duration_s,
            "voyages_completed": completed_count,
            "voyages_failed": failed_count,
            "modeled_contacts": self.episode_tracker.modeled_contacts,
            "close_approaches": self.episode_tracker.close_approaches,
            "whale_exposure_proxy": round(self.whale_exposure_proxy, 4),
            "weather_exposure_s": round(self.weather_exposure_s, 2),
            "added_delay_s": round(final_delay, 2) if final_delay is not None else None,
            "extra_distance_m": round(final_dist, 2) if final_dist is not None else None,
            "alerts": alerts_count,
            "route_changes": route_changes_count,
            "rejected_actions": rejected_actions_count
        }

