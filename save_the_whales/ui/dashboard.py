"""Streamlit presentation over injected ordinary callables. No routing policy."""
from copy import deepcopy
import json
from pathlib import Path
import streamlit as st

from .scenarios import load_config, readiness_issues, PRESETS
from .comparison import save_comparison
from .plots import map_data, build_deck, assessment_rows

CSS = '''<style>
[data-testid="stAppViewContainer"]{background:radial-gradient(ellipse at 80% 0%,#164c60 0%,#0a2c3e 38%,#061b2a 85%);color:#edf9fc}
[data-testid="stSidebar"]{background:#092b3b;border-right:1px solid #285366}
.block-container{padding-top:2rem;max-width:1600px}
h1{font-weight:550!important;letter-spacing:-1.5px!important;font-size:2.5rem!important}
h2,h3{letter-spacing:-.5px} [data-testid="stMetric"]{background:linear-gradient(140deg,#123f50,#0c2e40);border:1px solid #316075;border-radius:9px;padding:17px}
[data-testid="stMetricLabel"]{color:#a6bec9;font-size:12px}
[data-testid="stMetricValue"]{font-size:1.9rem;color:#8df1c5}
.stButton>button[kind="primary"]{background:#8df1c5;color:#08221b;border:0;font-weight:650}
.stButton>button{border-radius:6px}.eyebrow{color:#8df1c5;letter-spacing:2px;font-size:11px;margin-bottom:12px}
.lead{color:#a8c1cd;font-size:15px;line-height:1.7;max-width:850px}.legend{color:#aac1cd;font-size:12px;line-height:2}
.sectionline{border-top:1px solid #2b586d;margin:20px 0}.notice{background:#152d36;padding:18px;border:1px solid #36515d;border-radius:8px;color:#bcd1dc}
.ocean-wave{height:32px;margin:18px 0 16px;opacity:.65}
@media(max-width:1000px){.st-key-map_workspace [data-testid="stHorizontalBlock"]{flex-direction:column}.st-key-map_workspace [data-testid="stColumn"]{width:100%!important;flex:1 1 100%!important;min-width:0!important}}
</style>'''


