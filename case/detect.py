"""Anomaly detection for the digital twin: sensor fault or real event?

A naive detector flags every sensor whose reading is unusual. During a flood
that is a dozen alerts that all mean the same thing, and a broken sensor looks
exactly like a real event. This detector instead asks, for every sensor, every
simulated minute:

  * z_local   how far is it from its nearest same-type neighbours?
  * z_global  how far is it from all sensors of its type (removes area-wide
              effects such as lake seiche, weather and time of day)?
  * z_trend   how far has it moved from its own last hour?
  * flat / spike / noise   is the signal behaving like a sensor at all?

An IsolationForest per sensor type, trained on the warm-up period, scores
those features. Anomalous sensors are then explained:

  * several nearby sensors deviating the same way, and agreeing with each other
    -> one environmental EVENT (flood, plume, storm, power outage), with the
       sensors as evidence and a footprint;
  * a sensor that disagrees with its neighbours, flat-lines, spikes or goes
    silent without an outage to explain it -> a SENSOR FAULT.

Everything is driven by the simulated time inside the messages, never the
wall clock, so replaying a recording gives exactly the same alerts.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np
from sklearn.ensemble import IsolationForest

from .geo import dist

TICK = 60.0
FEATURES = ["z_local", "z_global", "z_trend", "flat", "spike", "noise"]
SCALE_FLOOR = {"water_level": 0.006, "air_quality": 1.0, "traffic": 1.5, "bridge_strain": 3.0,
               "weather": 0.25, "power_load": 0.3}
FLAT_TYPES = {"water_level", "air_quality", "bridge_strain", "weather", "power_load"}
EVENT_OF = {"water_level": "flood", "creek": "storm", "air_quality": "plume", "weather": "storm",
            "power_load": "outage", "traffic": "traffic_disruption"}
Z = 6.0                     # strong deviation, in robust standard deviations
AREA_WIDE = {"weather", "creek"}   # events that hit these sensors evenly: members must agree


def _mad(a):
    a = np.asarray(a, float)
    if len(a) == 0:
        return 0.0
    return float(1.4826 * np.median(np.abs(a - np.median(a))))


@dataclass
class Alert:
    id: str
    kind: str                  # fault | event
    cls: str                   # fault: stuck|drift|spike|noise|offline|disagrees ; event: flood|plume|storm|outage|traffic_disruption
    t_open: float
    sensors: list
    reason: str
    xy: tuple
    t_last: float = 0.0
    closed: float | None = None
    radius: float = 0.0
    detail: dict = field(default_factory=dict)

    def public(self):
        return {"id": self.id, "kind": self.kind, "cls": self.cls, "t_open": self.t_open, "t_last": self.t_last,
                "closed": self.closed, "sensors": sorted(self.sensors), "reason": self.reason,
                "xy": [round(self.xy[0], 1), round(self.xy[1], 1)], "radius": round(self.radius, 1),
                "detail": self.detail}


class SensorState:
    __slots__ = ("id", "type", "xy", "period", "power", "substation", "creek", "buf", "last_t",
                 "warm_md", "warm_rl", "warm_rg", "warm_max", "warm_sd", "rl0", "rg0", "s_rl", "s_rg",
                 "s_md", "s_max", "s_sd")

    def __init__(self, sid, stype, xy, period, power, substation, creek):
        self.id, self.type, self.xy, self.period = sid, stype, xy, period
        self.power, self.substation, self.creek = power, substation, creek
        self.buf = deque(maxlen=400)            # (t, v), 100 min at 15 s
        self.last_t = None
        self.warm_md, self.warm_rl, self.warm_rg, self.warm_max, self.warm_sd = [], [], [], [], []

    @property
    def group(self):
        return "creek" if self.creek else self.type


class Detector:
    def __init__(self, sensors: list[dict], warmup: float = 1800.0, k: int = 4, naive: bool = False):
        """sensors: [{id, type, xy, period, power, substation, creek}]"""
        self.s = {d["id"]: SensorState(d["id"], d["type"], tuple(d["xy"]), d.get("period", 15.0), d.get("power", "mains"),
                                       d.get("substation"), d.get("creek", False)) for d in sensors}
        self.warmup, self.k, self.naive = warmup, k, naive
        self.now = 0.0
        self.next_tick = TICK
        self.models: dict = {}
        self.cut: dict = {}
        self.train: dict = {}
        self.alerts: dict = {}
        self.history: list = []          # every alert open/close, in order
        self.counter = 0
        self.persist: dict = {}
        self.watch: dict = {}               # sensor -> ticks spent unexplained
        groups: dict = {}
        for st in self.s.values():
            groups.setdefault(st.group, []).append(st)
        self.groups = groups
        self.nb = {}
        for g in groups.values():
            for st in g:
                others = sorted((o for o in g if o.id != st.id), key=lambda o: dist(o.xy, st.xy))
                self.nb[st.id] = [o.id for o in others[:k]]

    # ------------------------------------------------------------ ingest
    def ingest(self, sid: str, t: float, v: float) -> list[dict]:
        st = self.s.get(sid)
        if st is None or v is None or (isinstance(v, float) and math.isnan(v)):
            return []
        st.buf.append((t, float(v)))
        st.last_t = t
        out = []
        while t >= self.next_tick:
            out += self._tick(self.next_tick)
            self.next_tick += TICK
        self.now = max(self.now, t)
        return out

    def advance(self, t: float) -> list[dict]:
        """Move the clock without a reading (so silence is noticed)."""
        out = []
        while t >= self.next_tick:
            out += self._tick(self.next_tick)
            self.next_tick += TICK
        self.now = max(self.now, t)
        return out

    # ------------------------------------------------------------ features
    def _recent(self, st, T, span):
        return [v for (t, v) in st.buf if T - span < t <= T]

    def _val(self, st, T):
        if st.type == "bridge_strain":
            # the empty-bridge reading: passing vehicles only ever add strain
            r = self._recent(st, T, 8 * st.period + 1)
            return float(np.min(r)) if r else None
        r = self._recent(st, T, 4 * st.period + 1)       # one-minute mean
        return float(np.mean(r)) if r else None

    def _stale(self, st, T):
        return st.last_t is None or T - st.last_t > 3.5 * st.period

    def _features(self, T):
        vals = {sid: self._val(st, T) for sid, st in self.s.items() if not self._stale(st, T)}
        feats = {}
        open_events = [a for a in self.alerts.values() if a.kind == "event" and a.closed is None]
        in_ev = {s for a in open_events for s in a.sensors}

        def inside(st):
            return any(dist(st.xy, a.xy) <= a.radius for a in open_events)
        for g, members in self.groups.items():
            live = [m for m in members if vals.get(m.id) is not None]
            # the reference excludes sensors already explained by an open event, so a large
            # event does not drag the reference up and make unaffected sensors look wrong
            ref = [m for m in live if m.id not in in_ev]
            if len(ref) < 3:
                ref = live
            gmed = float(np.median([vals[m.id] for m in ref])) if ref else None
            for st in live:
                v = vals[st.id]
                nbs = self.nb[st.id]
                if st.id not in in_ev and any(n in in_ev for n in nbs):
                    # neighbours that are evidence for an event are not comparable with a sensor
                    # outside it; use the nearest comparable ones within 1.5 km, at least two
                    nbs = [m.id for m in sorted(live, key=lambda m: dist(m.xy, st.xy))
                           if m.id != st.id and m.id not in in_ev and dist(m.xy, st.xy) <= 1500][: self.k]
                    if len(nbs) < 2:
                        nbs = []
                nbv = [vals[n] for n in nbs if vals.get(n) is not None]
                rl = v - float(np.median(nbv)) if nbv else st.rl0 if hasattr(st, "rl0") else 0.0
                rg = v - gmed
                hour = self._recent(st, T, 3600)
                trend = v - float(np.median(hour)) if len(hour) > 8 else 0.0
                win = self._recent(st, T, 12 * st.period + 1)
                d = np.diff(win) if len(win) > 2 else np.array([0.0])
                feats[st.id] = dict(v=v, rl=rl, rg=rg, trend=trend, md=float(np.median(np.abs(d))),
                                    sd=float(np.std(d)), maxdiff=float(np.abs(d).max()), n=len(win))
        return feats

    def _scaled(self, st, f):
        floor = SCALE_FLOOR.get(st.type, 1.0)
        z_local = (f["rl"] - st.rl0) / max(st.s_rl, floor)
        z_global = (f["rg"] - st.rg0) / max(st.s_rg, floor)
        z_trend = f["trend"] / max(st.s_sd * 2, floor)
        flat = 0.0
        if st.type in FLAT_TYPES and f["n"] >= 10 and st.s_md > 0:
            flat = math.log10(st.s_md / max(f["md"], st.s_md * 1e-3))       # 3 = perfectly flat
        spike = f["maxdiff"] / max(st.s_max, floor)
        noise = f["sd"] / max(st.s_sd, floor * 0.5)
        return dict(z_local=z_local, z_global=z_global, z_trend=z_trend, flat=max(0.0, flat), spike=spike, noise=noise)

    # ------------------------------------------------------------ training
    def _learn(self, T):
        feats = self._features(T)
        for sid, f in feats.items():
            st = self.s[sid]
            st.warm_rl.append(f["rl"])
            st.warm_rg.append(f["rg"])
            st.warm_md.append(f["md"])
            st.warm_max.append(f["maxdiff"])
            st.warm_sd.append(f["sd"])

    def _fit(self):
        for st in self.s.values():
            st.rl0 = float(np.median(st.warm_rl)) if st.warm_rl else 0.0
            st.rg0 = float(np.median(st.warm_rg)) if st.warm_rg else 0.0
            st.s_rl = _mad(st.warm_rl) if st.warm_rl else 1.0
            st.s_rg = _mad(st.warm_rg) if st.warm_rg else 1.0
            st.s_md = float(np.median(st.warm_md)) if st.warm_md else 1.0
            st.s_max = float(np.percentile(st.warm_max, 90)) if st.warm_max else 1.0
            st.s_sd = float(np.percentile(st.warm_sd, 90)) if st.warm_sd else 1.0
        for g, X in self.train.items():
            X = np.array(X)
            if len(X) < 20:
                continue
            m = IsolationForest(n_estimators=150, random_state=0, contamination="auto").fit(X)
            sc = m.score_samples(X)
            self.models[g] = m
            # IsolationForest ranks how unusual a feature pattern is but saturates far outside
            # the training data, so it flags the rarest 1% and robust thresholds confirm magnitude
            self.cut[g] = float(np.percentile(sc, 1.0))

    # ------------------------------------------------------------ tick
    def _tick(self, T) -> list[dict]:
        if T <= self.warmup:
            self._learn(T)
            if T + TICK > self.warmup:
                self._fit_warm(T)
            return []
        feats = self._features(T)
        z = {sid: self._scaled(self.s[sid], f) for sid, f in feats.items()}
        stale = {sid for sid, st in self.s.items() if self._stale(st, T)}
        return self._naive(T, z, stale) if self.naive else self._classify(T, feats, z, stale)

    def _fit_warm(self, T):
        # first pass: baselines; second pass: model training on scaled warm features
        self._fit()
        # build training rows from the warm-up buffer by replaying it tick by tick
        for t in np.arange(TICK * 10, T + 1, TICK):
            feats = self._features(t)
            for sid, f in feats.items():
                st = self.s[sid]
                self.train.setdefault(st.group, []).append([abs(v) for v in self._scaled(st, f).values()])
        self._fit()

    # ------------------------------------------------------------ naive baseline
    def _naive(self, T, z, stale):
        out = []
        for sid, f in z.items():
            if abs(f["z_trend"]) > Z or abs(f["z_global"]) > Z:
                out += self._open(T, f"naive:{sid}", "fault", "anomaly", [sid], "value unusual for this sensor", self.s[sid].xy)
            else:
                out += self._close(T, f"naive:{sid}")
        for sid in sorted(stale):
            out += self._open(T, f"naive:{sid}", "fault", "offline", [sid], "no data", self.s[sid].xy)
        return out

    # ------------------------------------------------------------ classifier
    def _anomalous(self, z):
        bad = {}
        by_group: dict = {}
        for sid in z:
            by_group.setdefault(self.s[sid].group, []).append(sid)
        for g, ids in by_group.items():
            m = self.models.get(g)
            if g == "power_load":
                for sid in ids:          # three substations of different sizes: explicit rules only
                    if z[sid]["flat"] >= 2.0:
                        bad[sid] = ("flat", z[sid])
                continue
            if m is None:
                continue
            sc = m.score_samples(np.array([[abs(v) for v in z[sid].values()] for sid in ids]))
            for sid, score in zip(ids, sc):
                f = z[sid]
                bursty = 2.0 if g in ("bridge_strain", "traffic") else 1.0   # traffic makes these jumpy
                strong = (max(abs(f["z_local"]), abs(f["z_global"]), abs(f["z_trend"])) >= Z
                          or f["flat"] >= 1.5 or f["spike"] >= 3.0 * bursty or f["noise"] >= 3.0 * bursty)
                very = (max(abs(f["z_local"]), abs(f["z_global"]), abs(f["z_trend"])) >= 1.5 * Z
                        or f["flat"] >= 2.0 or f["spike"] >= 4.5 * bursty or f["noise"] >= 4.5 * bursty)
                # the forest judges unusual combinations; a very large single deviation stands on its own
                if not strong or (score >= self.cut[g] and not very):
                    continue
                reason = max(f, key=lambda k: abs(f[k]) / {"flat": 1.0, "spike": 3.0 * bursty, "noise": 3.0 * bursty}.get(k, Z))
                # a clear sensor signature beats a large deviation: it says how the sensor misbehaves
                if f["flat"] >= 2.0:
                    reason = "flat"
                elif f["spike"] >= 4.5 * bursty:
                    reason = "spike"
                elif f["noise"] >= 4.5 * bursty:
                    reason = "noise"
                bad[sid] = (reason, f)
        return bad

    def _classify(self, T, feats, z, stale):
        out = []
        bad = self._anomalous(z)
        # persistence: a sensor must look anomalous on two consecutive ticks
        cur = set(bad)
        confirmed = {sid for sid in cur if self.persist.get(sid, 0) >= 1}
        for sid in list(self.persist):
            if sid not in cur:
                self.persist.pop(sid)
        for sid in sorted(cur):
            self.persist[sid] = self.persist.get(sid, 0) + 1

        # 1. power outage: a substation's load collapses, or its mains sensors fall silent together
        explained = set()
        for sid, st in self.s.items():
            if st.type != "power_load":
                continue
            f = feats.get(sid)
            hour = self._recent(st, T, 3600)
            dead_load = f is not None and hour and f["v"] < 0.25 * float(np.median(hour))
            silent = [o for o in sorted(stale) if self.s[o].substation == self._sub_of(sid)]
            if dead_load or len(silent) >= 3:
                explained |= set(silent)
                evid = silent + ([sid] if dead_load else [])
                out += self._event(T, "outage", evid, f"{len(silent)} mains-powered sensors silent"
                                   + (", substation load collapsed" if dead_load else ""), key=f"outage:{self._sub_of(sid)}")
        # 2. clusters of anomalous sensors that agree with each other -> events
        in_event = set()
        untrusted = {a.sensors[0] for a in self.alerts.values() if a.kind == "fault" and a.closed is None}
        # an area-wide event (storm) moves every station alike; while one is open, a station far
        # from the others is suspect and cannot be evidence for it
        area_open = {a.cls for a in self.alerts.values() if a.kind == "event" and a.closed is None}
        suspect = {sid for sid in confirmed if self.s[sid].group in AREA_WIDE
                   and EVENT_OF.get(self.s[sid].group) in area_open and abs(z[sid]["z_local"]) >= Z}
        for g, members in self.groups.items():
            pts = [m.id for m in members if m.id in confirmed and m.id not in untrusted and m.id not in suspect
                   and bad[m.id][0] not in ("flat", "spike", "noise")
                   and abs(z[m.id]["z_global"]) + abs(z[m.id]["z_trend"]) > Z * 0.6]
            for sign in (1, -1):
                cand = [sid for sid in pts if np.sign(z[sid]["z_global"] if abs(z[sid]["z_global"]) > abs(z[sid]["z_trend"]) else z[sid]["z_trend"]) == sign]
                for cl in self._clusters(cand, eps=900.0 if g != "weather" else 5000.0):
                    if g in AREA_WIDE and len(cl) >= 3:
                        # an area-wide event moves every member alike; one far from the others is suspect
                        odd = [s for s in cl if abs(z[s]["z_local"]) >= Z]
                        if len(odd) < len(cl) / 2:
                            suspect |= set(odd)
                            cl = [s for s in cl if s not in odd]
                    if len(cl) < 2:
                        continue
                    # members must agree with each other more than with the rest of the group
                    agree = np.median([abs(z[s]["z_local"]) for s in cl]) < np.median([abs(z[s]["z_global"]) + abs(z[s]["z_trend"]) for s in cl])
                    if not agree:
                        continue
                    cls = EVENT_OF.get(g, g)
                    if g == "air_quality" and sign < 0:
                        continue            # cleaner air than usual is not an incident
                    if g == "traffic":
                        cls = "traffic_disruption"
                    in_event |= set(cl)
                    out += self._event(T, cls, cl, f"{len(cl)} {g.replace('_', ' ')} sensors {'rising' if sign > 0 else 'falling'} together",
                                       sign=int(sign))
        # rain at two weather stations is a storm whatever the temperature does
        rainy = [sid for sid, st in self.s.items() if st.type == "weather" and sid not in suspect
                 and getattr(self, "_rain", {}).get(sid, 0) > 5]
        if len(rainy) >= 2:
            in_event |= set(rainy)
            out += self._event(T, "storm", rainy, f"rain over 5 mm/h at {len(rainy)} stations")

        # 3. what is left: sensor faults, or deviations nobody can explain yet
        in_ev = {s for a in self.alerts.values() if a.kind == "event" and a.closed is None for s in a.sensors}
        watching = set()
        for sid in sorted(confirmed - in_event - explained):
            reason, f = bad[sid]
            cls = {"flat": "stuck", "spike": "spike", "noise": "noise"}.get(reason)
            if cls is None and sid in untrusted:
                cls = self.alerts[f"fault:{sid}"].cls
            if cls is None and sid in suspect:
                cls = "drift" if self._slow(sid, T) else "disagrees"
            if cls is None:
                if abs(f["z_local"]) < Z * 0.5:
                    continue            # unusual but agrees with neighbours: not a fault
                if sid in untrusted:
                    cls = self.alerts[f"fault:{sid}"].cls      # still misbehaving: keep the fault open
                elif sid in in_ev:
                    continue                                    # evidence for an event it is part of
                else:
                    main = f["z_global"] if abs(f["z_global"]) >= Z * 0.4 else f["z_trend"]
                    # neighbours starting to move the same way: an event may be forming, wait
                    if any(n in z and np.sign(z[n]["z_global"]) == np.sign(main) and abs(z[n]["z_global"]) > Z * 0.4
                           for n in self.nb[sid]):
                        continue
                    # on the edge of an open event of its kind, moving the same way: evidence, not a fault
                    near = self._near_event(sid, np.sign(main))
                    if near is not None:
                        out += self._close(T, f"anomaly:{sid}")
                        out += self._open(T, near.id, "event", near.cls, [sid], near.reason, near.xy, near.radius)
                        continue
                    # alone: watch it; a fault if nothing explains it for 8 of the last 15 minutes
                    w = self.watch.setdefault(sid, deque(maxlen=15))
                    w.append(1)
                    watching.add(sid)
                    if sum(w) < 8:
                        out += self._open(T, f"anomaly:{sid}", "anomaly", "unexplained", [sid],
                                          f"{'rising' if main > 0 else 'falling'} alone; watching", self.s[sid].xy)
                        continue
                    cls = "drift" if self._slow(sid, T) else "step"
            out += self._close(T, f"anomaly:{sid}")
            out += self._open(T, f"fault:{sid}", "fault", cls, [sid],
                              f"{reason.replace('_', ' ')} {f[reason]:+.1f}; neighbours normal", self.s[sid].xy)
        for sid in list(self.watch):
            if sid not in watching:
                self.watch[sid].append(0)       # sliding window: an intermittent deviation still adds up
                if sum(self.watch[sid]) == 0:
                    self.watch.pop(sid)
                    out += self._close(T, f"anomaly:{sid}")
        for sid in sorted(stale - explained):
            if self.s[sid].last_t is not None:
                out += self._open(T, f"fault:{sid}", "fault", "offline", [sid], "no data, no outage to explain it", self.s[sid].xy)
        # close alerts no longer supported
        live_faults = {f"fault:{s}" for s in (confirmed - in_event - explained)} | {f"fault:{s}" for s in stale - explained} \
            | {f"anomaly:{s}" for s in watching}
        for aid, a in list(self.alerts.items()):
            if a.closed is not None:
                continue
            if a.kind in ("fault", "anomaly") and aid not in live_faults and T - a.t_last > 3 * TICK:
                out += self._close(T, aid)
            if a.kind == "event" and T - a.t_last > 5 * TICK:
                out += self._close(T, aid)
        return out

    def _sub_of(self, power_sensor_id):
        # power_load sensors are named pl-<substation osm id>
        return "substation-" + power_sensor_id.split("-", 1)[1]

    def _slow(self, sid, T):
        """A drift grows steadily; check the last 20 minutes are monotone-ish."""
        st = self.s[sid]
        r = self._recent(st, T, 1200)
        if len(r) < 20:
            return False
        a, b = np.mean(r[:5]), np.mean(r[-5:])
        return abs(b - a) > 3 * max(st.s_rl, SCALE_FLOOR.get(st.type, 1.0))

    def _clusters(self, ids, eps):
        ids = list(ids)
        seen, out = set(), []
        for i in ids:
            if i in seen:
                continue
            comp, stack = [], [i]
            seen.add(i)
            while stack:
                a = stack.pop()
                comp.append(a)
                for b in ids:
                    if b not in seen and dist(self.s[a].xy, self.s[b].xy) <= eps:
                        seen.add(b)
                        stack.append(b)
            out.append(sorted(comp))
        return out

    # ------------------------------------------------------------ alert bookkeeping
    def _near_event(self, sid, sign):
        st = self.s[sid]
        cls = EVENT_OF.get(st.group)
        margin = {"plume": 1300.0}.get(cls, 700.0)        # plumes are long and narrow
        for a in self.alerts.values():
            if (a.kind == "event" and a.closed is None and a.cls == cls and a.detail.get("sign") == int(sign)
                    and dist(a.xy, st.xy) <= a.radius + margin):
                return a
        return None

    def _event(self, T, cls, sensors, reason, key=None, sign=None):
        xs = [self.s[s].xy for s in sensors]
        c = (float(np.mean([p[0] for p in xs])), float(np.mean([p[1] for p in xs])))
        radius = max([dist(c, p) for p in xs] + [150.0]) + 150.0
        if key is None:
            # merge with an open event of the same class nearby
            for aid, a in self.alerts.items():
                if a.kind == "event" and a.cls == cls and a.closed is None and dist(a.xy, c) < 1500:
                    key = aid
                    break
            if key is None:
                key = f"event:{cls}:{self.counter + 1}"
        out = self._open(T, key, "event", cls, sensors, reason, c, radius)
        if sign is not None and key in self.alerts:
            self.alerts[key].detail["sign"] = sign
        return out

    def _open(self, T, aid, kind, cls, sensors, reason, xy, radius=0.0):
        a = self.alerts.get(aid)
        if a is not None and a.closed is None:
            a.t_last = T
            changed = set(sensors) - set(a.sensors)
            a.sensors = sorted(set(a.sensors) | set(sensors))
            a.xy, a.radius = xy, max(a.radius, radius)
            if changed:
                ev = {"op": "update", "t": T, **a.public()}
                self.history.append(ev)
                return [ev]
            return []
        self.counter += 1
        a = Alert(aid, kind, cls, T, sorted(sensors), reason, xy, T, radius=radius)
        self.alerts[aid] = a
        ev = {"op": "open", "t": T, **a.public()}
        self.history.append(ev)
        return [ev]

    def _close(self, T, aid):
        a = self.alerts.get(aid)
        if a is None or a.closed is not None:
            return []
        a.closed = T
        del self.alerts[aid]
        ev = {"op": "close", "t": T, **a.public()}
        self.history.append(ev)
        return [ev]

    def note_extra(self, sid, extra: dict):
        if extra and "rain_mmh" in extra:
            self.__dict__.setdefault("_rain", {})[sid] = extra["rain_mmh"]
