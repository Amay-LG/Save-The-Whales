"""Production composition root. Launch: streamlit run app.py."""
import importlib
from save_the_whales.ui.scenarios import build_run_inputs
from save_the_whales.ui.comparison import run_pair, compare_runs


def production_services():
    """Expose missing owner interfaces explicitly; never substitute a fixture."""
    services = {'build_run_inputs': build_run_inputs, 'run_pair': run_pair, 'compare_runs': compare_runs,
                'preview': False, 'integration_issues': []}
    interfaces = {
        'save_the_whales.sources.ais': ['start_capture'],
        'save_the_whales.sources.routes': ['prepare_routes', 'load_route_bundle', 'save_route_bundle'],
        'save_the_whales.sources.land': ['load_land'],
        'save_the_whales.simulation.engine': ['run_scenario'],
        'save_the_whales.advisor.planner': ['make_decider'],
        'save_the_whales.advisor.constraints': ['make_validator'],
        'save_the_whales.advisor.weather': ['weather_exposure'],
    }
    for path, names in interfaces.items():
        try:
            module = importlib.import_module(path)
        except Exception as exc:
            services['integration_issues'].append(f'{path}: {type(exc).__name__}')
            continue
        for name in names:
            value = getattr(module, name, None)
            if callable(value):
                services[name] = value
            else:
                services['integration_issues'].append(f'{path}.{name}')
    try:
        geometry = importlib.import_module('save_the_whales.shared.geo')
        missing = [n for n in ('project', 'unproject', 'ship_clearance_m', 'check_water_path', 'segment_zone_intervals') if not callable(getattr(geometry, n, None))]
        if missing:
            services['integration_issues'].extend('shared.geo.'+n for n in missing)
        else:
            services['geometry'] = geometry
    except Exception as exc:
        services['integration_issues'].append(f'shared.geo: {type(exc).__name__}')
    return services


if __name__ == '__main__':
    from save_the_whales.ui.dashboard import render_dashboard
    render_dashboard(production_services())
