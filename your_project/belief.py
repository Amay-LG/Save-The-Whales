"""Whale belief model — processes raw whale detection evidence and maintains
two representations of where whales might be:

1. **Localized hypotheses** — specific estimated whale positions with uncertainty
2. **Regional risk field** — a grid of background whale risk

Used by the advisor to inform routing decisions and speed recommendations.
"""

import math
import numpy as np
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone

from your_project.projection import project, distance_m, KMH_TO_MS


# ── Timestamp helper ────────────────────────────────────────────────────────


def _parse_iso(ts: str) -> datetime:
    """Parse ISO-8601 timestamp string to datetime. Handle 'Z' suffix."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ── Data types ──────────────────────────────────────────────────────────────


@dataclass
class WhaleHypothesis:
    """A localized whale position hypothesis derived from clustered detections."""

    hypothesis_id: str          # e.g. "wh_001"
    x: float                    # metres, local projection (east)
    y: float                    # metres, local projection (north)
    uncertainty_m: float        # current radius of position uncertainty
    confidence: float           # 0.0–1.0
    evidence_ids: list[str] = field(default_factory=list)   # contributing detection_ids
    last_observed: str = ""     # ISO-8601 timestamp of most recent evidence
    species: str = "unknown"    # preserve actual species from detection
    provenance_group: str = ""  # for dedup in regional field


# ── Clustering algorithm ───────────────────────────────────────────────────


def cluster_detections(
    new_detections: list[dict],
    existing: list[WhaleHypothesis],
    cluster_radius_m: float = 1000.0,
    max_age_s: float = 21600.0,
) -> tuple[list[WhaleHypothesis], list[dict]]:
    """Cluster new point-sighting detections against existing hypotheses.

    Algorithm (greedy nearest-neighbour):
      1. Sort new detections by observed_at (oldest first)
      2. For each detection:
         a. Project (lat, lon) to local XY
         b. Find nearest existing hypothesis within *cluster_radius_m*
            whose ``last_observed`` is within *max_age_s* of this detection's
            ``observed_at``
         c. If found: merge into it (weighted centroid, updated uncertainty,
            add evidence_id)
         d. If not found: create a new hypothesis
      3. Return (updated_hypotheses, unmatched_detections)

    Merging rules:
      - New position = weighted average of old and new (weighted by
        1/uncertainty²)
      - New uncertainty = max(old_uncertainty, detection.position_error_m)
      - New confidence = min(1.0, old_confidence + 0.2)
      - Append detection_id to evidence_ids
      - Update last_observed to the later timestamp
      - Preserve species (prefer non-"unknown")

    Parameters
    ----------
    new_detections : list[dict]
        Detections with keys: detection_id, species, lat, lon,
        position_error_m, observed_at, reported_at, source.
    existing : list[WhaleHypothesis]
        Mutable list — updated in-place **and** returned.
    cluster_radius_m : float
        Maximum distance (m) to merge a detection into an existing hypothesis.
    max_age_s : float
        Maximum time difference (s) between detection and hypothesis to allow
        merging.

    Returns
    -------
    tuple[list[WhaleHypothesis], list[dict]]
        (updated hypothesis list, list of detections that created new
        hypotheses)
    """
    # Sort oldest-first
    sorted_dets = sorted(new_detections, key=lambda d: d.get("observed_at", ""))

    unmatched: list[dict] = []

    for det in sorted_dets:
        dx, dy = project(det["lat"], det["lon"])
        det_time = _parse_iso(det["observed_at"])
        det_unc = max(det.get("position_error_m", 500.0), 1.0)  # guard zero

        best_idx: Optional[int] = None
        best_dist = float("inf")

        for i, hyp in enumerate(existing):
            d = distance_m((dx, dy), (hyp.x, hyp.y))
            if d > cluster_radius_m:
                continue
            # Check time difference
            if hyp.last_observed:
                hyp_time = _parse_iso(hyp.last_observed)
                elapsed = abs((det_time - hyp_time).total_seconds())
                if elapsed > max_age_s:
                    continue
            if d < best_dist:
                best_dist = d
                best_idx = i

        if best_idx is not None:
            # ── Merge into existing hypothesis ──────────────────────────
            hyp = existing[best_idx]
            # Weighted centroid by 1/uncertainty²
            w_old = 1.0 / (hyp.uncertainty_m ** 2) if hyp.uncertainty_m > 0 else 1.0
            w_new = 1.0 / (det_unc ** 2)
            total_w = w_old + w_new
            hyp.x = (hyp.x * w_old + dx * w_new) / total_w
            hyp.y = (hyp.y * w_old + dy * w_new) / total_w
            hyp.uncertainty_m = max(hyp.uncertainty_m, det_unc)
            hyp.confidence = min(1.0, hyp.confidence + 0.2)
            hyp.evidence_ids.append(det["detection_id"])
            # Update last_observed to the later timestamp
            if hyp.last_observed:
                hyp_time = _parse_iso(hyp.last_observed)
                if det_time > hyp_time:
                    hyp.last_observed = det["observed_at"]
            else:
                hyp.last_observed = det["observed_at"]
            # Prefer non-"unknown" species
            det_species = det.get("species", "unknown")
            if hyp.species == "unknown" and det_species != "unknown":
                hyp.species = det_species
        else:
            # Detection doesn't match any existing hypothesis — will be
            # turned into a new hypothesis by the caller.
            unmatched.append(det)

    return existing, unmatched


# ── Regional risk field ────────────────────────────────────────────────────


class RegionalRiskField:
    """A 2D grid covering the bounding box at a configurable cell size.

    Each cell holds a scalar risk value in [0, 1]. The grid supports additive
    contributions from sightings (Gaussian blobs), area-presence evidence
    (smooth circular regions), and habitat priors (configurable hotspots).
    """

    def __init__(
        self,
        x_min: float,
        y_min: float,
        x_max: float,
        y_max: float,
        cell_size_m: float = 1000.0,
    ):
        """Create an empty risk grid stored as a 2D numpy array.

        Parameters
        ----------
        x_min, y_min, x_max, y_max : float
            Bounding box in projected metres.
        cell_size_m : float
            Side length of each square cell, in metres.
        """
        self.x_min = x_min
        self.y_min = y_min
        self.x_max = x_max
        self.y_max = y_max
        self.cell_size = cell_size_m

        self.nx = max(1, int(math.ceil((x_max - x_min) / cell_size_m)))
        self.ny = max(1, int(math.ceil((y_max - y_min) / cell_size_m)))

        self.grid: np.ndarray = np.zeros((self.nx, self.ny), dtype=np.float64)

        # Track provenance groups that have already contributed to prevent
        # double-counting.
        self._contributed: set[str] = set()

    # ── helpers ──────────────────────────────────────────────────────────

    def _xy_to_ix(self, x: float) -> int:
        """Convert x coordinate to grid index (clamped)."""
        return int((x - self.x_min) / self.cell_size)

    def _xy_to_iy(self, y: float) -> int:
        """Convert y coordinate to grid index (clamped)."""
        return int((y - self.y_min) / self.cell_size)

    def _cell_center(self, ix: int, iy: int) -> tuple[float, float]:
        """Return the (x, y) world-coordinate of the centre of cell (ix, iy)."""
        cx = self.x_min + (ix + 0.5) * self.cell_size
        cy = self.y_min + (iy + 0.5) * self.cell_size
        return cx, cy

    def _clamp_grid(self) -> None:
        """Clamp all risk values to [0, 1]."""
        np.clip(self.grid, 0.0, 1.0, out=self.grid)

    # ── public interface ─────────────────────────────────────────────────

    def update_from_sighting(
        self,
        x: float,
        y: float,
        uncertainty_m: float,
        weight: float = 1.0,
        provenance_group: str = "",
    ) -> None:
        """Add Gaussian contribution centred on (x, y) with sigma = uncertainty_m.

        Only add if this *provenance_group* hasn't already contributed.

        Parameters
        ----------
        x, y : float
            Centre of the sighting in projected metres.
        uncertainty_m : float
            Standard deviation (sigma) of the Gaussian blob.
        weight : float
            Peak amplitude scaling factor.
        provenance_group : str
            Identifier for deduplication; empty string is never deduplicated.
        """
        if provenance_group and provenance_group in self._contributed:
            return
        if provenance_group:
            self._contributed.add(provenance_group)

        sigma = max(uncertainty_m, 1.0)  # guard against zero
        radius = 3.0 * sigma

        # Determine index range to update
        ix_min = max(0, int((x - radius - self.x_min) / self.cell_size))
        ix_max = min(self.nx - 1, int((x + radius - self.x_min) / self.cell_size))
        iy_min = max(0, int((y - radius - self.y_min) / self.cell_size))
        iy_max = min(self.ny - 1, int((y + radius - self.y_min) / self.cell_size))

        if ix_min > ix_max or iy_min > iy_max:
            return

        # Build cell-centre coordinate arrays
        ixs = np.arange(ix_min, ix_max + 1)
        iys = np.arange(iy_min, iy_max + 1)
        cxs = self.x_min + (ixs + 0.5) * self.cell_size
        cys = self.y_min + (iys + 0.5) * self.cell_size

        # Meshgrid for vectorized Gaussian
        cx_grid, cy_grid = np.meshgrid(cxs, cys, indexing="ij")
        dsq = (cx_grid - x) ** 2 + (cy_grid - y) ** 2
        gauss = weight * np.exp(-0.5 * dsq / (sigma ** 2))

        self.grid[ix_min : ix_max + 1, iy_min : iy_max + 1] += gauss
        self._clamp_grid()

    def update_from_area_presence(
        self, center_x: float, center_y: float, radius_m: float, level: float
    ) -> None:
        """Add uniform/smooth contribution over a circular area.

        Used for AREA_PRESENCE evidence (e.g. acoustic detections) — no
        invented whale coordinate.

        Parameters
        ----------
        center_x, center_y : float
            Centre of the area in projected metres.
        radius_m : float
            Radius of the circular area.
        level : float
            Risk level to add within the area (will be clamped after).
        """
        radius_m = max(radius_m, 1.0)

        ix_min = max(0, int((center_x - radius_m - self.x_min) / self.cell_size))
        ix_max = min(self.nx - 1, int((center_x + radius_m - self.x_min) / self.cell_size))
        iy_min = max(0, int((center_y - radius_m - self.y_min) / self.cell_size))
        iy_max = min(self.ny - 1, int((center_y + radius_m - self.y_min) / self.cell_size))

        if ix_min > ix_max or iy_min > iy_max:
            return

        ixs = np.arange(ix_min, ix_max + 1)
        iys = np.arange(iy_min, iy_max + 1)
        cxs = self.x_min + (ixs + 0.5) * self.cell_size
        cys = self.y_min + (iys + 0.5) * self.cell_size

        cx_grid, cy_grid = np.meshgrid(cxs, cys, indexing="ij")
        dsq = (cx_grid - center_x) ** 2 + (cy_grid - center_y) ** 2

        # Smooth falloff at the edge using a cosine taper over the outer 20%
        dist = np.sqrt(dsq)
        inner = 0.8 * radius_m
        mask = np.where(
            dist <= inner,
            1.0,
            np.where(
                dist <= radius_m,
                0.5 * (1.0 + np.cos(math.pi * (dist - inner) / (radius_m - inner))),
                0.0,
            ),
        )

        self.grid[ix_min : ix_max + 1, iy_min : iy_max + 1] += level * mask
        self._clamp_grid()

    def update_from_habitat(self, hotspots: list[dict]) -> None:
        """Add background prior from hotspot config.

        Each hotspot dict must contain:
          center_x, center_y : float — projected metres
          sigma_m : float — Gaussian sigma
          weight : float — peak amplitude

        Parameters
        ----------
        hotspots : list[dict]
            List of hotspot configuration dicts.
        """
        for hs in hotspots:
            self.update_from_sighting(
                x=hs["center_x"],
                y=hs["center_y"],
                uncertainty_m=hs["sigma_m"],
                weight=hs["weight"],
                provenance_group="",  # habitat prior always applies
            )

    def decay(self, factor: float) -> None:
        """Multiply all cells by *factor* (0–1) for temporal aging.

        Parameters
        ----------
        factor : float
            Decay multiplier applied element-wise to the grid.
        """
        self.grid *= np.clip(factor, 0.0, 1.0)

    def query(self, x: float, y: float) -> float:
        """Return interpolated risk at a point. Returns 0 if outside grid.

        Uses bilinear interpolation between the four nearest cell centres.

        Parameters
        ----------
        x, y : float
            Query point in projected metres.

        Returns
        -------
        float
            Risk value in [0, 1].
        """
        # Continuous cell coordinates (relative to cell centres)
        fx = (x - self.x_min) / self.cell_size - 0.5
        fy = (y - self.y_min) / self.cell_size - 0.5

        ix0 = int(math.floor(fx))
        iy0 = int(math.floor(fy))

        # Fully outside the grid
        if ix0 < -1 or ix0 >= self.nx or iy0 < -1 or iy0 >= self.ny:
            return 0.0

        # Fractional parts
        sx = fx - ix0
        sy = fy - iy0

        def _val(i: int, j: int) -> float:
            if 0 <= i < self.nx and 0 <= j < self.ny:
                return float(self.grid[i, j])
            return 0.0

        # Bilinear interpolation
        v = (
            _val(ix0, iy0) * (1 - sx) * (1 - sy)
            + _val(ix0 + 1, iy0) * sx * (1 - sy)
            + _val(ix0, iy0 + 1) * (1 - sx) * sy
            + _val(ix0 + 1, iy0 + 1) * sx * sy
        )
        return max(0.0, min(1.0, v))

    def query_path(self, points_xy: list) -> float:
        """Maximum risk along a polyline path (sampled at each vertex).

        Parameters
        ----------
        points_xy : list
            List of (x, y) tuples describing the path vertices.

        Returns
        -------
        float
            Maximum risk value encountered.
        """
        if not points_xy:
            return 0.0
        return max(self.query(p[0], p[1]) for p in points_xy)

    def cells_above(self, threshold: float) -> list[tuple[float, float, float]]:
        """Return (x, y, risk) for cells above *threshold*.

        Used by the A* planner to enumerate high-risk cells.

        Parameters
        ----------
        threshold : float
            Minimum risk value to include.

        Returns
        -------
        list[tuple[float, float, float]]
            List of (x_centre, y_centre, risk_value) tuples.
        """
        result: list[tuple[float, float, float]] = []
        ixs, iys = np.where(self.grid > threshold)
        for ix, iy in zip(ixs, iys):
            cx, cy = self._cell_center(int(ix), int(iy))
            result.append((cx, cy, float(self.grid[ix, iy])))
        return result


# ── Main belief model ──────────────────────────────────────────────────────


class WhaleBelief:
    """Main whale belief model used by the advisor.

    Processes raw whale detection evidence from observations and maintains
    localized hypotheses (specific whale position estimates) and a regional
    risk field (grid of background whale risk).
    """

    # ── Constants ────────────────────────────────────────────────────────
    WHALE_MAX_SPEED_MS: float = 5.0 * KMH_TO_MS     # ~1.39 m/s, blue whale max swim speed
    UNKNOWN_SPECIES_UNCERTAINTY_FACTOR: float = 1.5  # unknown species get larger base uncertainty
    MIN_CONFIDENCE: float = 0.05                     # below this, drop the hypothesis

    def __init__(self, bbox_xy: tuple, cell_size_m: float = 1000.0):
        """Initialise the whale belief model.

        Parameters
        ----------
        bbox_xy : tuple
            (x_min, y_min, x_max, y_max) in projected metres.
        cell_size_m : float
            Cell size for the regional risk field grid.
        """
        self.hypotheses: list[WhaleHypothesis] = []
        self.field = RegionalRiskField(*bbox_xy, cell_size_m)
        self._next_id: int = 0
        self._bbox_xy: tuple = bbox_xy
        self._last_age_ts: Optional[str] = None

    # ── ID generation ────────────────────────────────────────────────────

    def _new_id(self) -> str:
        """Generate a unique hypothesis ID."""
        hid = f"wh_{self._next_id:03d}"
        self._next_id += 1
        return hid

    # ── Core update ──────────────────────────────────────────────────────

    def update(self, detections: list[dict], now: str) -> None:
        """Process new whale detections from the observation.

        Each detection dict has fields from ``contract.schema.WhaleDetection``:
          detection_id, species, lat, lon, position_error_m, observed_at,
          reported_at, source

        Algorithm:
          1. Separate detections by type (``source`` field):
             - ``"visual:*"`` or ``"oracle"`` → POINT_SIGHTING →
               cluster into hypotheses + regional field
             - ``"acoustic:*"`` → AREA_PRESENCE → regional field only
               (no localized hypothesis)
          2. Cluster POINT_SIGHTING detections with existing hypotheses
             (within 1000 m and 6 h)
          3. Create new hypotheses for unmatched clusters
          4. Update regional field (with provenance tracking to prevent
             double-counting)

        Parameters
        ----------
        detections : list[dict]
            Raw whale detection dicts from the current observation.
        now : str
            Current ISO-8601 timestamp.
        """
        if not detections:
            return

        point_sightings: list[dict] = []
        area_presences: list[dict] = []

        for det in detections:
            source = det.get("source", "")
            if source.startswith("acoustic"):
                area_presences.append(det)
            else:
                # visual:*, oracle, or any other source → point sighting
                point_sightings.append(det)

        # ── 1. Cluster point sightings ──────────────────────────────────
        if point_sightings:
            self.hypotheses, unmatched = cluster_detections(
                point_sightings, self.hypotheses
            )

            # Create new hypotheses for unmatched detections
            for det in unmatched:
                dx, dy = project(det["lat"], det["lon"])
                species = det.get("species", "unknown")
                base_unc = max(det.get("position_error_m", 500.0), 1.0)
                if species == "unknown":
                    base_unc *= self.UNKNOWN_SPECIES_UNCERTAINTY_FACTOR

                hyp = WhaleHypothesis(
                    hypothesis_id=self._new_id(),
                    x=dx,
                    y=dy,
                    uncertainty_m=base_unc,
                    confidence=0.5,
                    evidence_ids=[det["detection_id"]],
                    last_observed=det["observed_at"],
                    species=species,
                    provenance_group=det["detection_id"],
                )
                self.hypotheses.append(hyp)

        # ── 2. Update regional field from hypotheses ────────────────────
        for hyp in self.hypotheses:
            prov = hyp.provenance_group or hyp.hypothesis_id
            self.field.update_from_sighting(
                hyp.x,
                hyp.y,
                hyp.uncertainty_m,
                weight=hyp.confidence,
                provenance_group=prov,
            )

        # ── 3. Area-presence detections → regional field only ───────────
        for det in area_presences:
            dx, dy = project(det["lat"], det["lon"])
            unc = max(det.get("position_error_m", 5000.0), 500.0)
            self.field.update_from_area_presence(
                center_x=dx,
                center_y=dy,
                radius_m=unc,
                level=0.3,  # moderate risk contribution for acoustic detections
            )

        self._last_age_ts = now

    # ── Temporal aging ───────────────────────────────────────────────────

    def age(self, now: str, max_age_h: float = 24.0) -> None:
        """Age all hypotheses and the regional field.

        For each hypothesis:
          - Compute elapsed seconds since ``last_observed``
          - Grow uncertainty: ``uncertainty_m += WHALE_MAX_SPEED_MS * elapsed_s``
          - Decay confidence: ``confidence *= exp(-elapsed_s / (3600 * 6))``
            (6-hour half-life)
          - Drop hypotheses with ``confidence < MIN_CONFIDENCE`` or age
            exceeding *max_age_h*

        For the regional field:
          - Apply a decay factor based on elapsed time.

        Parameters
        ----------
        now : str
            Current ISO-8601 timestamp.
        max_age_h : float
            Maximum hypothesis age in hours before forced removal.
        """
        now_dt = _parse_iso(now)
        max_age_s = max_age_h * 3600.0

        surviving: list[WhaleHypothesis] = []

        for hyp in self.hypotheses:
            if not hyp.last_observed:
                continue

            hyp_dt = _parse_iso(hyp.last_observed)
            elapsed_s = max(0.0, (now_dt - hyp_dt).total_seconds())

            # Forced removal if too old
            if elapsed_s > max_age_s:
                continue

            # Grow uncertainty by maximum whale displacement
            hyp.uncertainty_m += self.WHALE_MAX_SPEED_MS * elapsed_s

            # Exponential confidence decay (τ = 6 h)
            tau = 3600.0 * 6.0
            hyp.confidence *= math.exp(-elapsed_s / tau)

            if hyp.confidence >= self.MIN_CONFIDENCE:
                surviving.append(hyp)

        self.hypotheses = surviving

        # Regional field decay — use the time since last aging if available
        if self._last_age_ts:
            last_dt = _parse_iso(self._last_age_ts)
            dt_s = max(0.0, (now_dt - last_dt).total_seconds())
        else:
            dt_s = 0.0

        if dt_s > 0.0:
            # Exponential decay with 12-hour half-life
            tau_field = 3600.0 * 12.0
            decay_factor = math.exp(-dt_s / tau_field)
            self.field.decay(decay_factor)

        self._last_age_ts = now

    # ── Accessors ────────────────────────────────────────────────────────

    def get_hypotheses(self) -> list[WhaleHypothesis]:
        """Return current localized hypotheses (shallow copy of list)."""
        return list(self.hypotheses)

    def get_field(self) -> RegionalRiskField:
        """Return the regional risk field."""
        return self.field

    def reset_field(self) -> None:
        """Clear and rebuild the regional field (called before fresh update cycle)."""
        self.field.grid[:] = 0.0
        self.field._contributed.clear()
