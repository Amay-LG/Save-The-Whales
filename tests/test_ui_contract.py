"""Dashboard-owned tests; no real source, planner, or simulator required."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from fixtures.ui.services import fixtures, preview_services, project
from save_the_whales.ui.scenarios import build_run_inputs, load_config, readiness_issues
from save_the_whales.ui.comparison import compare_runs, run_pair, save_comparison
from save_the_whales.ui.plots import map_data


def inputs(**settings):
    bundle,land=fixtures()
    return build_run_inputs(bundle,land,settings,load_config())


def test_inputs_are_copied_and_fixture_flags_preserved():
    bundle,land=fixtures()
    original=deepcopy(bundle)
    frozen=build_run_inputs(bundle,land,{'ship_count':2,'whale_count':3},load_config())
    bundle['routes'][0]['waypoints'][0]['lat']=0
    assert frozen['route_bundle']==original
    assert frozen['fixture_only'] is True
    assert len(frozen['scenario']['voyages'])==2
    assert len(frozen['scenario']['whales'])==3
    assert frozen['scenario']['weather']['coverage_end_s']>=12600


def test_invalid_routes_and_empty_land_rejected():
    bundle,land=fixtures()
    with pytest.raises(ValueError,match='UNKNOWN_ROUTE_ID'):
        build_run_inputs(bundle,land,{'route_ids':['missing']},load_config())
    land['rings']=[]
    with pytest.raises(ValueError,match='INVALID_MAP'):
        build_run_inputs(bundle,land,{},load_config())


def test_readiness_cannot_promote_fixture_data_to_real():
    bundle,land=fixtures()
    issues=readiness_issues(bundle,land)
    assert 'AIS_PROVENANCE_UNVERIFIED' in issues
    assert 'FIXTURE_MAP' in issues
    bundle['source'].update(kind='AISSTREAM_CAPTURE',api_verified=True,fixture_only=False)
    land['fixture_only']=False
    assert readiness_issues(bundle,land)==[]


@pytest.mark.parametrize('preset',['crossing','no_threat','storm_detour','land_blocked_side'])
def test_presets_have_finite_weather_coverage(preset):
    result=inputs(preset=preset)
    assert result['scenario']['scenario_id']==preset
    weather=result['scenario']['weather']
    assert weather['source']=='SIMULATED'
    if preset=='storm_detour':
        assert len(weather['zones'])==1
    json.dumps(result,allow_nan=False)


def test_weather_validation():
    with pytest.raises(ValueError,match='INVALID_WEATHER_SEVERITY'):
        inputs(storm_enabled=True,storm_severity=float('nan'))
    with pytest.raises(ValueError,match='WEATHER_OUT_OF_BOUNDS'):
        inputs(storm_enabled=True,storm_lat=90)


def test_pair_worlds_share_frozen_inputs_but_not_mutable_objects():
    services=preview_services()
    captured=[]
    original_runner=services['run_scenario']
    def runner(run_inputs,*args,**kwargs):
        captured.append(deepcopy(run_inputs))
        result=original_runner(run_inputs,*args,**kwargs)
        run_inputs['scenario']['whales'].clear()
        run_inputs['route_bundle']['routes'].clear()
        return result
    services['run_scenario']=runner
    source=inputs(seed=42)
    before=deepcopy(source)
    result=run_pair(source,services)
    assert source==before
    assert captured[0]==captured[1]
    assert result['comparison']['status']=='COMPLETE'
    assert captured[0]['config']['simulation']['wall_limit_s']<=14.5
    assert result['results']['ADVISOR']['manifest']['fixture_only']


def test_incomplete_and_null_metrics_never_make_fake_deltas():
    a={'summary':{'status':'COMPLETE','added_delay_s':0,'modeled_contacts':1}}
    b={'summary':{'status':'INCOMPLETE','added_delay_s':None,'modeled_contacts':0}}
    comparison=compare_runs(a,b)
    assert comparison['status']=='INCOMPLETE'
    assert all(row['advisor_minus_baseline'] is None for row in comparison['metrics'])


def test_runner_failure_stays_in_comparison():
    services=preview_services()
    services['run_scenario']=lambda *a,**k: (_ for _ in ()).throw(ValueError('scripted failure'))
    result=run_pair(inputs(),services)
    assert result['comparison']['status']=='INCOMPLETE'
    assert set(result['results'])=={'NO_ACTION','ADVISOR'}
    assert all(r['summary']['status']=='ERROR' for r in result['results'].values())


def test_artifact_directories_are_unique_and_json_roundtrips(tmp_path):
    result=run_pair(inputs(),preview_services())
    a=save_comparison(result,tmp_path);b=save_comparison(result,tmp_path)
    assert a!=b
    assert json.loads((a/'comparison.json').read_text())['inputs_sha256']==result['inputs_sha256']


def test_truth_layer_is_opt_in_and_routes_are_distinct():
    bundle,land=fixtures()
    frame={'sim_time_s':0,'ships':[{'ship_id':'sim:1','lat':34.2,'lon':-120.1,'current_route':[{'lat':34.21,'lon':-120.08}]}],
           'analyst_truth':{'whales':[{'lat':34.2,'lon':-120.09}]}}
    layers=map_data(bundle,land,frame,{'zones':[]})
    assert layers['truth']==[]
    assert layers['baseline'][0]['path']!=layers['current'][0]['path']
    assert map_data(bundle,land,frame,{'zones':[]},show_truth=True)['truth']


def test_production_does_not_import_or_fallback_to_preview():
    from app import production_services
    services=production_services()
    assert services['preview'] is False
    assert 'initial_bundle' not in services


def test_preview_runs_and_rerender_does_not_run_again():
    from streamlit.testing.v1 import AppTest
    at=AppTest.from_file(str(Path(__file__).parent/'ui_preview.py'),default_timeout=20).run()
    assert not at.exception
    assert not any('<section class="save-hero">' in code.value for code in at.code)
    next(b for b in at.button if b.label=='Compare routes').click().run()
    assert not at.exception
    run_id=at.session_state['dashboard_pair']['run_id']
    at.run()
    assert not at.exception
    assert at.session_state['dashboard_pair']['run_id']==run_id
    assert any(m.label=='Advisor route changes' and m.value=='1' for m in at.metric)


def test_production_empty_state_is_explicit():
    from streamlit.testing.v1 import AppTest
    at=AppTest.from_file(str(Path(__file__).parents[1]/'app.py'),default_timeout=20).run()
    assert not at.exception
    # Missing teammate implementations disable Run rather than invoking fixtures.
    from app import production_services
    if production_services()['integration_issues']:
        assert next(b for b in at.button if b.label=='Compare routes').disabled


def test_factory_failure_is_a_failed_policy_not_a_hidden_fallback():
    services=preview_services()
    services['make_decider']=lambda *a: (_ for _ in ()).throw(ValueError('planner unavailable'))
    result=run_pair(inputs(),services)
    assert result['results']['NO_ACTION']['summary']['status']=='COMPLETE'
    assert result['results']['ADVISOR']['summary']['status']=='ERROR'
    assert result['comparison']['status']=='INCOMPLETE'


def test_result_fixture_flag_is_never_upgraded():
    source=inputs()
    source['fixture_only']=False
    result=run_pair(source,preview_services())
    assert result['fixture_only'] is True
    assert result['results']['ADVISOR']['manifest']['fixture_only'] is True
