"""Explicit preview-only canned services. Never imported by production app.py."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import math

from save_the_whales.ui.scenarios import build_run_inputs, load_config
from save_the_whales.ui.comparison import run_pair


POINTS = [{'lat':34.2,'lon':lon} for lon in (-120.1,-120.075,-120.05,-120.025)]
BBOX = {'lat_min':33.8,'lat_max':34.6,'lon_min':-120.5,'lon_max':-119.0}


def fixtures():
    bundle = {'schema_version':'2.0','bundle_id':'fixture-route-bundle','bbox':BBOX,
              'source':{'kind':'SYNTHETIC_FIXTURE','api_verified':False,'captured_at':'2026-07-01T12:00:00Z','capture_sha256':'fixture-only','fixture_only':True},
              'issues':[], 'routes':[{'route_id':'fixture-passage','source_mmsi':'999000001',
                'source_start_at':'2026-07-01T12:00:00Z','source_end_at':'2026-07-01T12:12:00Z',
                'waypoints':POINTS,'service_speed_knots':18,'length_m':300,'beam_m':40,
                'assumed_fields':['all fields are test fixtures'],'validation':{'ok':True,'reasons':[]}}]}
    land = {'map_id':'fixture-map','source':'SYNTHETIC_FIXTURE','sha256':'fixture-only','validated':True,'fixture_only':True,
            'bbox':BBOX,'issues':[],'rings':[[{'lat':34.204,'lon':-120.075},{'lat':34.216,'lon':-120.075},
                                          {'lat':34.216,'lon':-120.055},{'lat':34.204,'lon':-120.055}]]}
    return deepcopy(bundle), deepcopy(land)


def project(p):
    return (6371000*math.radians(p['lon']+119.75)*math.cos(math.radians(34.2)),
            6371000*math.radians(p['lat']-34.2))


class Capture:
    def __init__(self):self.stopped=False
    def status(self):
        return {'state':'STOPPED' if self.stopped else 'CAPTURING','reason_codes':['FIXTURE_ONLY'],
                'records_received':3,'records_valid':3,'vessels_tracked':1,'last_received_at':'2026-07-01T12:00:00Z','api_verified':False}
    def snapshot_records(self):return []
    def stop(self):self.stopped=True


def canned_decide(obs):
    rejoin=2
    start={'lat':34.2,'lon':-120.1}
    path=[start,{'lat':34.197,'lon':-120.08},{'lat':34.197,'lon':-120.06}]+POINTS[rejoin:]
    return {'decision_id':obs['decision_id'],'sim_time_s':60,
            'actions':[{'ship_id':'sim:999000001:voyage-0','issued_at':'2026-07-01T12:01:00Z','command':'DETOUR',
                        'action_id':'fixture-action','revision':1,'target_speed_knots':10,'valid_until':'2026-07-01T12:10:00Z',
                        'detour_waypoints':path[1:3],'rejoin_route_index':rejoin,
                        'metadata':{'reason':'Canned preview decision: illustrate a southern detour around a reported synthetic whale.'}}],
            'assessments':[{'ship_id':'sim:999000001:voyage-0','status':'DETOUR_SELECTED','reason_codes':['FIXTURE_ONLY','WHALE_WATCH'],
                            'selected_path':path,'predicted_cpa_m':850,'predicted_tcpa_s':240,
                            'costs':{'whale_risk':.15,'weather_exposure_s':0,'weather_cost_normalized':0,'added_delay_s':120,
                                     'extra_distance_m':450,'objective':.9},
                            'rejected_candidates':[{'candidate':'north','reason_codes':['INSUFFICIENT_LAND_CLEARANCE']}]}]}


def canned_runner(inputs, decide, validate, weather, *, project, on_frame=None):
    obs={'decision_id':'fixture-decision','sim_time_s':60}
    decision=decide(obs)
    advisor=bool(decision['actions'])
    ship={'ship_id':'sim:999000001:voyage-0','lat':34.2,'lon':-120.1,'current_route':deepcopy(POINTS[1:]),'original_route':deepcopy(POINTS),
          'sog_knots':18,'cog_deg':90,'length_m':300,'beam_m':40,'service_speed_knots':18,'active_command':None,'baseline_progress_index':0,'latest_revision':0,'last_action_at':None}
    report={'detection_id':'fixture-sighting','lat':34.2,'lon':-120.065,'position_error_m':80,
            'observed_at':'2026-07-01T12:00:00Z','reported_at':'2026-07-01T12:00:30Z','source':'SIMULATED'}
    first={'sim_time_s':0,'now':'2026-07-01T12:00:00Z','ships':[deepcopy(ship)],'whale_detections':[],'alerts':[]}
    ship['lat']=34.197 if advisor else 34.2;ship['lon']=-120.065
    ship['current_route']=[{'lat':34.197,'lon':-120.06}]+deepcopy(POINTS[2:]) if advisor else deepcopy(POINTS[2:])
    last={'sim_time_s':360,'now':'2026-07-01T12:06:00Z','ships':[ship],'whale_detections':[report],'alerts':decision['actions']}
    summary={'status':'COMPLETE','elapsed_wall_s':.01,'simulated_duration_s':900,'voyages_completed':1,'voyages_failed':0,
             'modeled_contacts':0 if advisor else 1,'close_approaches':0 if advisor else 1,
             'whale_exposure_proxy':.18 if advisor else .8,'weather_exposure_s':0,
             'added_delay_s':120 if advisor else 0,'extra_distance_m':450 if advisor else 0,
             'alerts':1 if advisor else 0,'route_changes':1 if advisor else 0,'rejected_actions':0}
    return {'manifest':{'mode':'AIS_SEEDED_SIMULATION','fixture_only':True,'status':'COMPLETE'},'summary':summary,
            'frames':[first,last],'events':[{'sim_time_s':60,'type':'FIXTURE_ONLY','details':'Canned UI output; no simulator executed.'}], 'decisions':[decision]}


def preview_services():
    bundle,land=fixtures()
    return {'preview':True,'integration_issues':[], 'config':load_config(), 'initial_bundle':bundle,'initial_land':land,
            'build_run_inputs':build_run_inputs,'run_pair':run_pair,'run_scenario':canned_runner,
            'make_decider':lambda config,geometry:canned_decide,
            'make_validator':lambda config,geometry:lambda *a,**k: {'ok':False,'reasons':['PREVIEW_NOT_A_VALIDATOR'],'validated_action':None},
            'weather_exposure':lambda *a:0,'geometry':SimpleNamespace(project=project),
            'start_capture':lambda config:Capture(),'prepare_routes':lambda *a:deepcopy(bundle),
            'load_land':lambda *a:deepcopy(land),'load_route_bundle':lambda *a:deepcopy(bundle)}
