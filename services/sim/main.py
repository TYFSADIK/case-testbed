"""Site A federate: the city simulation.

Runs the scenario engine at SPEED x real time, publishes every sensor reading
to MQTT (case/sensor/<id>), every vehicle to DIS (UDP to the twin, threshold
dead reckoning + 5 s heartbeat, simulated-time stamps), and writes ground
truth to /data/truth/truth.jsonl for the evaluation tools only.

Instructor injects arrive on case/control. With SKIP_TO the engine runs ahead
silently to that simulated time first (used to stage the 3D screenshots).
"""
import json
import os
import socket
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from case import dis                                    # noqa: E402
from case.engine import Engine, sensor_registry         # noqa: E402
from case.geo import to_latlon, to_local                # noqa: E402
from case.pipeline import EPOCH0                         # noqa: E402

SPEED = float(os.environ.get("SPEED", "1"))
SCENARIO = os.environ.get("SCENARIO", "scenarios/exercise-1.yaml")
VEHICLES = int(os.environ.get("VEHICLES", "100"))
SEED = int(os.environ.get("SEED", "1"))
SKIP_TO = float(os.environ.get("SKIP_TO", "0"))
SLOW_AT = float(os.environ.get("SLOW_AT", "inf"))     # switch to real time at this simulated time
DIS_TARGET = (os.environ.get("DIS_HOST", "backend"), int(os.environ.get("DIS_PORT", "3000")))
TRUTH = Path(os.environ.get("TRUTH_DIR", "/data/truth"))
LOOP = os.environ.get("LOOP", "0") == "1"
ROOT = Path(__file__).resolve().parents[2]

TRUTH.mkdir(parents=True, exist_ok=True)
eng = Engine(ROOT / SCENARIO, vehicles=VEHICLES, seed=SEED)
truth_f = open(TRUTH / "truth.jsonl", "w", buffering=1)
truth_f.write(json.dumps(eng.truth_header()) + "\n")
controls: list = []

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="site-a-sim")
c.on_message = lambda _c, _u, m: controls.append(json.loads(m.payload))
c.on_connect = lambda c, *_: c.subscribe("case/control", qos=1)
while True:
    try:
        c.connect(os.environ.get("MQTT_HOST", "mosquitto"), 1883, 30)
        break
    except OSError:
        time.sleep(1)
c.loop_start()
for r in sensor_registry(eng.world):
    c.publish(f"case/registry/{r['id']}", json.dumps(r), qos=1, retain=True)

udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sender = dis.Sender(threshold_m=1.0)


def apply_controls():
    while controls:
        m = controls.pop(0)
        t0 = eng.t
        dur = float(m.get("duration", 1800))
        if m.get("op") == "event":
            xy = to_local(m["lat"], m["lon"]) if "lat" in m else None
            e = eng.inject_event(m["kind"], t0, t0 + dur, xy, m.get("params", {}))
            truth_f.write(json.dumps({"type": "inject", "t": t0, "event": e.__dict__}) + "\n")
        elif m.get("op") == "fault":
            f = eng.inject_fault(m["sensor"], m["kind"], t0, t0 + dur, m.get("params", {}))
            truth_f.write(json.dumps({"type": "inject", "t": t0, "fault": f.__dict__}) + "\n")


def send_vehicles(now_sim_epoch):
    for v in eng.traffic.vehicles:
        es = dis.state_from_local(1, 1, v.id, v.kind, f"CAR{v.id:03d}", v.xy, v.vel, v.heading, now_sim_epoch)
        if sender.due(es, now_sim_epoch):
            udp.sendto(dis.encode(es), DIS_TARGET)
            sender.mark(es)


def publish_truth():
    """Instructor overlay: what is really happening (the twin's detector never sees this)."""
    t = eng.t
    ev = []
    for e in eng.env.events:
        lv = e.level(t)
        if lv <= 0:
            continue
        item = {"id": e.id, "kind": e.kind, "level": round(lv, 3)}
        if e.xy:
            item["lat"], item["lon"] = to_latlon(*e.xy)
        if e.kind == "plume":
            speed, ang = eng.env.wind(t)
            item["toward_deg"] = round((90 - __import__("math").degrees(ang)) % 360, 1)
        if e.kind == "outage":
            item["substation"] = e.params.get("substation")
        ev.append(item)
    area = eng.env.flooded_area(t)
    flood = None
    if area is not None and not area.is_empty:
        simp = area.simplify(8)
        polys = getattr(simp, "geoms", [simp])
        flood = [[[round(v, 6) for v in to_latlon(x, y)[::-1]] for x, y in p.exterior.coords] for p in polys if p.area > 200]
    faults = [{"id": f.id, "sensor": f.sensor, "kind": f.kind} for f in eng.faults.faults if f.t0 <= t <= f.t1]
    c.publish("case/truth/state", json.dumps({"t": t, "events": ev, "faults": faults, "flood": flood,
                                               "closed_edges": len(eng.closed_now)}))


# run ahead silently
while eng.t < SKIP_TO:
    _, tr = eng.step(1.0)
    for r in tr:
        truth_f.write(json.dumps(r) + "\n")

start_wall, start_sim = time.time(), eng.t
last_truth = -999.0
sent_msgs = 0
while True:
    if eng.t >= SLOW_AT and SPEED != 1.0:
        SPEED, start_wall, start_sim = 1.0, time.time(), eng.t
        print(f"real time from t={eng.t:.0f}", flush=True)
    target = start_sim + (time.time() - start_wall) * SPEED
    if eng.t + 1.0 > target:
        time.sleep(min(0.02, (eng.t + 1.0 - target) / SPEED))
        continue
    apply_controls()
    msgs, tr = eng.step(1.0)
    now_wall = time.time()
    for m in msgs:
        m["ts"] = round(now_wall, 4)
        c.publish(f"case/sensor/{m['id']}", json.dumps(m))
        sent_msgs += 1
    for r in tr:
        truth_f.write(json.dumps(r) + "\n")
    send_vehicles(EPOCH0 + eng.t)
    c.publish("case/clock", json.dumps({"t": eng.t, "speed": SPEED}))
    if eng.t - last_truth >= 10:
        publish_truth()
        last_truth = eng.t
    if eng.t >= eng.duration:
        truth_f.write(json.dumps({"type": "end", "t": eng.t, "sensor_msgs": sent_msgs, "pdus": sender.sent}) + "\n")
        c.publish("case/status", json.dumps({"done": True, "t": eng.t}), qos=1, retain=True)
        print(f"scenario complete: {sent_msgs} sensor messages, {sender.sent} PDUs", flush=True)
        if not LOOP:
            while True:
                time.sleep(3600)
        eng = Engine(ROOT / SCENARIO, vehicles=VEHICLES, seed=SEED)
        start_wall, start_sim = time.time(), 0.0
