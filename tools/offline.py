#!/usr/bin/env python3
"""Run the whole scenario in one process, as fast as possible.

Same engine, same detector, no network: used to tune and unit-check the
detector in seconds instead of running the distributed stack. The rehearsal
(local/rehearse.sh) runs the real thing; its numbers are the published ones.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from case.detect import Detector              # noqa: E402
from case.engine import Engine, sensor_registry  # noqa: E402
from case.evaluate import score               # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--scenario", default="scenarios/exercise-1.yaml")
ap.add_argument("--vehicles", type=int, default=100)
ap.add_argument("--seed", type=int, default=1)
ap.add_argument("--out")
ap.add_argument("--quiet", action="store_true")
ap.add_argument("--cache", help="pickle of the simulation output: reuse it if present (detector tuning)")
a = ap.parse_args()

import pickle  # noqa: E402

t0 = time.time()
if a.cache and Path(a.cache).exists():
    reg, steps, truth, hdr = pickle.load(open(a.cache, "rb"))
else:
    eng = Engine(a.scenario, vehicles=a.vehicles, seed=a.seed)
    reg = sensor_registry(eng.world)
    steps, truth = [], []
    while eng.t < eng.duration:
        msgs, tr = eng.step(1.0)
        truth += tr
        steps.append((eng.t, msgs))
    hdr = eng.truth_header()
    if a.cache:
        pickle.dump((reg, steps, truth, hdr), open(a.cache, "wb"))
smart, naive = Detector(reg), Detector(reg, naive=True)
n = 0
for t, msgs in steps:
    for m in msgs:
        n += 1
        if "rain_mmh" in m:
            smart.note_extra(m["id"], m)
        smart.ingest(m["id"], m["t"], m["v"])
        naive.ingest(m["id"], m["t"], m["v"])
    smart.advance(t)
    naive.advance(t)
res = {"messages": n, "seconds": round(time.time() - t0, 1),
       "twin": score(hdr, truth, smart.history, "twin"), "naive": score(hdr, truth, naive.history, "naive")}
if a.out:
    Path(a.out).write_text(json.dumps(res, indent=1))
if not a.quiet:
    for k in ("twin", "naive"):
        r = res[k]
        print(f"{k:6} alerts {r['alerts_raised']:4}  faults {r['faults']['detected']}/{r['faults']['total']} "
              f"(median ttd {r['faults']['median_ttd_s']} s)  events {r['events']['detected']}/{r['events']['total']} "
              f"(median ttd {r['events']['median_ttd_s']} s)  false faults {r['false_fault_alerts']}  "
              f"events called faults {r['events_misreported_as_faults']}")
    tw = res["twin"]
    print("missed faults:", [f for f in tw["faults"]["per_fault"] if not f["detected"]])
    print("faults:", [(f["fault"], f.get("ttd_s"), f.get("as")) for f in tw["faults"]["per_fault"] if f["detected"]])
    print("events:", tw["events"]["per_event"])
    print("false:", tw["false_fault_detail"][:12])
    print("event->fault:", tw["event_sensors_called_faults_detail"][:12])
    print("other events:", tw["other_event_alerts"][:12])
    print("watching:", {k: v for k, v in tw["watching_alerts"].items() if k != "detail"}, tw["watching_alerts"]["detail"][:12])
    print(f"{n} messages in {res['seconds']} s")
