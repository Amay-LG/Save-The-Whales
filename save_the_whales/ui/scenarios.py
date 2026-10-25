"""Scenario assembly only. No capture, decision policy, or world execution."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parents[2]
PRESETS = ('crossing', 'no_threat', 'storm_detour', 'land_blocked_side')


def load_config(path=None):
    return json.loads(Path(path or ROOT / 'config' / 'default.json').read_text())


def load_preset(name):
    if name not in PRESETS:
        raise ValueError('UNKNOWN_SCENARIO')
    return json.loads((ROOT / 'config' / 'scenarios' / f'{name}.json').read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def readiness_issues(bundle, land):
    issues = []
    if not bundle or not bundle.get('routes'):
        issues.append('AIS_ROUTE_UNAVAILABLE')
    else:
        source = bundle.get('source', {})
        if source.get('kind') != 'AISSTREAM_CAPTURE' or source.get('api_verified') is not True or source.get('fixture_only') is not False:
            issues.append('AIS_PROVENANCE_UNVERIFIED')
        if any(not r.get('validation', {}).get('ok') for r in bundle['routes']):
            issues.append('INVALID_BASELINE_ROUTE')
    if not land or not land.get('validated') or not land.get('rings') or any(len(r) < 3 for r in land.get('rings', [])):
        issues.append('INVALID_MAP')
    elif land.get('fixture_only') is not False:
        issues.append('FIXTURE_MAP')
    return issues


def rectangle(lat, lon, width_m, height_m):
    dx = width_m / (2 * 6371000 * math.cos(math.radians(34.2))) * 180 / math.pi
    dy = height_m / (2 * 6371000) * 180 / math.pi
    return [{'lat': lat-y, 'lon': lon+x} for x, y in [(-dx, dy), (dx, dy), (dx, -dy), (-dx, -dy)]]


def build_run_inputs(bundle, land, scenario_settings, config):
    """Freeze JSON inputs. Fixture flags are propagated, never upgraded to real data.

    Explicit whales/weather in scenario_settings override the preset assembly.
    This allows the world owner to supply richer scenarios without UI changes.
    """
    if not bundle or not bundle.get('routes'):
        raise ValueError('AIS_ROUTE_UNAVAILABLE')
    if not land or not land.get('validated') or not land.get('rings'):
        raise ValueError('INVALID_MAP')
    settings = deepcopy(scenario_settings)
    config = deepcopy(config)
    selected = settings.get('route_ids') or [bundle['routes'][0]['route_id']]
    by_id = {r['route_id']: r for r in bundle['routes']}
    if any(r not in by_id for r in selected):
        raise ValueError('UNKNOWN_ROUTE_ID')
    routes = [by_id[r] for r in selected]
    if any(not r.get('validation', {}).get('ok') or len(r.get('waypoints', [])) < 2 for r in routes):
        raise ValueError('INVALID_BASELINE_ROUTE')
    count = int(settings.get('ship_count', 5))
    whale_count = int(settings.get('whale_count', 20))
    if not 1 <= count <= 10 or not 0 <= whale_count <= 30:
        raise ValueError('SCENARIO_SIZE_EXCEEDED')
    name = settings.get('preset', 'crossing')
    if name not in PRESETS:
        raise ValueError('UNKNOWN_SCENARIO')
    preset = load_preset(name)
    sim = config['simulation']
    coverage = sim['duration_s'] + sim['drain_max_s'] + config['planner']['max_rejoin_s']
    first = routes[0]['waypoints']
    a, b = first[0], first[min(2, len(first)-1)]
    center = {'lat': (a['lat']+b['lat'])/2, 'lon': (a['lon']+b['lon'])/2}
    bbox = config['bbox']
    whales = []
    for i in range(whale_count):
        crossing = i == 0 and name != 'no_threat'
        lat = center['lat'] - .002 if crossing else min(bbox['lat_max']-.01, center['lat']+.035+i*.0002)
        lon = center['lon'] if crossing else center['lon']+.012+(i % 5)*.001
        lat = max(bbox['lat_min']+.001, min(bbox['lat_max']-.001, lat))
        lon = max(bbox['lon_min']+.001, min(bbox['lon_max']-.001, lon))
        point = {'lat': lat, 'lon': lon}
        whales.append({'whale_id': f'whale-{i}', 'initial_position': point,
                       'speed_mps': 1.5 if crossing else .3, 'heading_deg': 0,
                       'waypoints': [point, {'lat': min(bbox['lat_max']-.001, center['lat']+.004), 'lon': lon}] if crossing else [],
                       'exposure_schedule': [{'start_s': 0, 'end_s': coverage, 'exposed': True}]})
    weather = {'source': 'SIMULATED', 'coverage_start_s': 0, 'coverage_end_s': coverage, 'zones': []}
    if preset['storm'] or settings.get('storm_enabled'):
        severity = float(settings.get('storm_severity', .8))
        if not math.isfinite(severity) or not 0 <= severity <= 1:
            raise ValueError('INVALID_WEATHER_SEVERITY')
        lat = float(settings.get('storm_lat', center['lat']-.004))
        lon = float(settings.get('storm_lon', center['lon']))
        width = float(settings.get('storm_width_m', 1800))
        height = float(settings.get('storm_height_m', 800))
        if not all(math.isfinite(x) for x in (lat, lon, width, height)) or not (0 < width <= 20000 and 0 < height <= 20000):
            raise ValueError('INVALID_WEATHER_GEOMETRY')
        if not (bbox['lat_min'] <= lat <= bbox['lat_max'] and bbox['lon_min'] <= lon <= bbox['lon_max']):
            raise ValueError('WEATHER_OUT_OF_BOUNDS')
        weather['zones'].append({'zone_id': 'storm-1', 'polygon': rectangle(lat, lon, width, height),
                                'severity': severity, 'active_from_s': 0, 'active_until_s': coverage,
                                'blocked': bool(settings.get('storm_blocked', False))})
    scenario = {'scenario_id': name, 'seed': int(settings.get('seed', 0)),
                'start_at': settings.get('start_at', '2026-07-01T12:00:00Z'),
                'duration_s': sim['duration_s'],
                'voyages': [{'voyage_id': f'voyage-{i}', 'route_id': selected[i % len(selected)],
                             'entry_s': i * int(settings.get('entry_spacing_s', 60))} for i in range(count)],
                'whales': deepcopy(settings.get('whales', whales)),
                'weather': deepcopy(settings.get('weather', weather))}
    # This preset requests a coast-side experiment; it never fabricates real land.
    scenario['notes'] = preset['notes']
    fixture = bool(bundle.get('source', {}).get('fixture_only', True) or land.get('fixture_only', True))
    return {'run_id': uuid.uuid4().hex, 'route_bundle': deepcopy(bundle), 'land': deepcopy(land),
            'scenario': scenario, 'config': config, 'fixture_only': fixture}
