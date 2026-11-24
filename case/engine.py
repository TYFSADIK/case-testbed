"""The site A simulation: world + traffic + environment + sensors + faults.

`Engine.step()` advances simulated time and returns what happened: sensor
messages (what the sensors *report*), vehicle states, and ground truth (what
is *really* going on). Ground truth goes to a separate log that only the
evaluation tools read; the twin's backend never sees it.
"""
from __future__ import annotations

import math
from dataclasses import asdict

import yaml

from .geo import to_local
from .physics import Environment, Event, Fault, FaultInjector, SensorModel
from .traffic import Traffic
from .world import load


def _xy(spec, world):
    if "lat" in spec:
        return to_local(spec["lat"], spec["lon"])
    if "asset" in spec:
        return next(a.xy for a in world.assets if a.id == spec["asset"])
    if "sensor" in spec:
        return world.sensor(spec["sensor"]).xy
    return None


def load_scenario(path, world):
    sc = yaml.safe_load(open(path))
    events, faults = [], []
    for e in sc.get("events", []):
        events.append(Event(e["id"], e["kind"], float(e["t0"]), float(e["t1"]), _xy(e.get("at", {}), world),
                            dict(e.get("params", {}))))
    for f in sc.get("faults", []):
        faults.append(Fault(f["id"], f["sensor"], f["kind"], float(f["t0"]), float(f["t1"]), dict(f.get("params", {}))))
    return sc, events, faults


class Engine:
    def __init__(self, scenario_path=None, vehicles: int = 100, seed: int = 1, world=None):
        self.world = world or load()
        self.env = Environment(self.world, seed=seed + 2)
        self.traffic = Traffic(self.world.roads, vehicles, seed=seed + 10)
        self.model = SensorModel(self.world, self.env, self.traffic, seed=seed + 4)
        self.faults = FaultInjector(seed=seed + 8)
        self.t = 0.0
        self.duration = 4 * 3600.0
        self.scenario = {}
        if scenario_path:
            self.scenario, events, faults = load_scenario(scenario_path, self.world)
            self.env.events += events
            self.faults.faults += faults
            self.duration = float(self.scenario.get("duration", self.duration))
            self.env.wind_toward_deg = float(self.scenario.get("wind_toward_deg", self.env.wind_toward_deg))
        self.next_sample = {s.id: (i * 0.157) % s.period for i, s in enumerate(self.world.sensors)}
        self.closed_now: frozenset = frozenset()
        self.truth_affected: dict = {}

    # -- instructor injects at run time
    def inject_event(self, kind, t0, t1, xy, params=None, eid=None):
        e = Event(eid or f"inject-{kind}-{int(t0)}", kind, t0, t1, xy, params or {})
        self.env.events.append(e)
        return e

    def inject_fault(self, sensor, kind, t0, t1, params=None, fid=None):
        f = Fault(fid or f"inject-{kind}-{sensor}-{int(t0)}", sensor, kind, t0, t1, params or {})
        self.faults.faults.append(f)
        return f

    def _update_closures(self):
        area = self.env.flooded_area(self.t)
        closed = set()
        if area is not None and not area.is_empty:
            from shapely.geometry import Point
            from shapely.prepared import prep
            pa = prep(area)
            g = self.world.roads
            for u, v in g.edges:
                a, b = g.nodes[u]["xy"], g.nodes[v]["xy"]
                if pa.contains(Point((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)):
                    closed.add((u, v))
        closed = frozenset(closed)
        if closed != self.closed_now:
            self.closed_now = closed
            self.traffic.close(set(closed))
            return True
        return False

    def step(self, dt: float = 1.0):
        """Advance dt seconds. Returns (messages, truth_records)."""
        self.t += dt
        t = self.t
        storm = self.env.storm(t)
        self.traffic.speed_factor = 1.0 - 0.35 * storm
        truth = []
        if int(t) % 30 == 0 and self._update_closures():
            truth.append({"t": t, "type": "roads_closed", "edges": len(self.closed_now)})
        self.traffic.step(dt)
        msgs = []
        for s in self.world.sensors:
            if t < self.next_sample[s.id]:
                continue
            self.next_sample[s.id] += s.period
            v, extra, contrib = self.model.true_value(s, t)
            powered = self.model.powered(s, t)
            reported = self.faults.apply(s.id, t, v) if powered else None
            faults = [f.id for f in self.faults.active(s.id, t)]
            if contrib or faults or not powered:
                truth.append({"t": t, "type": "sample", "sensor": s.id, "true": round(v, 4),
                              "reported": None if reported is None else round(reported, 4),
                              "events": {k: round(c, 4) for k, c in contrib.items()}, "faults": faults,
                              "powered": powered})
            if reported is None:
                continue
            m = {"id": s.id, "type": s.type, "t": round(t, 3), "v": round(reported, 4), "unit": s.unit}
            if extra:
                m.update(extra)
            msgs.append(m)
        return msgs, truth

    def truth_header(self):
        return {"type": "header", "events": [asdict(e) for e in self.env.events],
                "faults": [asdict(f) for f in self.faults.faults], "duration": self.duration,
                "sensors": len(self.world.sensors)}


def sensor_registry(world):
    return [{"id": s.id, "type": s.type, "xy": list(s.xy), "latlon": list(s.latlon), "period": s.period,
             "power": s.power, "substation": s.substation, "creek": bool(s.meta.get("creek")), "unit": s.unit}
            for s in world.sensors]


def vehicle_heading(v):
    return v.heading if v.speed > 0 else 0.0


def kmh(ms):
    return ms * 3.6


__all__ = ["Engine", "sensor_registry", "load_scenario", "math"]
