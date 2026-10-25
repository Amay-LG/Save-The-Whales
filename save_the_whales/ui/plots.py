"""Pure layer/table formatting. No decisions or hidden-world access."""


def coordinates(points):
    return [[p['lon'], p['lat']] for p in points]


def map_data(bundle, land, frame, weather, assessments=(), show_truth=False):
    layers = {'baseline': [], 'current': [], 'land': [], 'weather': [], 'ships': [], 'sightings': [], 'selected': [], 'truth': []}
    for route in (bundle or {}).get('routes', []):
        layers['baseline'].append({'path': coordinates(route['waypoints']), 'label': f"Recorded AIS · {route['route_id']}"})
    for ring in (land or {}).get('rings', []):
        layers['land'].append({'polygon': coordinates(ring), 'label': 'Research land geometry'})
    sim_time = (frame or {}).get('sim_time_s', 0)
    for zone in (weather or {}).get('zones', []):
        active = zone['active_from_s'] <= sim_time < zone['active_until_s']
        layers['weather'].append({'polygon': coordinates(zone['polygon']), 'label': f"Simulated storm · severity {zone['severity']}",
                                  'color': [251, 149, 87, 110 if active else 25]})
    for ship in (frame or {}).get('ships', []):
        p = {'lat': ship['lat'], 'lon': ship['lon']}
        layers['ships'].append({'position': coordinates([p])[0], 'label': f"SIMULATED · {ship['ship_id']}"})
        path = [p]+ship.get('current_route', [])
        if len(path) > 1:
            layers['current'].append({'path': coordinates(path), 'label': f"Simulated current route · {ship['ship_id']}"})
    for report in (frame or {}).get('whale_detections', []):
        layers['sightings'].append({'position': [report['lon'], report['lat']],
                                   'radius': max(30, report.get('position_error_m', 50)),
                                   'label': f"Reported synthetic sighting · {report['detection_id']} · observed {report['observed_at']}"})
    for assessment in assessments:
        if assessment.get('selected_path'):
            layers['selected'].append({'path': coordinates(assessment['selected_path']), 'label': f"Assessed plan · {assessment['ship_id']}"})
    if show_truth:
        for whale in (frame or {}).get('analyst_truth', {}).get('whales', []):
            layers['truth'].append({'position': [whale['lon'], whale['lat']], 'label': 'ANALYST TRUTH · hidden from advisor'})
    return layers


def build_deck(data, bbox, basemap=False):
    import pydeck as pdk
    layers = [
        pdk.Layer('PolygonLayer', data['land'], get_polygon='polygon', get_fill_color=[59, 84, 87, 230], get_line_color=[108, 145, 140], stroked=True, line_width_min_pixels=1, pickable=True),
        pdk.Layer('PolygonLayer', data['weather'], get_polygon='polygon', get_fill_color='color', get_line_color=[251,149,87], stroked=True, line_width_min_pixels=1, pickable=True),
        pdk.Layer('PathLayer', data['baseline'], get_path='path', get_color=[121, 165, 211, 150], width_min_pixels=2, pickable=True),
        pdk.Layer('PathLayer', data['current'], get_path='path', get_color=[95, 236, 185], width_min_pixels=3, pickable=True),
        pdk.Layer('PathLayer', data['selected'], get_path='path', get_color=[235, 208, 127], width_min_pixels=2, pickable=True),
        pdk.Layer('ScatterplotLayer', data['sightings'], get_position='position', get_radius='radius', get_fill_color=[246, 190, 112, 170], radius_min_pixels=5, pickable=True),
        pdk.Layer('ScatterplotLayer', data['ships'], get_position='position', get_radius=45, get_fill_color=[125, 249, 204], radius_min_pixels=6, pickable=True),
        pdk.Layer('ScatterplotLayer', data['truth'], get_position='position', get_radius=30, get_fill_color=[221, 138, 238], radius_min_pixels=4, pickable=True),
    ]
    points = [p for row in data['baseline'] for p in row['path']]
    lat = sum(p[1] for p in points)/len(points) if points else (bbox['lat_min']+bbox['lat_max'])/2
    lon = sum(p[0] for p in points)/len(points) if points else (bbox['lon_min']+bbox['lon_max'])/2
    if not basemap:
        water = [[bbox['lon_min'],bbox['lat_min']],[bbox['lon_max'],bbox['lat_min']],[bbox['lon_max'],bbox['lat_max']],[bbox['lon_min'],bbox['lat_max']]]
        layers.insert(0,pdk.Layer('PolygonLayer',[{'polygon':water}],get_polygon='polygon',get_fill_color=[10,48,65,255],pickable=False))
    return pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(
        latitude=lat, longitude=lon,
        zoom=11 if points else 8.2, pitch=0), map_style='https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json' if basemap else None,
        map_provider='carto', tooltip={'text': '{label}'})


def assessment_rows(decisions):
    rows = []
    for decision in decisions:
        for assessment in decision.get('assessments', []):
            costs = assessment.get('costs', {})
            rows.append({'Ship': assessment.get('ship_id'), 'Status': assessment.get('status'),
                         'Reasons': ', '.join(assessment.get('reason_codes', [])),
                         'Whale risk proxy': costs.get('whale_risk'), 'Weather (severity·s)': costs.get('weather_exposure_s'),
                         'Added delay (s)': costs.get('added_delay_s'), 'Extra distance (m)': costs.get('extra_distance_m'),
                         'Objective': costs.get('objective')})
    return rows