def render_dashboard(services):
    st.set_page_config(page_title='Save the Whales', page_icon='🐋', layout='wide')
    st.markdown(CSS, unsafe_allow_html=True)
    state = st.session_state
    config = deepcopy(services.get('config') or load_config())
    preview = services.get('preview', False)
    prefix = 'dashboard_'
    if prefix+'bundle' not in state:
        state[prefix+'bundle'] = deepcopy(services.get('initial_bundle'))
        state[prefix+'land'] = deepcopy(services.get('initial_land'))
    bundle, land = state[prefix+'bundle'], state[prefix+'land']
    st.markdown('<div class="eyebrow">OCEAN LAB / SANTA BARBARA CHANNEL</div>', unsafe_allow_html=True)
    st.title('Save the Whales')
    st.markdown('<div class="lead">A clearer course through a changing ocean. Explore how simulated ships respond to whale sightings and storm conditions along real recorded passages.</div>', unsafe_allow_html=True)
    st.markdown('<div class="ocean-wave"><svg viewBox="0 0 1000 32" width="100%" height="32" preserveAspectRatio="none"><path d="M0 18 Q125 0 250 18 T500 18 T750 18 T1000 18" fill="none" stroke="#74d2d5" stroke-width="1" opacity=".7"/><path d="M0 25 Q125 7 250 25 T500 25 T750 25 T1000 25" fill="none" stroke="#3c8eab" stroke-width="1" opacity=".5"/></svg></div>', unsafe_allow_html=True)
    if preview:
        st.warning('FIXTURE PREVIEW — all routes, land, decisions, and outcomes on this page are synthetic test data. This does not verify real AIS integration or a working planner.')
    missing = services.get('integration_issues', [])
    if missing:
        with st.expander(f'Integration pending · {len(missing)} owner interfaces unavailable', expanded=False):
            st.write('The dashboard is ready for these interfaces. Production does not silently replace them with preview services.')
            st.code('\n'.join(missing), language='text')
    with st.sidebar:
        st.subheader('Experiment setup')
        st.caption('Choose inputs before running. Source capture never moves simulated ships.')
        with st.expander('01 · Routes & land', expanded=not bool(bundle)):
            land_path = st.text_input('Regional land GeoJSON', value='data/land/santa_barbara.geojson')
            bundle_path = st.text_input('AIS route bundle', value='data/ais/routes.json')
            if st.button('Load route cache', disabled=not all(k in services for k in ('load_land','load_route_bundle'))):
                try:
                    next_land = services['load_land'](land_path, config)
                    next_bundle = services['load_route_bundle'](bundle_path, next_land, config)
                    state[prefix+'land'], state[prefix+'bundle'] = next_land, next_bundle
                    st.rerun()
                except Exception as exc:
                    st.error(f'INPUT_LOAD_FAILED · {type(exc).__name__}: {exc}')
            st.caption('AISSTREAM_API_KEY is read by the backend environment. Never enter a key into this page.')
            capture = state.get(prefix+'capture')
            capture_running = capture is not None and capture.status().get('state') not in ('STOPPED', 'ERROR')
            left, right = st.columns(2)
            if left.button('Start capture', disabled=capture_running or 'start_capture' not in services):
                try:
                    state[prefix+'capture'] = services['start_capture'](deepcopy(config))
                    st.rerun()
                except Exception as exc:
                    st.error(f'CAPTURE_START_FAILED · {type(exc).__name__}')
            if right.button('Stop capture', disabled=not capture_running):
                capture.stop()
                st.rerun()
            @st.fragment(run_every=2 if capture else None)
            def capture_status():
                if capture:
                    status = capture.status()
                    st.caption(f"{status.get('state', 'UNKNOWN')} · {status.get('records_valid', 0)} valid records · {status.get('vessels_tracked', 0)} vessels")
                    if status.get('reason_codes'):
                        st.warning(' · '.join(status['reason_codes']))
                else:
                    st.caption('Capture idle. A validated route cache also supports offline runs.')
            capture_status()
            if st.button('Freeze captured routes', disabled=not (capture and land and 'prepare_routes' in services)):
                try:
                    state[prefix+'bundle'] = services['prepare_routes'](capture.snapshot_records(), deepcopy(land), deepcopy(config))
                    st.rerun()
                except Exception as exc:
                    st.error(f'ROUTE_PREPARATION_FAILED · {type(exc).__name__}')
            if bundle:
                st.download_button('Download frozen route bundle', json.dumps(bundle, indent=2), 'route_bundle.json', 'application/json')
        st.markdown('**02 · Scenario**')
        preset = st.selectbox('Controlled case', PRESETS, format_func=lambda x: x.replace('_',' ').title())
        route_ids = [r['route_id'] for r in (bundle or {}).get('routes', [])]
        selected = st.multiselect('Recorded route segments', route_ids, default=route_ids[:1])
        seed = st.number_input('Random seed', min_value=0, max_value=2147483647, value=0, step=1)
        c1,c2 = st.columns(2)
        ships = c1.number_input('Ships', 1, 10, 5)
        whales = c2.number_input('Whales', 0, 30, 20)
        st.caption('All ships, whales, and sightings in the run are simulated.')
        settings = {'preset':preset,'route_ids':selected,'seed':int(seed),'ship_count':int(ships),'whale_count':int(whales)}
        with st.expander('03 · Simulated weather', expanded=preset=='storm_detour'):
            enabled = st.checkbox('Include a storm zone', value=preset=='storm_detour')
            settings['storm_enabled'] = enabled
            settings['storm_severity'] = st.slider('Severity', 0., 1., .8, .05)
            settings['storm_blocked'] = st.checkbox('Treat storm as impassable', value=False)
            if enabled:
                route = next((r for r in (bundle or {}).get('routes',[]) if r['route_id'] in selected), None)
                points = route['waypoints'] if route else [{'lat':34.2,'lon':-119.75}]
                center = points[len(points)//2]
                settings['storm_lat'] = st.number_input('Storm latitude', 33.8, 34.6, float(center['lat']), format='%.5f')
                settings['storm_lon'] = st.number_input('Storm longitude', -120.5, -119., float(center['lon']), format='%.5f')
                settings['storm_width_m'] = st.number_input('Width (m)', 100, 20000, 1800, 100)
                settings['storm_height_m'] = st.number_input('Height (m)', 100, 20000, 800, 100)
        issues = readiness_issues(bundle, land)
        can_run = bool(selected) and not missing and (preview or not issues)
        clicked = st.button('Run paired comparison', type='primary', width="stretch", disabled=not can_run)
        if clicked:
            with st.spinner('Freezing inputs · running independent baseline and advisor worlds…'):
                try:
                    inputs = services['build_run_inputs'](bundle, land, settings, config)
                    pair = services['run_pair'](inputs, services)
                    state[prefix+'inputs'] = inputs
                    state[prefix+'pair'] = pair
                    if not preview:
                        state[prefix+'saved'] = str(save_comparison(pair))
                except Exception as exc:
                    st.error(f'RUN_FAILED · {type(exc).__name__}: {exc}')
        st.caption('One run per click. UI refresh does not rerun policy decisions.')
    pair = state.get(prefix+'pair')
    inputs = state.get(prefix+'inputs')
    if not pair:
        source = (bundle or {}).get('source', {})
        a,b,c,d = st.columns(4)
        a.metric('Recorded segments', len((bundle or {}).get('routes', [])))
        b.metric('AIS provenance', 'Fixture' if preview else ('Verified' if source.get('api_verified') else 'Pending'))
        c.metric('Land geometry', 'Fixture' if preview else ('Ready' if land and land.get('validated') else 'Required'))
        d.metric('Comparison', 'Not run')
        if issues and not preview:
            st.info('Input readiness: '+ ' · '.join(issues)+'. Load validated inputs in the sidebar to begin.')
        frame, decisions, weather = {}, [], {'zones': []}
        try:
            if bundle and land and selected:
                staged = services['build_run_inputs'](bundle, land, settings, config)
                weather = staged['scenario']['weather']
        except (ValueError, KeyError):
            pass
    else:
        if pair.get('fixture_only') and not preview:
            st.warning('FIXTURE RESULT — a dependency returned fixture-only output. This is not an integrated real-data result.')
        if pair['comparison']['status']!='COMPLETE':
            st.warning('INCOMPLETE COMPARISON — failed or censored outcomes remain visible. Paired differences are unavailable.')
        if inputs and (settings != state.get(prefix+'last_settings', settings)):
            st.caption('Displayed results are from the last frozen run. New control settings apply to your next run.')
        if clicked:
            state[prefix+'last_settings'] = deepcopy(settings)
        advisor = pair['results']['ADVISOR']
        baseline = pair['results']['NO_ACTION']
        summary = advisor.get('summary', {})
        a,b,c,d = st.columns(4)
        a.metric('Advisor route changes', summary.get('route_changes', '—'))
        b.metric('Whale exposure proxy', _display(summary.get('whale_exposure_proxy')))
        c.metric('Added delay (min)', _display(summary.get('added_delay_s'), scale=60))
        d.metric('Pair runtime (s)', f"{pair['elapsed_wall_s']:.2f}")
        frames = advisor.get('frames', [])
        if len(frames)>1:
            frame_index = st.slider('Inspect saved simulation frame', 0, len(frames)-1, len(frames)-1)
        else:
            frame_index = 0
        frame = frames[frame_index] if frames else {}
        decisions = [decision for decision in advisor.get('decisions', []) if decision.get('sim_time_s', 0)<=frame.get('sim_time_s', float('inf'))]
        weather = inputs['scenario']['weather']
    if pair and inputs:
        bundle, land = inputs['route_bundle'], inputs['land']
    with st.container(key='map_workspace'):
        left,right = st.columns([3.1,1], gap='large')
        with left:
            st.subheader('Santa Barbara · simulated passage')
            basemap = st.toggle('Optional online basemap', value=False)
            truth = st.checkbox('Analyst-only whale truth overlay', value=False, help='For evaluation only. This overlay never builds advisor inputs.')
            assessments = decisions[-1].get('assessments', []) if decisions else []
            if bundle or land:
                data = map_data(bundle, land, frame, weather, assessments, truth)
                st.pydeck_chart(build_deck(data, config['bbox'], basemap), width="stretch")
            else:
                st.markdown('<div class="notice">The map will show your validated route and regional land data here.<br><br>No route has been loaded. Empty or fixture data cannot establish a real AIS-backed demonstration.</div>', unsafe_allow_html=True)
            st.markdown('<div class="legend">🔵 Recorded AIS baseline &nbsp; 🟢 Simulated current route &nbsp; 🟡 Reported synthetic sighting &nbsp; 🟠 Simulated storm &nbsp; 🟣 Analyst truth (optional)</div>', unsafe_allow_html=True)
        with right:
            st.subheader('Decision journal')
            st.caption('What changed, why it changed, and where the vessel rejoins.')
            actions = [action for decision in decisions for action in decision.get('actions', [])]
            if not actions:
                st.info('No actions to display. Run a controlled scenario to inspect decisions and rejection reasons.')
            for action in actions[-5:][::-1]:
                with st.container(border=True):
                    st.markdown(f"**{action['command']} · {action['ship_id']}**")
                    st.caption(f"Revision {action.get('revision','—')} · forward rejoin {action.get('rejoin_route_index','—')}")
                    st.write(action.get('metadata', {}).get('reason', 'See the full assessment below.'))
                    if action.get('target_speed_knots') is not None:
                        st.caption(f"Target speed {action['target_speed_knots']} kn · lease until {action.get('valid_until','—')}")
            source = (bundle or {}).get('source', {})
            with st.expander('Source and model assumptions'):
                st.write({'source':source.get('kind'), 'captured_at':source.get('captured_at'), 'fixture_only':preview or source.get('fixture_only',True), 'land_source':(land or {}).get('source')})
                st.caption('Excluded: ship–ship coordination, maritime/TSS legality, water depth, and realistic manoeuvring dynamics. No real vessel is controlled.')
    if pair:
        overview, diagnostics, artifacts = st.tabs(['Paired outcomes', 'Costs & rejected candidates', 'Artifacts'])
        with overview:
            st.caption('Advisor minus baseline. These are model outcomes, not deaths avoided or calibrated probabilities.')
            rows = [{"Outcome": r['metric'].replace('_',' ').title(), "No action": r['baseline'], "Advisor": r['advisor'], "Difference": None if r['advisor_minus_baseline'] is None else round(r['advisor_minus_baseline'],4)} for r in pair['comparison']['metrics']]
            st.dataframe(rows, width="stretch", hide_index=True)
            for policy,result in pair['results'].items():
                if result.get('error'):
                    st.error(policy+': '+result['error'])
            st.caption(' · '.join(pair['comparison'].get('notes', [])))
        with diagnostics:
            rows = assessment_rows(decisions)
            if rows:
                st.dataframe(rows, width="stretch", hide_index=True)
            else:
                st.info('No assessments returned for this frame.')
            with st.expander('Full decisions, CPA windows, and rejected factors'):
                st.json(decisions[-10:])
        with artifacts:
            st.download_button('Download comparison JSON', json.dumps(pair, indent=2, allow_nan=False), 'comparison.json', 'application/json')
            st.json({key:pair[key] for key in ('run_id','inputs_sha256','fixture_only','elapsed_wall_s')})
            if state.get(prefix+'saved'):
                st.caption('Saved locally: '+state[prefix+'saved'])
    st.markdown('<div class="sectionline"></div>', unsafe_allow_html=True)
    st.caption('Santa Barbara Ocean Lab · recorded route geometry; simulated ships, whales, sightings, and weather. Source AIS updates apply to future runs only.')


def _display(value, scale=1):
    return '—' if value is None else f'{value/scale:,.2f}'
