"""Threat detection: dedupe detections, CPA/TCPA of each ship's projected track vs whales."""
from dataclasses import dataclass
from typing import List


@dataclass
class Threat:
    ship_id: str
    whale_lat: float
    whale_lon: float
    cpa_m: float
    tcpa_min: float
    confidence: float


def cluster_detections(detections, radius_m=1000, max_age_h=6) -> List[dict]:
    """Merge detections likely to be the same whale. TODO."""
    return detections


def find_threats(obs) -> List[Threat]:
    """For each ship, project along current_route at SOG; flag clusters with small CPA and TCPA < horizon. TODO."""
    return []
