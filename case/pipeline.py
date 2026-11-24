"""The twin's processing core: one ordered stream in, state and alerts out.

The backend feeds every message (MQTT telemetry, DIS entity state, instructor
injects) through `Pipeline.process` in arrival order, and records each one
first. Replaying a recording through a fresh Pipeline therefore rebuilds the
same state and the same alerts, which is what after-action review relies on.
Nothing in here reads the wall clock.
"""
from __future__ import annotations

import base64
import hashlib
import json

from . import dis
from .detect import Detector
from .geo import to_latlon, to_local

EPOCH0 = 1795690800.0        # 2026-11-23 06:00 EST: simulated time 0 at site A


class Pipeline:
    def __init__(self, registry: list[dict], naive: bool = True):
        self.registry = {r["id"]: r for r in registry}
        self.det = Detector(registry)
        self.naive = Detector(registry, naive=True) if naive else None
        self.latest: dict = {}           # sensor id -> last message
        self.entities: dict = {}         # (site, app, entity) -> (EntityState, sim or wall receive time)
        self.sim_t = 0.0
        self.counts = {"mqtt": 0, "dis": 0, "inject": 0, "register": 0}

    def register(self, r: dict) -> bool:
        """A sensor announced itself (e.g. a hardware node). New sensors join the twin's map;
        the detector only scores sensors that were present for its warm-up."""
        if r["id"] in self.registry:
            return False
        if "xy" not in r and "latlon" in r:
            r["xy"] = list(to_local(*r["latlon"]))
        self.registry[r["id"]] = r
        self.counts["register"] += 1
        return True

    def process(self, kind: str, topic: str | None, payload: bytes, t_recv: float) -> list[dict]:
        """Returns events for the outside world: {"type": "sensor"|"alert"|"entity"|"clock"|...}."""
        out: list[dict] = []
        if kind == "mqtt":
            self.counts["mqtt"] += 1
            try:
                m = json.loads(payload)
            except ValueError:
                return out
            if topic.startswith("case/registry/"):
                if self.register(m):
                    out.append({"type": "registered", "sensor": m})
                return out
            if topic == "case/clock":
                self.sim_t = max(self.sim_t, float(m["t"]))
                for ev in self.det.advance(self.sim_t) + (self.naive.advance(self.sim_t) if self.naive else []):
                    out.append(self._alert(ev))
                out.append({"type": "clock", "t": self.sim_t})
                return out
            if topic.startswith("case/sensor/"):
                sid = m.get("id")
                if sid not in self.registry:
                    return out
                self.latest[sid] = m
                t = float(m["t"])
                if not self.registry[sid].get("hardware"):
                    self.sim_t = max(self.sim_t, t)
                    if "rain_mmh" in m:
                        self.det.note_extra(sid, m)
                    evs = self.det.ingest(sid, t, m["v"])
                    if self.naive:
                        evs += self.naive.ingest(sid, t, m["v"])
                    out += [self._alert(e) for e in evs]
                out.append({**m, "sensor_type": m.get("type"), "type": "sensor", "t_recv": t_recv})
            return out
        if kind == "dis":
            self.counts["dis"] += 1
            es = dis.decode(payload, now=t_recv if not _is_site_a(payload) else EPOCH0 + self.sim_t)
            if es is None:
                return out
            self.entities[es.key] = (es, t_recv)
            out.append({"type": "entity", "key": list(es.key), "kind": es.kind, "marking": es.marking,
                        "lat": es.lat, "lon": es.lon, "alt": es.alt, "ve": es.ve, "vn": es.vn, "t": es.t})
            return out
        if kind == "inject":
            self.counts["inject"] += 1
            out.append({"type": "inject", **json.loads(payload)})
        return out

    def _alert(self, ev: dict) -> dict:
        lat, lon = to_latlon(*ev["xy"])
        return {"type": "alert", "detector": "naive" if ev["id"].startswith("naive:") else "twin",
                "lat": lat, "lon": lon, **ev}

    def alerts(self, detector="twin"):
        d = self.det if detector == "twin" else self.naive
        return [a.public() for a in d.alerts.values()] if d else []


def _is_site_a(payload: bytes) -> bool:
    # entity ID site field: bytes 12-13 of the PDU (after the 12-byte header)
    return len(payload) > 14 and int.from_bytes(payload[12:14], "big") == 1


class Recorder:
    """Append-only recording with a SHA-256 hash chain; one JSON line per input."""

    def __init__(self, path):
        self.f = open(path, "a", buffering=1)
        self.prev = "0" * 64
        self.seq = 0

    def write(self, kind, topic, payload: bytes, t_recv: float, extra=None):
        self.seq += 1
        rec = {"seq": self.seq, "kind": kind, "topic": topic, "t_recv": round(t_recv, 6),
               "payload": base64.b64encode(payload).decode(), "prev": self.prev}
        if extra:
            rec["extra"] = extra
        body = json.dumps(rec, sort_keys=True, separators=(",", ":"))
        h = hashlib.sha256(body.encode()).hexdigest()
        self.prev = h
        self.f.write(body[:-1] + f',"hash":"{h}"}}\n')
        return h


def verify_recording(path):
    prev, n = "0" * 64, 0
    for line in open(path):
        rec = json.loads(line)
        h = rec.pop("hash")
        if rec["prev"] != prev:
            return False, n, "broken link"
        body = json.dumps(rec, sort_keys=True, separators=(",", ":"))
        if hashlib.sha256(body.encode()).hexdigest() != h:
            return False, n, "hash mismatch"
        prev, n = h, n + 1
    return True, n, "ok"
