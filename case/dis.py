"""Distributed Interactive Simulation (IEEE 1278.1-2012, DIS v7) entity state.

Federates publish Entity State PDUs (ESPDUs) over UDP. To save bandwidth a
federate does not send every tick: it runs the same dead-reckoning model the
receivers run and only sends when the receivers' extrapolation would be more
than a threshold off, or when the 5 s heartbeat is due (the standard's
default). Receivers extrapolate between updates.

Encoding uses the Open DIS library; positions are WGS84 ECEF metres.
"""
from __future__ import annotations

import io
import math
import time
from dataclasses import dataclass

from opendis.dis7 import EntityStatePdu
from opendis.PduFactory import createPdu
from opendis.stream import DataOutputStream

from .geo import ecef_to_geodetic, enu_to_ecef_velocity, geodetic_to_ecef, to_latlon, to_local

EXERCISE = 7
DRM_STATIC, DRM_FPW, DRM_RVW = 1, 2, 4
HEARTBEAT = 5.0

# entity kinds (SISO-REF-010): kind 1 platform; domain 1 land, 2 air; country 39 Canada
KINDS = {
    "car": (1, 1, 39, 6, 1),           # land, wheeled, car
    "truck": (1, 1, 39, 7, 1),
    "emergency": (1, 1, 39, 6, 2),
    "uav": (1, 2, 39, 50, 1),          # air, UAV
    "robot": (1, 1, 39, 81, 1),        # land, unmanned ground vehicle
}


# ------------------------------------------------------------- timestamps

def dis_timestamp(t: float) -> int:
    """Absolute DIS timestamp: units of 3600/2^31 s past the hour, low bit 1 = absolute."""
    frac = (t % 3600.0) / 3600.0
    return (int(frac * (2 ** 31)) << 1) | 1


def timestamp_to_time(ts: int, now: float) -> float:
    """Recover full epoch seconds from an absolute timestamp, given a nearby clock reading."""
    past_hour = (ts >> 1) / (2 ** 31) * 3600.0
    hour = math.floor(now / 3600.0) * 3600.0
    t = hour + past_hour
    if t - now > 1800:
        t -= 3600
    elif now - t > 1800:
        t += 3600
    return t


# ------------------------------------------------------------- encode / decode

@dataclass
class EntityState:
    site: int
    app: int
    entity: int
    kind: str
    marking: str
    lat: float
    lon: float
    alt: float
    ve: float               # east, north, up velocity m/s
    vn: float
    vu: float
    heading: float          # radians, local frame (0 = east, counter-clockwise)
    t: float                # sender's clock, epoch seconds
    drm: int = DRM_FPW

    @property
    def key(self):
        return (self.site, self.app, self.entity)


def encode(es: EntityState) -> bytes:
    p = EntityStatePdu()
    p.exerciseID = EXERCISE
    p.timestamp = dis_timestamp(es.t)
    p.entityID.siteID, p.entityID.applicationID, p.entityID.entityID = es.site, es.app, es.entity
    k = KINDS.get(es.kind, KINDS["car"])
    et = p.entityType
    et.entityKind, et.domain, et.country, et.category, et.subcategory = k
    x, y, z = geodetic_to_ecef(es.lat, es.lon, es.alt)
    p.entityLocation.x, p.entityLocation.y, p.entityLocation.z = x, y, z
    vx, vy, vz = enu_to_ecef_velocity(es.ve, es.vn, es.vu, es.lat, es.lon)
    p.entityLinearVelocity.x, p.entityLinearVelocity.y, p.entityLinearVelocity.z = vx, vy, vz
    p.entityOrientation.psi = math.pi / 2 - es.heading           # heading from north, clockwise
    p.deadReckoningParameters.deadReckoningAlgorithm = es.drm
    p.marking.setString(es.marking[:11])
    buf = io.BytesIO()
    p.serialize(DataOutputStream(buf))
    return buf.getvalue()


def decode(data: bytes, now: float | None = None) -> EntityState | None:
    try:
        p = createPdu(data)
    except Exception:
        return None
    if p is None or p.pduType != 1 or p.exerciseID != EXERCISE:
        return None
    loc = p.entityLocation
    lat, lon, alt = ecef_to_geodetic(loc.x, loc.y, loc.z)
    v = p.entityLinearVelocity
    la, lo = math.radians(lat), math.radians(lon)
    sl, cl, so, co = math.sin(la), math.cos(la), math.sin(lo), math.cos(lo)
    ve = -so * v.x + co * v.y
    vn = -sl * co * v.x - sl * so * v.y + cl * v.z
    vu = cl * co * v.x + cl * so * v.y + sl * v.z
    et = p.entityType
    kind = next((k for k, val in KINDS.items() if val == (et.entityKind, et.domain, et.country, et.category, et.subcategory)), "car")
    now = time.time() if now is None else now
    return EntityState(p.entityID.siteID, p.entityID.applicationID, p.entityID.entityID, kind,
                       p.marking.charactersString(), lat, lon, alt, ve, vn, vu,
                       math.pi / 2 - p.entityOrientation.psi, timestamp_to_time(p.timestamp, now),
                       p.deadReckoningParameters.deadReckoningAlgorithm)


# ------------------------------------------------------------- dead reckoning

def extrapolate(es: EntityState, dt: float) -> tuple[float, float]:
    """Local (x, y) after dt seconds under the entity's dead-reckoning model."""
    x, y = to_local(es.lat, es.lon)
    if es.drm == DRM_STATIC or dt <= 0:
        return x, y
    return x + es.ve * dt, y + es.vn * dt


class Sender:
    """Decides when an entity needs a fresh ESPDU (threshold + heartbeat)."""

    def __init__(self, threshold_m: float = 1.0, heartbeat: float = HEARTBEAT):
        self.threshold, self.heartbeat = threshold_m, heartbeat
        self.last: dict = {}
        self.sent = 0

    def due(self, es: EntityState, now: float) -> bool:
        prev = self.last.get(es.key)
        if prev is None or now - prev.t >= self.heartbeat:
            return True
        px, py = extrapolate(prev, now - prev.t)
        x, y = to_local(es.lat, es.lon)
        return math.hypot(x - px, y - py) > self.threshold

    def mark(self, es: EntityState):
        self.last[es.key] = es
        self.sent += 1


def state_from_local(site, app, entity, kind, marking, xy, vel, heading, t, alt=0.0, vu=0.0, drm=DRM_FPW):
    lat, lon = to_latlon(*xy)
    return EntityState(site, app, entity, kind, marking, lat, lon, alt, vel[0], vel[1], vu, heading, t, drm)
