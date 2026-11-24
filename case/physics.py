"""Environmental events, sensor physics and sensor faults.

Events change the true state of the world (water rises, a plume spreads,
power fails, a storm slows traffic). Sensors measure that state with noise.
Faults change what a sensor *reports* without the world changing. The
detector's job is to tell those apart, so the ground truth records both.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from shapely.geometry import Point

from .geo import dist

DAY = 86400.0
START_HOUR = 6.0            # the exercise starts at 06:00 local


def ramp(t, t0, t1, rise=900.0, fall=900.0) -> float:
    """0 -> 1 over `rise` seconds from t0, hold, 1 -> 0 over `fall` ending at t1."""
    if t < t0 or t > t1:
        return 0.0
    up = min(1.0, (t - t0) / rise) if rise > 0 else 1.0
    down = min(1.0, (t1 - t) / fall) if fall > 0 else 1.0
    return max(0.0, min(up, down))


def diurnal(t: float) -> float:
    """-1 .. 1, peaking mid-afternoon."""
    hour = (START_HOUR + t / 3600.0) % 24
    return math.sin(2 * math.pi * (hour - 9.0) / 24.0)


def load_shape(t: float) -> float:
    """0 .. 1 electrical demand shape: morning and evening peaks."""
    hour = (START_HOUR + t / 3600.0) % 24
    return 0.5 + 0.25 * math.exp(-((hour - 8.5) ** 2) / 4) + 0.35 * math.exp(-((hour - 18.5) ** 2) / 6)


# ------------------------------------------------------------------ events

@dataclass
class Event:
    id: str
    kind: str                   # storm | flood | plume | outage
    t0: float
    t1: float
    xy: tuple | None = None
    params: dict = field(default_factory=dict)

    def level(self, t) -> float:
        return ramp(t, self.t0, self.t1, self.params.get("rise", 900), self.params.get("fall", 900))


@dataclass
class Fault:
    id: str
    sensor: str
    kind: str                   # stuck | drift | spike | offset | noise | dropout | replay
    t0: float
    t1: float
    params: dict = field(default_factory=dict)


class Environment:
    """True state of the environment at time t."""

    def __init__(self, world, seed: int = 3):
        self.world = world
        self.events: list[Event] = []
        self.rng = random.Random(seed)
        self.wind_toward_deg = 15.0                 # compass direction the wind blows toward

    def active(self, kind, t):
        return [e for e in self.events if e.kind == kind and e.t0 <= t <= e.t1]

    def storm(self, t) -> float:
        return max((e.level(t) for e in self.active("storm", t)), default=0.0)

    def wind(self, t) -> tuple[float, float]:
        """(speed m/s, direction the wind blows TOWARD, radians in the local frame)."""
        s = self.storm(t)
        speed = 4.0 + 1.5 * diurnal(t) + 9.0 * s
        toward_compass = math.radians(self.wind_toward_deg) + 0.15 * math.sin(t / 2700.0)
        return speed, math.pi / 2 - toward_compass

    def flood_rise(self, xy, t) -> float:
        r = 0.0
        for e in self.active("flood", t):
            d = dist(xy, e.xy)
            radius = e.params.get("radius", 650.0)
            r += e.params.get("peak", 0.75) * e.level(t) * math.exp(-(d / radius) ** 2)
        return r

    def flooded_area(self, t):
        """Land the flood covers right now (used to close roads)."""
        polys = []
        for e in self.active("flood", t):
            lv = e.level(t)
            if lv <= 0.2:
                continue
            reach = e.params.get("radius", 650.0) * 0.9
            inland = 140.0 * lv
            zone = Point(e.xy).buffer(reach)
            shore = self.world.water.boundary.intersection(zone)
            polys.append(shore.buffer(inland).difference(self.world.water))
        if not polys:
            return None
        from shapely.ops import unary_union
        return unary_union(polys)

    def plume(self, xy, t) -> float:
        """Ground-level concentration (ug/m3) from active plumes: Gaussian plume model."""
        c = 0.0
        for e in self.active("plume", t):
            u, ang = self.wind(t)
            dx, dy = xy[0] - e.xy[0], xy[1] - e.xy[1]
            down = dx * math.cos(ang) + dy * math.sin(ang)
            cross = -dx * math.sin(ang) + dy * math.cos(ang)
            if down <= 10:
                continue
            sy = 0.22 * down / math.sqrt(1 + 0.0004 * down)       # Briggs urban, class D
            sz = 0.14 * down / math.sqrt(1 + 0.0003 * down)
            q = e.params.get("q", 9.0e6) * e.level(t)              # ug/s
            h = e.params.get("stack", 12.0)
            c += q / (math.pi * u * sy * sz) * math.exp(-cross ** 2 / (2 * sy ** 2)) * math.exp(-h ** 2 / (2 * sz ** 2))
        return c

    def outage(self, substation_id, t) -> bool:
        return any(e.params.get("substation") == substation_id for e in self.active("outage", t))


# ------------------------------------------------------------------ sensors

class SensorModel:
    """True value of each sensor, plus measurement noise."""

    def __init__(self, world, env: Environment, traffic, seed: int = 5):
        self.world, self.env, self.traffic = world, env, traffic
        self.rng = random.Random(seed)
        from .world import building_loads
        self.peak = building_loads(world)
        self.bridge_edges = {}
        for s in world.sensors:
            if s.type == "bridge_strain":
                way = s.meta["way"]
                self.bridge_edges[s.id] = {(u, v) for u, v, d in world.roads.edges(data=True) if d["way"] == way}
        self.offset = {s.id: self.rng.gauss(0, 1) for s in world.sensors}   # per-sensor calibration

    def true_value(self, s, t) -> tuple[float, dict, dict]:
        """(value, extra fields, event contributions by event kind)."""
        env, rng = self.env, self.rng
        contrib: dict = {}
        extra: dict = {}
        if s.type == "water_level":
            if s.meta.get("creek"):
                base = 0.35 + 0.01 * self.offset[s.id]
                st = 0.22 * env.storm(t)
                if st > 0.005:
                    contrib["storm"] = st
                v = base + st + rng.gauss(0, 0.004)
            else:
                base = 219.05 + 0.025 * math.sin(2 * math.pi * t / 11500.0)     # lake seiche
                st = 0.06 * env.storm(t)
                fl = env.flood_rise(s.xy, t)
                if st > 0.005:
                    contrib["storm"] = st
                if fl > 0.01:
                    contrib["flood"] = fl
                v = base + st + fl + rng.gauss(0, 0.004)
        elif s.type == "air_quality":
            near = sum(1 for v in self.traffic.vehicles if abs(v.xy[0] - s.xy[0]) < 150 and abs(v.xy[1] - s.xy[1]) < 150)
            washout = 1.0 - 0.45 * env.storm(t)
            base = (7.0 + 1.5 * diurnal(t) + 0.6 * self.offset[s.id] + 0.35 * near) * washout
            pl = env.plume(s.xy, t)
            if pl > 2.0:
                contrib["plume"] = pl
            if washout < 0.97:
                contrib["storm"] = base * (1 / washout - 1)
            v = max(0.0, base + pl + rng.gauss(0, 0.8) * (1 + 0.02 * pl))
        elif s.type == "traffic":
            v = float(self.traffic.take_count(s.edge))
            if self.env.storm(t) > 0.1:
                contrib["storm"] = self.env.storm(t)
        elif s.type == "bridge_strain":
            occ = self.traffic.on_edges(self.bridge_edges[s.id])
            temp = 2.0 + 3.0 * diurnal(t) - 3.0 * env.storm(t)
            v = 80.0 + 4 * self.offset[s.id] + 15.0 * occ + 0.8 * (temp - 5) + rng.gauss(0, 2.0)
        elif s.type == "weather":
            st = env.storm(t)
            temp = 2.0 + 3.0 * diurnal(t) - 3.5 * st + 0.3 * self.offset[s.id] + rng.gauss(0, 0.15)
            speed, ang = env.wind(t)
            extra = {"rain_mmh": round(max(0.0, 22.0 * st + rng.gauss(0, 0.3) * (st > 0.05)), 2),
                     "wind_ms": round(max(0.0, speed + rng.gauss(0, 0.6)), 2),
                     "wind_to_deg": round((90 - math.degrees(ang)) % 360, 1)}
            if st > 0.05:
                contrib["storm"] = st
            v = temp
        elif s.type == "power_load":
            sub = s.meta["asset"]
            if env.outage(sub, t):
                contrib["outage"] = 1.0
                v = 0.3 + rng.gauss(0, 0.05)
            else:
                v = self.peak[sub] * (0.55 + 0.45 * load_shape(t)) + rng.gauss(0, 0.2)
        else:
            raise ValueError(s.type)
        return v, extra, contrib

    def powered(self, s, t) -> bool:
        return s.power != "mains" or not self.env.outage(s.substation, t)


class FaultInjector:
    def __init__(self, seed: int = 9):
        self.faults: list[Fault] = []
        self.rng = random.Random(seed)
        self.last: dict = {}
        self.history: dict = {}

    def active(self, sid, t):
        return [f for f in self.faults if f.sensor == sid and f.t0 <= t <= f.t1]

    def apply(self, sid, t, v) -> float | None:
        """Return the reported value, or None if the sensor reports nothing."""
        hist = self.history.setdefault(sid, [])
        hist.append((t, v))
        if len(hist) > 2000:
            del hist[:500]
        out = v
        for f in self.active(sid, t):
            p = f.params
            if f.kind == "dropout":
                return None
            if f.kind == "stuck":
                out = p.setdefault("value", self.last.get(sid, v))
            elif f.kind == "drift":
                out = out + p["rate_per_h"] * (t - f.t0) / 3600.0
            elif f.kind == "offset":
                out = out + p["delta"]
            elif f.kind == "spike":
                if self.rng.random() < p.get("prob", 0.35):
                    out = out + p["size"] * self.rng.choice([-1, 1]) * self.rng.uniform(0.6, 1.0)
            elif f.kind == "noise":
                out = out + self.rng.gauss(0, p["sigma"])
            elif f.kind == "replay":
                lag = p.get("lag", 3600.0)
                past = [x for (tt, x) in hist if tt <= t - lag]
                out = past[-1] if past else out
        if not self.active(sid, t) or all(f.kind != "stuck" for f in self.active(sid, t)):
            self.last[sid] = v
        return out
