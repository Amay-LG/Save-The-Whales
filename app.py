"""Streamlit entry point and the one production composition of the four owners' modules.

    streamlit run app.py

``production_services()`` binds Person 1's capture/route/land/geometry functions, Person 3's decider,
validator and weather integral, and Person 2's ``run_scenario`` into the plain mapping that
``render_dashboard`` and ``run_comparison.py`` consume. Wiring happens once here; the UI never
reimplements an algorithm, and test preview services live only under ``tests/``.
"""
from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any, Dict

from save_the_whales.advisor import weather as advisor_weather
from save_the_whales.advisor.constraints import make_validator
from save_the_whales.advisor.planner import make_decider
from save_the_whales.shared import geo
from save_the_whales.simulation.engine import run_scenario
from save_the_whales.sources import ais, land, routes
from save_the_whales.ui import scenarios

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config" / "default.json"
DEFAULT_BUNDLE = ROOT / "data" / "ais" / "route_bundle.json"
DEFAULT_LAND = ROOT / "data" / "land" / "santa_barbara.geojson"


def load_config(path: Any = DEFAULT_CONFIG) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def production_services(config: Dict[str, Any] = None) -> Dict[str, Any]:
    """The real module boundaries as ordinary callables (see ``save_the_whales.ui.dashboard``)."""
    import run_comparison                     # local module; imported lazily to avoid an import cycle

    cfg = config if config is not None else load_config()
    services: Dict[str, Any] = {
        "mode_label": "PRODUCTION",
        "preview": False,
        "config": cfg,
        "paths": {"bundle": str(DEFAULT_BUNDLE), "land": str(DEFAULT_LAND), "runs": str(ROOT / "runs")},
        # Person 1: capture, route cache, land, geometry
        "start_capture": ais.start_capture,
        "save_capture": ais.save_capture,
        "load_capture": ais.load_capture,
        "prepare_routes": routes.prepare_routes,
        "save_route_bundle": routes.save_route_bundle,
        "load_route_bundle": routes.load_route_bundle,
        "load_land": land.load_land,
        "geometry": geo,
        # Person 3: advisor factories and the shared weather integral
        "make_decider": make_decider,
        "make_validator": make_validator,
        "weather_exposure": partial(advisor_weather.weather_exposure, geometry=geo),
        "blocked_hits": partial(advisor_weather.blocked_hits, geometry=geo),
        # Person 2: simulator
        "run_scenario": run_scenario,
        # Person 4: scenario assembly and the paired comparison
        "load_presets": scenarios.load_presets,
        "land_override_path": scenarios.land_override_path,
        "build_run_inputs": scenarios.build_run_inputs,
        "readiness": scenarios.readiness,
        "compare_runs": run_comparison.compare_runs,
        "save_run": run_comparison.save_run,
    }
    services["run_pair"] = partial(run_comparison.run_pair, services=services)
    return services


def main() -> None:
    from save_the_whales.ui.dashboard import render_dashboard
    render_dashboard(production_services())


if __name__ == "__main__":
    main()
