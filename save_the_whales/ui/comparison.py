"""Paired orchestration and artifacts; algorithms are injected, not reimplemented."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
import uuid

from .scenarios import digest

METRICS = ('modeled_contacts', 'close_approaches', 'whale_exposure_proxy', 'weather_exposure_s',
           'added_delay_s', 'extra_distance_m', 'alerts', 'route_changes', 'rejected_actions')


def no_action(observation):
    return {'decision_id': observation['decision_id'], 'actions': [], 'assessments': []}


def compare_runs(baseline_result, advisor_result):
    left, right = baseline_result.get('summary', {}), advisor_result.get('summary', {})
    complete = left.get('status') == right.get('status') == 'COMPLETE'
    rows = []
    for metric in METRICS:
        a, b = left.get(metric), right.get(metric)
        numeric = all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (a, b))
        rows.append({'metric': metric, 'baseline': a, 'advisor': b,
                     'advisor_minus_baseline': b-a if complete and numeric else None})
    return {'status': 'COMPLETE' if complete else 'INCOMPLETE', 'metrics': rows,
            'notes': [] if complete else ['Incomplete outcomes retained; paired differences are unavailable.']}


def run_pair(inputs, services):
    """Two independent worlds with identical frozen data and randomness settings."""
    frozen = deepcopy(inputs)
    start = time.monotonic()
    config = frozen['config']
    # Both policies receive the same runtime budget, leaving a bounded 30 s pair.
    policy_budget = min(14.5, float(config['simulation']['wall_limit_s']))
    frozen['config']['simulation']['wall_limit_s'] = policy_budget
    results = {}
    for policy in ('NO_ACTION', 'ADVISOR'):
        run_inputs = deepcopy(frozen)
        try:
            geometry = services['geometry']
            decide = no_action if policy == 'NO_ACTION' else services['make_decider'](deepcopy(run_inputs['config']), geometry)
            validate = services['make_validator'](deepcopy(run_inputs['config']), geometry)
            result = services['run_scenario'](run_inputs, decide, validate, services['weather_exposure'], project=geometry.project)
            if not isinstance(result, dict) or not isinstance(result.get('summary'), dict):
                raise ValueError('INVALID_RUN_RESULT')
        except Exception as exc:
            # Keep a failed policy in the comparison. Never claim a fallback succeeded.
            result = {'manifest': {}, 'summary': {'status': 'ERROR'}, 'frames': [], 'events': [], 'decisions': [],
                      'error': f'{type(exc).__name__}: {exc}'}
        result = deepcopy(result)
        result.setdefault('manifest', {})
        result_fixture = bool(frozen['fixture_only'] or result['manifest'].get('fixture_only', False))
        result['manifest'].update(policy=policy, source_sha256=digest(frozen['route_bundle']),
                                                map_sha256=digest(frozen['land']), config_sha256=digest(frozen['config']),
                                                seed=frozen['scenario']['seed'], fixture_only=result_fixture)
        results[policy] = result
    elapsed = time.monotonic()-start
    comparison = compare_runs(results['NO_ACTION'], results['ADVISOR'])
    if elapsed > 30:
        comparison['status'] = 'INCOMPLETE'
        comparison['notes'].append('Comparison exceeded 30 seconds; runner wall-budget enforcement requires attention.')
        for row in comparison['metrics']:
            row['advisor_minus_baseline'] = None
    return {'run_id': inputs['run_id'], 'elapsed_wall_s': elapsed, 'fixture_only': any(r['manifest']['fixture_only'] for r in results.values()),
            'inputs_sha256': digest(frozen), 'results': results, 'comparison': comparison}


def save_comparison(pair, root='runs'):
    directory = Path(root) / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    text = json.dumps(pair, indent=2, allow_nan=False)
    (directory/'comparison.json').write_text(text)
    (directory/'manifest.json').write_text(json.dumps({k: pair[k] for k in ('run_id', 'inputs_sha256', 'fixture_only', 'elapsed_wall_s')}, indent=2))
    return directory
