import math

import numpy as np
from opendis.DeadReckoning import drm_fpw

from case import dis
from case.geo import ecef_to_geodetic, enu_to_ecef_velocity, geodetic_to_ecef, to_latlon, to_local


def test_ecef_round_trip():
    for lat, lon, h in [(44.385, -79.685, 0.0), (44.39, -79.70, 60.0), (-33.9, 151.2, 120.0)]:
        x, y, z = geodetic_to_ecef(lat, lon, h)
        la, lo, hh = ecef_to_geodetic(x, y, z)
        assert abs(la - lat) < 1e-8 and abs(lo - lon) < 1e-8 and abs(hh - h) < 1e-3


def test_local_round_trip_and_scale():
    lat, lon = to_latlon(1000.0, -500.0)
    x, y = to_local(lat, lon)
    assert abs(x - 1000) < 1e-6 and abs(y + 500) < 1e-6


def test_velocity_rotation_keeps_speed():
    v = enu_to_ecef_velocity(3.0, 4.0, 0.0, 44.385, -79.685)
    assert abs(math.hypot(*v) - 5.0) < 1e-9


def test_espdu_round_trip():
    es = dis.state_from_local(2, 1, 10, "emergency", "FIRE-1", (120.0, -40.0), (10.0, 5.0), 0.46, 1795690800.25, alt=0.0)
    raw = dis.encode(es)
    assert len(raw) == 144                       # a DIS v7 ESPDU with no articulation parameters
    back = dis.decode(raw, now=1795690801.0)
    assert back.key == (2, 1, 10) and back.kind == "emergency" and back.marking == "FIRE-1"
    x, y = to_local(back.lat, back.lon)
    assert abs(x - 120) < 0.01 and abs(y + 40) < 0.01
    assert abs(back.ve - 10) < 1e-3 and abs(back.vn - 5) < 1e-3
    assert abs(back.t - es.t) < 0.01             # absolute timestamp resolution is ~3.4 microseconds


def test_timestamp_recovers_across_the_hour():
    t = 1795694399.9                              # 0.1 s before an hour boundary
    assert abs(dis.timestamp_to_time(dis.dis_timestamp(t), t + 0.3) - t) < 1e-3


def test_extrapolation_matches_open_dis_fpw():
    es = dis.state_from_local(1, 1, 1, "car", "CAR001", (0.0, 0.0), (12.0, -3.0), 0.0, 0.0)
    x, y = dis.extrapolate(es, 2.5)
    assert abs(x - 30.0) < 1e-6 and abs(y + 7.5) < 1e-6
    pos, _ = drm_fpw([1.0, 2.0, 3.0], [4.0, 5.0, 6.0], 2.0, [0.0, 0.0, 0.0])
    assert np.allclose(pos, [9.0, 12.0, 15.0])    # the library's FPW is the same linear model


def test_sender_threshold_and_heartbeat():
    s = dis.Sender(threshold_m=1.0, heartbeat=5.0)
    es = dis.state_from_local(1, 1, 1, "car", "C", (0.0, 0.0), (10.0, 0.0), 0.0, 100.0)
    assert s.due(es, 100.0)
    s.mark(es)
    on_track = dis.state_from_local(1, 1, 1, "car", "C", (20.0, 0.0), (10.0, 0.0), 0.0, 102.0)
    assert not s.due(on_track, 102.0)            # exactly where dead reckoning puts it
    turned = dis.state_from_local(1, 1, 1, "car", "C", (19.0, 3.0), (0.0, 10.0), 1.57, 102.0)
    assert s.due(turned, 102.0)                  # 3.2 m off the extrapolation
    assert s.due(on_track, 105.1)                # heartbeat
