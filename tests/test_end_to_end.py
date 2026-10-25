"""Integration gates. Skipped while owner modules or real artifacts are missing."""
import os
import pytest
from app import production_services
from save_the_whales.ui.scenarios import build_run_inputs, load_config, readiness_issues
from save_the_whales.ui.comparison import run_pair


def test_real_owner_modules_and_genuine_inputs():
    services=production_services()
    if services['integration_issues']:
        pytest.skip('Owner interfaces pending: '+', '.join(services['integration_issues']))
    bundle_path=os.environ.get('WHALE_ROUTE_BUNDLE')
    land_path=os.environ.get('WHALE_LAND_FILE')
    if not bundle_path or not land_path:
        pytest.skip('Set WHALE_ROUTE_BUNDLE and WHALE_LAND_FILE to genuine source artifacts')
    config=load_config()
    land=services['load_land'](land_path,config)
    bundle=services['load_route_bundle'](bundle_path,land,config)
    assert readiness_issues(bundle,land)==[]
    inputs=build_run_inputs(bundle,land,{'preset':'crossing','ship_count':1,'whale_count':1},config)
    result=run_pair(inputs,services)
    assert result['fixture_only'] is False
    assert result['elapsed_wall_s']<30
    assert result['comparison']['status']=='COMPLETE'
    assert result['results']['ADVISOR']['summary']['route_changes']>=1
