"""Coordinates.

Two frames are used:
  * a local east/north frame in metres around the scenario origin, for the
    simulation physics (distances, plume, dead reckoning in the twin), and
  * WGS84 earth-centred earth-fixed (ECEF) metres, which is what DIS
    (IEEE 1278.1) carries on the wire.

The local frame is an equirectangular approximation, accurate to well under a
metre across the 3 x 3 km scenario area. ECEF conversions are exact WGS84.
"""
from __future__ import annotations

import math

ORIGIN = (44.3850, -79.6850)      # lat, lon: downtown Barrie, Ontario
R_EARTH = 6_371_008.8

# WGS84
_A = 6_378_137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)


def to_local(lat: float, lon: float, origin=ORIGIN) -> tuple[float, float]:
    lat0, lon0 = origin
    x = math.radians(lon - lon0) * R_EARTH * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * R_EARTH
    return x, y


def to_latlon(x: float, y: float, origin=ORIGIN) -> tuple[float, float]:
    lat0, lon0 = origin
    lat = lat0 + math.degrees(y / R_EARTH)
    lon = lon0 + math.degrees(x / (R_EARTH * math.cos(math.radians(lat0))))
    return lat, lon


def geodetic_to_ecef(lat: float, lon: float, h: float = 0.0) -> tuple[float, float, float]:
    la, lo = math.radians(lat), math.radians(lon)
    n = _A / math.sqrt(1 - _E2 * math.sin(la) ** 2)
    return ((n + h) * math.cos(la) * math.cos(lo),
            (n + h) * math.cos(la) * math.sin(lo),
            (n * (1 - _E2) + h) * math.sin(la))


def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Bowring's method, iterated; sub-millimetre for near-surface points."""
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - _E2))
    h = 0.0
    for _ in range(6):
        n = _A / math.sqrt(1 - _E2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - _E2 * n / (n + h)))
    return math.degrees(lat), math.degrees(lon), h


def enu_to_ecef_velocity(ve: float, vn: float, vu: float, lat: float, lon: float):
    """Rotate a local east/north/up vector into ECEF axes."""
    la, lo = math.radians(lat), math.radians(lon)
    sl, cl, so, co = math.sin(la), math.cos(la), math.sin(lo), math.cos(lo)
    vx = -so * ve - sl * co * vn + cl * co * vu
    vy = co * ve - sl * so * vn + cl * so * vu
    vz = cl * vn + sl * vu
    return vx, vy, vz


def local_to_ecef(x: float, y: float, h: float = 0.0, origin=ORIGIN):
    lat, lon = to_latlon(x, y, origin)
    return geodetic_to_ecef(lat, lon, h)


def dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
