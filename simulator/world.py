"""Static world: land, lanes, VSR zone, whale density (§5.1)."""
import json
from dataclasses import dataclass

from simulator import geo
from simulator.config import ROOT


@dataclass
class World:
    land: list          # projected polygons
    lanes: dict
    vsr_zone: list      # projected polygons
    density: dict
    raw_static: dict    # unprojected GeoJSON, sent to the advisor on its first call

    @classmethod
    def load(cls, data_dir=ROOT / "data"):
        rd = lambda n: json.loads((data_dir / n).read_text())
        land, vsr = rd("land.geojson"), rd("vsr_zone.geojson")
        return cls(geo.load_land(land), rd("lanes.json"), geo.load_land(vsr),
                   rd("whale_density.json"), {"land": land, "vsr_zone": vsr})
