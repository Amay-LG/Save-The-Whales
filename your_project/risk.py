"""Risk engine for the whale-strike advisor.

Computes vessel–whale encounter risk using multi-leg route projection,
swept hull corridors, speed-dependent severity (Vanderlaan & Taggart 2007),
and Gaussian exposure probability.
"""

import math
import numpy as np
from dataclasses import dataclass, field
from typing import Optional

from your_project.projection import (
    project, distance_m, velocity_from_cog_sog, heading_rad,
    path_length_m, KN_TO_MS, MS_TO_KN,
)


# ── Data classes ────────────────────────────────────────────────────────────


@dataclass
class RouteSegment:
    """One straight-line segment of a projected vessel route."""

    p0: tuple[float, float]   # start (x, y) metres
    p1: tuple[float, float]   # end   (x, y) metres
    speed_ms: float           # vessel speed on this segment
    duration_s: float         # time to traverse
    length_m: float           # segment length
    heading_rad: float        # heading angle (radians, 0 = north, clockwise)


@dataclass
class VesselForecast:
    """Projected vessel path as a sequence of segments."""

    ship_id: str
    segments: list[RouteSegment]
    total_distance_m: float
    total_time_s: float
    service_speed_kn: float
    beam_m: float
    length_m: float


@dataclass
class EncounterRegion:
    """A spatial region where an encounter may occur."""

    x: float
    y: float
    radius_m: float
    time_from_now_s: float     # when the encounter would happen
    risk_contribution: float   # how much this contributes to total risk


