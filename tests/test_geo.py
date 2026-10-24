from simulator import geo


def test_projection_error_small():
    for a, b in [((33.8, -120.5), (34.6, -119.0)), ((34.1, -119.8), (34.11, -119.79))]:
        (x1, y1), (x2, y2) = geo.project(*a), geo.project(*b)
        d_xy = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        assert abs(d_xy / geo.haversine_km(*a, *b) - 1) < 0.005


def test_roundtrip():
    lat, lon = geo.unproject(*geo.project(34.3, -120.1))
    assert abs(lat - 34.3) < 1e-9 and abs(lon + 120.1) < 1e-9


def test_polygon():
    sq = [(0, 0), (1, 0), (1, 1), (0, 1)]
    assert geo.point_in_polygon((0.5, 0.5), sq) and not geo.point_in_polygon((2, 0.5), sq)
    assert geo.segment_hits_polygon((-1, 0.5), (2, 0.5), sq)
    assert not geo.path_hits_land([(-1, 2), (2, 2)], [sq])
