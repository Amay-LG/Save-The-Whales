"""Headless paired comparison using the same production composition as the UI."""
import argparse
import json
from app import production_services
from save_the_whales.ui.scenarios import build_run_inputs, load_config, readiness_issues
from save_the_whales.ui.comparison import run_pair, save_comparison


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--land', required=True)
    parser.add_argument('--scenario', choices=['crossing','no_threat','storm_detour','land_blocked_side'], default='crossing')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--ships', type=int, default=1)
    parser.add_argument('--whales', type=int, default=20)
    parser.add_argument('--output', default='runs')
    args = parser.parse_args(argv)
    services = production_services()
    if services['integration_issues']:
        parser.exit(2, 'INTEGRATION_PENDING: '+', '.join(services['integration_issues'])+'\n')
    config = load_config()
    try:
        land = services['load_land'](args.land, config)
        bundle = services['load_route_bundle'](args.bundle, land, config)
        issues = readiness_issues(bundle, land)
        if issues:
            parser.exit(2, 'INPUT_NOT_READY: '+', '.join(issues)+'\n')
        inputs = build_run_inputs(bundle, land, {'preset':args.scenario,'seed':args.seed,'ship_count':args.ships,'whale_count':args.whales}, config)
        pair = run_pair(inputs, services)
        directory = save_comparison(pair, args.output)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, str(exc)+'\n')
    print(json.dumps({'status':pair['comparison']['status'],'elapsed_wall_s':pair['elapsed_wall_s'],'directory':str(directory)}, indent=2))
    return 0 if pair['comparison']['status']=='COMPLETE' else 1


if __name__=='__main__':
    raise SystemExit(main())
