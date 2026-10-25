"""Sensor simulation: noisy, range/probability-limited sightings with delay queue."""
import math
import random
from typing import Dict, Any, List, Tuple
from save_the_whales.shared.geo import project, unproject, distance_m

class SensorSuite:
    def __init__(self, config: Dict[str, Any], seed: int):
        sensor_cfg = config.get("sensors", {})
        self.r_visual_m = sensor_cfg.get("r_visual_km", 3.0) * 1000.0
        self.p_detect = sensor_cfg.get("p_detect", 0.5)
        self.sigma_obs_m = sensor_cfg.get("sigma_obs_m", 200.0)
        self.latency_max_s = sensor_cfg.get("latency_max_min", 30) * 60.0
        self.max_detection_age_s = sensor_cfg.get("max_detection_age_s", 1800.0)
        self.max_retained = sensor_cfg.get("max_retained_detections", 50)
        self.seed = seed
        
        # Internal queue: list of (available_at_s, detection_dict)
        self.due_queue: List[Tuple[float, Dict[str, Any]]] = []
        self.detection_counter = 0

    def observe(self,
                sim_time_s: float,
                whales: List[Any],
                ships: List[Any],
                now_iso: str):
        """Sample sensors at sim_time_s from whales and active ships."""
        for w in whales:
            # Sighting requires whale at surface (exposure >= 0.8)
            if w.exposure < 0.8:
                continue

            for s in ships:
                if s.arrived:
                    continue

                dist = distance_m(s.pos_m, w.pos_m)
                if dist <= self.r_visual_m:
                    # Keyed random draw for detection probability and noise
                    key = f"{self.seed}:sensor:{w.whale_id}:{s.ship_id}:{int(sim_time_s)}"
                    rng = random.Random(key)
                    if rng.random() <= self.p_detect:
                        # Successful observation
                        noise_x = rng.gauss(0.0, self.sigma_obs_m)
                        noise_y = rng.gauss(0.0, self.sigma_obs_m)
                        obs_x = w.pos_m[0] + noise_x
                        obs_y = w.pos_m[1] + noise_y
                        obs_pt = unproject(obs_x, obs_y)

                        latency_s = rng.uniform(0.0, self.latency_max_s)
                        avail_s = sim_time_s + latency_s

                        self.detection_counter += 1
                        det_id = f"det_{self.detection_counter:06d}"

                        detection = {
                            "detection_id": det_id,
                            "lat": obs_pt["lat"],
                            "lon": obs_pt["lon"],
                            "position_error_m": self.sigma_obs_m,
                            "observed_at": now_iso,
                            "reported_at": now_iso,  # will be stamped or passed
                            "source": f"visual:{s.source_mmsi}",
                            "species": "blue"
                        }
                        self.due_queue.append((avail_s, detection))

    def get_reported(self, sim_time_s: float) -> List[Dict[str, Any]]:
        """Return all reported detections whose availability <= sim_time_s and age <= max_detection_age_s."""
        reported = []
        new_queue = []
        for avail_s, det in self.due_queue:
            if avail_s <= sim_time_s:
                # Retain if within age bound
                reported.append(det)
            else:
                new_queue.append((avail_s, det))

        self.due_queue = new_queue
        # Deduplicate by detection_id and cap at max_retained
        unique_dets: Dict[str, Dict[str, Any]] = {}
        for d in reported:
            unique_dets[d["detection_id"]] = d
            
        res = list(unique_dets.values())
        if len(res) > self.max_retained:
            res = res[-self.max_retained:]
        return res

