import json
import random

from case.detect import Detector
from case.pipeline import Pipeline, Recorder, verify_recording


def _grid(n=9, spacing=300.0):
    return [{"id": f"w{i}", "type": "water_level", "xy": [(i % 3) * spacing, (i // 3) * spacing], "period": 15.0,
             "power": "solar"} for i in range(n)]


FROZEN: set = set()      # sensors whose value is reported exactly, with no measurement noise


def _run(det, until, value):
    rng = random.Random(4)
    out = []
    t = 0.0
    while t <= until:
        for s in det.s:
            v = value(s, t)
            out += det.ingest(s, t, v if s in FROZEN else v + rng.gauss(0, 0.004))
        t += 15.0
    return out


def test_one_stuck_sensor_is_a_fault_not_an_event():
    det = Detector(_grid())
    frozen = {}

    def v(s, t):
        base = 219.0 + 0.02 * (t / 3600)
        if s == "w4" and t > 2400:
            FROZEN.add(s)
            return frozen.setdefault(s, base)
        FROZEN.discard(s)
        return base
    alerts = _run(det, 4200, v)
    faults = [a for a in alerts if a["op"] == "open" and a["kind"] == "fault"]
    assert [a["sensors"] for a in faults] == [["w4"]] and faults[0]["cls"] == "stuck"
    assert not [a for a in alerts if a["kind"] == "event"]


def test_neighbours_rising_together_are_one_event():
    det = Detector(_grid())

    def v(s, t):
        rise = 0.5 * min(1, max(0, (t - 2400) / 900)) if s in ("w0", "w1", "w3", "w4") else 0.0
        return 219.0 + rise
    alerts = _run(det, 4200, v)
    events = [a for a in alerts if a["op"] == "open" and a["kind"] == "event"]
    assert len(events) == 1 and events[0]["cls"] == "flood"
    assert not [a for a in alerts if a["op"] == "open" and a["kind"] == "fault"]


def test_pipeline_tags_sensor_messages():
    # regression: the reading's own "type" (sensor type) overwrote the message tag
    p = Pipeline(_grid(), naive=False)
    out = p.process("mqtt", "case/sensor/w0", json.dumps({"id": "w0", "type": "water_level", "t": 15, "v": 219.0}).encode(), 1.0)
    assert out[0]["type"] == "sensor" and out[0]["sensor_type"] == "water_level"


def test_recording_hash_chain_detects_tampering(tmp_path):
    f = tmp_path / "rec.jsonl"
    r = Recorder(f)
    for i in range(5):
        r.write("mqtt", "case/sensor/w0", json.dumps({"i": i}).encode(), float(i))
    assert verify_recording(f) == (True, 5, "ok")
    lines = f.read_text().splitlines()
    lines[2] = lines[2].replace('"t_recv":2.0', '"t_recv":2.5')
    f.write_text("\n".join(lines) + "\n")
    assert verify_recording(f)[0] is False