@dataclass
class RiskAssessment:
    """Complete risk assessment for one vessel."""

    ship_id: str
    risk_score: float            # 0–1 relative risk index
    expected_encounters: float   # integrated exposure metric
    severity_weighted: float     # encounters weighted by P_lethal(v)
    min_cpa_m: float             # closest approach across all whale hypotheses
    min_tcpa_s: float            # time to closest approach
    encounter_regions: list[EncounterRegion] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _phi(z: float) -> float:
    """Standard normal CDF Φ(z) via math.erf."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ── 1. Route projection ────────────────────────────────────────────────────


def project_vessel_route(
    ship_obs: dict,
    horizon_s: float = 1800.0,
) -> VesselForecast:
    """Project a vessel's future path from its observation data.

    Parameters
    ----------
    ship_obs : dict
        A contract *ShipObs* dict with at least the keys:
        ``ship_id``, ``lat``, ``lon``, ``sog_knots``, ``cog_deg``,
        ``length_m``, ``beam_m``, ``service_speed_knots``,
        ``original_route``, ``current_route``, ``active_command``.
    horizon_s : float
        How far into the future (seconds) to project.

    Returns
    -------
    VesselForecast
        The projected route broken into straight-line segments.

    Algorithm
    ---------
    1. Project current position to XY.
    2. If ``current_route`` has waypoints, build segments along them
       (capping at *horizon_s*).
    3. Otherwise project straight ahead on COG/SOG for *horizon_s*.
    """

    ship_id: str = ship_obs.get("ship_id", "unknown")
    lat: float = ship_obs.get("lat", 0.0)
    lon: float = ship_obs.get("lon", 0.0)
    sog_kn: float = ship_obs.get("sog_knots", 0.0)
    cog_deg: float = ship_obs.get("cog_deg", 0.0)
    ship_length: float = ship_obs.get("length_m", 200.0)
    beam: float = ship_obs.get("beam_m", 30.0)
    service_kn: float = ship_obs.get("service_speed_knots", 12.0)

    current_route: list[dict] = ship_obs.get("current_route", None) or []

    # Decide effective speed: prefer SOG, fall back to service speed
    if sog_kn < 0.5:
        effective_kn = service_kn
    else:
        effective_kn = sog_kn
    effective_ms = effective_kn * KN_TO_MS

    pos_xy = project(lat, lon)
    segments: list[RouteSegment] = []
    total_dist = 0.0
    total_time = 0.0

    if len(current_route) >= 1:
        # Build waypoint list starting from current position
        waypoints_xy = [pos_xy]
        for wp in current_route:
            wp_lat = wp.get("lat", 0.0)
            wp_lon = wp.get("lon", 0.0)
            waypoints_xy.append(project(wp_lat, wp_lon))

        for i in range(len(waypoints_xy) - 1):
            p0 = waypoints_xy[i]
            p1 = waypoints_xy[i + 1]
            seg_len = distance_m(p0, p1)

            if seg_len < 1.0:
                # Degenerate segment – skip
                continue

            if effective_ms < 0.01:
                # Vessel effectively stationary
                break

            seg_duration = seg_len / effective_ms

            # Cap this segment if it would exceed the horizon
            remaining_time = horizon_s - total_time
            if remaining_time <= 0:
                break

            if seg_duration > remaining_time:
                # Truncate segment to fit within horizon
                frac = remaining_time / seg_duration
                p1 = (
                    p0[0] + frac * (p1[0] - p0[0]),
                    p0[1] + frac * (p1[1] - p0[1]),
                )
                seg_len *= frac
                seg_duration = remaining_time

            seg_heading = heading_rad(p0, p1)
            seg = RouteSegment(
                p0=p0,
                p1=p1,
                speed_ms=effective_ms,
                duration_s=seg_duration,
                length_m=seg_len,
                heading_rad=seg_heading,
            )
            segments.append(seg)
            total_dist += seg_len
            total_time += seg_duration

            if total_time >= horizon_s:
                break
    else:
        # No waypoints – straight-ahead projection on COG
        if effective_ms < 0.01:
            # Vessel stationary: single zero-length segment
            seg = RouteSegment(
                p0=pos_xy,
                p1=pos_xy,
                speed_ms=0.0,
                duration_s=horizon_s,
                length_m=0.0,
                heading_rad=math.radians(cog_deg),
            )
            segments.append(seg)
            total_time = horizon_s
        else:
            vx, vy = velocity_from_cog_sog(cog_deg, effective_kn)
            dist = effective_ms * horizon_s
            end_xy = (pos_xy[0] + vx * horizon_s, pos_xy[1] + vy * horizon_s)
            seg_heading = heading_rad(pos_xy, end_xy)

            seg = RouteSegment(
                p0=pos_xy,
                p1=end_xy,
                speed_ms=effective_ms,
                duration_s=horizon_s,
                length_m=dist,
                heading_rad=seg_heading,
            )
            segments.append(seg)
            total_dist = dist
            total_time = horizon_s

    return VesselForecast(
        ship_id=ship_id,
        segments=segments,
        total_distance_m=total_dist,
        total_time_s=total_time,
        service_speed_kn=service_kn,
        beam_m=beam,
        length_m=ship_length,
    )


# ── 2. CPA / TCPA calculation (multi-segment) ──────────────────────────────


def calculate_cpa_segment(
    seg: RouteSegment,
    whale_x: float,
    whale_y: float,
    whale_uncertainty_m: float = 0.0,
    time_offset_s: float = 0.0,
) -> tuple[float, float, float]:
    """Calculate CPA between a vessel route segment and a stationary whale.

    The whale is treated as stationary with an uncertainty radius because
    its motion is unpredictable (unlike ship–ship CPA).

    Parameters
    ----------
    seg : RouteSegment
        One leg of the projected route.
    whale_x, whale_y : float
        Whale position in projected metres.
    whale_uncertainty_m : float
        Position-uncertainty radius (subtracted from raw CPA distance).
    time_offset_s : float
        Absolute time corresponding to the start of this segment.

    Returns
    -------
    tcpa_s : float
        Absolute time of CPA (time_offset_s + local time on segment).
    cpa_distance_m : float
        Closest approach distance in metres (reduced by uncertainty, min 0).
    along_track_fraction : float
        0–1 position along the segment where CPA occurs.
    """

    dx_seg = seg.p1[0] - seg.p0[0]
    dy_seg = seg.p1[1] - seg.p0[1]
    seg_len = seg.length_m

    # Degenerate (zero-length) segment
    if seg_len < 1e-6:
        raw_dist = math.hypot(whale_x - seg.p0[0], whale_y - seg.p0[1])
        cpa_dist = max(0.0, raw_dist - whale_uncertainty_m)
        return (time_offset_s, cpa_dist, 0.0)

    # Unit direction vector along segment
    inv_len = 1.0 / seg_len
    ux = dx_seg * inv_len
    uy = dy_seg * inv_len

    # Relative position of whale from segment start
    rx = whale_x - seg.p0[0]
    ry = whale_y - seg.p0[1]

    # Along-track distance, clamped to [0, seg_len]
    s = rx * ux + ry * uy
    s = max(0.0, min(seg_len, s))

    # CPA point on track
    cpx = seg.p0[0] + s * ux
    cpy = seg.p0[1] + s * uy

    raw_dist = math.hypot(whale_x - cpx, whale_y - cpy)
    cpa_dist = max(0.0, raw_dist - whale_uncertainty_m)

    # Along-track fraction
    frac = s / seg_len

    # Time to CPA
    if seg.speed_ms > 1e-6:
        local_t = s / seg.speed_ms
    else:
        local_t = 0.0

    tcpa = time_offset_s + local_t
    return (tcpa, cpa_dist, frac)


def calculate_route_cpa(
    forecast: VesselForecast,
    whale_x: float,
    whale_y: float,
    whale_uncertainty_m: float = 0.0,
) -> tuple[float, float, int]:
    """Find the minimum CPA across all segments of a vessel forecast.

    Parameters
    ----------
    forecast : VesselForecast
        Projected vessel route.
    whale_x, whale_y : float
        Whale position in projected metres.
    whale_uncertainty_m : float
        Position-uncertainty radius.

    Returns
    -------
    min_tcpa_s : float
        Time of the closest approach (seconds from now).
    min_cpa_m : float
        Minimum CPA distance in metres.
    segment_index : int
        Index of the segment where the minimum CPA occurs.
    """

    if not forecast.segments:
        # No route information – return large defaults
        return (0.0, float("inf"), -1)

    min_cpa = float("inf")
    min_tcpa = 0.0
    min_idx = 0
    time_offset = 0.0

    for idx, seg in enumerate(forecast.segments):
        tcpa, cpa, _ = calculate_cpa_segment(
            seg, whale_x, whale_y, whale_uncertainty_m, time_offset
        )
        if cpa < min_cpa:
            min_cpa = cpa
            min_tcpa = tcpa
            min_idx = idx
        time_offset += seg.duration_s

    return (min_tcpa, min_cpa, min_idx)


# ── 3. Speed-dependent severity ────────────────────────────────────────────


def p_lethal(v_kn: float) -> float:
    """Probability of lethal injury given a strike, Vanderlaan & Taggart 2007.

    .. math::
        P(v) = \\frac{1}{1 + \\exp(-(-4.89 + 0.41 \\cdot v))}

    Reference values (knots → P):
        8.6 → 0.20, 10 → 0.31, 12 → 0.51, 15 → 0.78, 20 → 0.97

    Parameters
    ----------
    v_kn : float
        Vessel speed in knots.

    Returns
    -------
    float
        Probability in [0, 1].
    """

    z = -4.89 + 0.41 * v_kn
    return 1.0 / (1.0 + math.exp(-z))


def p_path_gaussian(
    lateral_miss_m: float,
    half_width_m: float,
    sigma_m: float,
) -> float:
    """Probability of a whale being within the hull strip under Gaussian lateral jitter.

    .. math::
        P = \\Phi\\bigl(\\frac{w - d}{\\sigma}\\bigr)
          - \\Phi\\bigl(\\frac{-w - d}{\\sigma}\\bigr)

    where Φ is the standard normal CDF, *w* is ``half_width_m``,
    *d* is ``lateral_miss_m``, and σ is ``sigma_m``.

    Parameters
    ----------
    lateral_miss_m : float
        Signed or unsigned lateral distance from track centreline to the whale.
    half_width_m : float
        Half the effective swept width (beam/2 + whale_length/2).
    sigma_m : float
        Standard deviation of lateral position uncertainty (metres).

    Returns
    -------
    float
        Probability in [0, 1].
    """

    if sigma_m < 1e-6:
        # Degenerate case: if sigma → 0 the Gaussian becomes a delta.
        # Whale is "in the strip" iff |d| <= w
        return 1.0 if abs(lateral_miss_m) <= half_width_m else 0.0

    d = abs(lateral_miss_m)  # symmetry – use magnitude
    return _phi((half_width_m - d) / sigma_m) - _phi((-half_width_m - d) / sigma_m)


# ── 4. Integrated route risk ───────────────────────────────────────────────


def compute_route_risk(
    forecast: VesselForecast,
    whale_positions: list[tuple[float, float, float, float, list[str]]],
    regional_field=None,
    sigma_m: float = 300.0,
    whale_length_m: float = 25.0,
) -> RiskAssessment:
    """Compute integrated risk for a vessel route against all whale information.

    Parameters
    ----------
    forecast : VesselForecast
        Projected vessel path.
    whale_positions : list of (x, y, uncertainty_m, confidence, evidence_ids)
        Whale hypothesis tuples. ``confidence`` is in [0, 1].
    regional_field : object, optional
        Object with a ``.query(x, y) -> float`` method returning background
        whale density / risk at a point.
    sigma_m : float
        Standard deviation for Gaussian lateral jitter (metres).
    whale_length_m : float
        Representative whale body length (metres).

    Returns
    -------
    RiskAssessment
        Aggregated risk result for this vessel.

    Notes
    -----
    Risk normalization uses ``risk_score = 1 - exp(-total_weighted_encounters)``
    which maps raw exposure into [0, 1] with diminishing returns.
    """

    half_hull_width = forecast.beam_m / 2.0
    half_width = half_hull_width + whale_length_m / 2.0  # effective swept half-width

    total_exposure = 0.0          # sum of encounter probabilities
    total_severity = 0.0          # exposure weighted by P_lethal
    min_cpa = float("inf")
    min_tcpa = 0.0
    encounter_regions: list[EncounterRegion] = []
    all_evidence: set[str] = set()
    significant_evidence: set[str] = set()

    time_offset = 0.0

    for seg in forecast.segments:
        seg_speed_kn = seg.speed_ms * MS_TO_KN
        p_leth = p_lethal(seg_speed_kn)

        for whale_tuple in whale_positions:
            wx, wy, w_unc, w_conf, w_eids = whale_tuple

            # CPA for this segment
            tcpa, cpa_dist, along_frac = calculate_cpa_segment(
                seg, wx, wy, whale_uncertainty_m=0.0, time_offset_s=time_offset
            )

            # Track global minimum CPA (raw, without subtracting uncertainty)
            if cpa_dist < min_cpa:
                min_cpa = cpa_dist
                min_tcpa = tcpa

            # Gaussian path probability (lateral miss ≈ cpa_dist)
            p_path = p_path_gaussian(cpa_dist, half_width, sigma_m)

            # Exposure = whale confidence * path probability
            exposure = w_conf * p_path
            total_exposure += exposure
            total_severity += exposure * p_leth

            # Determine if this qualifies as an encounter region
            encounter_threshold = w_unc + half_hull_width
            if cpa_dist < encounter_threshold + 3.0 * sigma_m:
                # CPA point for the encounter region
                if seg.length_m > 1e-6:
                    inv_len = 1.0 / seg.length_m
                    ux = (seg.p1[0] - seg.p0[0]) * inv_len
                    uy = (seg.p1[1] - seg.p0[1]) * inv_len
                else:
                    ux, uy = 0.0, 0.0
                s = along_frac * seg.length_m
                enc_x = seg.p0[0] + s * ux
                enc_y = seg.p0[1] + s * uy

                enc = EncounterRegion(
                    x=enc_x,
                    y=enc_y,
                    radius_m=max(w_unc, sigma_m),
                    time_from_now_s=tcpa,
                    risk_contribution=exposure * p_leth,
                )
                encounter_regions.append(enc)

            # Collect evidence
            if w_eids:
                all_evidence.update(w_eids)
                if exposure > 0.01:
                    significant_evidence.update(w_eids)

        # Regional / background field contribution
        if regional_field is not None and seg.length_m > 1e-6:
            # Sample at start, midpoint, and end
            mid_x = (seg.p0[0] + seg.p1[0]) / 2.0
            mid_y = (seg.p0[1] + seg.p1[1]) / 2.0

            bg_samples = [
                regional_field.query(seg.p0[0], seg.p0[1]),
                regional_field.query(mid_x, mid_y),
                regional_field.query(seg.p1[0], seg.p1[1]),
            ]
            avg_bg = sum(bg_samples) / 3.0

            # Scale background contribution by segment length relative to
            # a reference distance (1 km), and dampen relative to localised
            bg_exposure = avg_bg * (seg.length_m / 1000.0) * 0.1
            total_exposure += bg_exposure
            total_severity += bg_exposure * p_leth

        time_offset += seg.duration_s

    # Risk normalisation: 1 - exp(-λ)  →  [0, 1] with diminishing returns
    risk_score = 1.0 - math.exp(-total_severity) if total_severity > 0 else 0.0
    risk_score = max(0.0, min(1.0, risk_score))

    # Use significant evidence if any, otherwise fall back to all evidence
    used_evidence = sorted(significant_evidence) if significant_evidence else sorted(all_evidence)

    return RiskAssessment(
        ship_id=forecast.ship_id,
        risk_score=risk_score,
        expected_encounters=total_exposure,
        severity_weighted=total_severity,
        min_cpa_m=min_cpa if min_cpa < float("inf") else 0.0,
        min_tcpa_s=min_tcpa,
        encounter_regions=encounter_regions,
        evidence_ids=used_evidence,
    )


# ── 5. Risk comparison helper ──────────────────────────────────────────────


def compare_route_risks(
    current: RiskAssessment,
    candidate: RiskAssessment,
) -> dict:
    """Compare two risk assessments for the same vessel.

    Parameters
    ----------
    current : RiskAssessment
        Risk on the vessel's current route.
    candidate : RiskAssessment
        Risk on a proposed alternative route.

    Returns
    -------
    dict
        Comparison metrics:

        - **risk_reduction** – ``current.risk_score - candidate.risk_score``
        - **risk_ratio** – ``candidate.risk_score / max(current.risk_score, 1e-9)``
        - **severity_reduction** – ``current.severity_weighted - candidate.severity_weighted``
        - **improvement** – ``True`` if the candidate is meaningfully better
    """

    risk_reduction = current.risk_score - candidate.risk_score
    denom = max(current.risk_score, 1e-9)
    risk_ratio = candidate.risk_score / denom
    severity_reduction = current.severity_weighted - candidate.severity_weighted

    # "Meaningfully better" heuristic: at least 5 % absolute *or* 20 % relative
    improvement = (risk_reduction > 0.05) or (risk_ratio < 0.80 and risk_reduction > 0.0)

    return {
        "risk_reduction": risk_reduction,
        "risk_ratio": risk_ratio,
        "severity_reduction": severity_reduction,
        "improvement": improvement,
    }
