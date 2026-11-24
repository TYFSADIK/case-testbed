#!/usr/bin/env python3
"""After-action review check: verify a recording and replay it.

1. Verify the SHA-256 hash chain of the recording (tamper evidence).
2. Feed every recorded input, in order, through a fresh Pipeline.
3. Compare the alerts the replay produces with the alerts the live twin
   recorded. They must match exactly: same order, ids, classes, times, sensors.
"""
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from case.engine import sensor_registry   # noqa: E402
from case.pipeline import Pipeline, verify_recording  # noqa: E402
from case.world import load               # noqa: E402

rec = Path(sys.argv[1])
ok, n, why = verify_recording(rec)
print(f"hash chain: {why}, {n} records")
t0 = time.time()
p = Pipeline(sensor_registry(load()))
live, replayed = [], []
for line in open(rec):
    r = json.loads(line)
    payload = base64.b64decode(r["payload"])
    if r["kind"] == "alert":
        live.append(json.loads(payload))
        continue
    for o in p.process(r["kind"], r["topic"], payload, r["t_recv"]):
        if o["type"] == "alert":
            replayed.append({k: o[k] for k in ("op", "id", "cls", "t", "sensors", "detector")})
match = live == replayed
res = {"chain_ok": ok, "records": n, "chain": why, "alerts_live": len(live), "alerts_replayed": len(replayed),
       "identical": match, "replay_seconds": round(time.time() - t0, 1)}
if not match:
    for i, (a, b) in enumerate(zip(live, replayed)):
        if a != b:
            res["first_difference"] = {"index": i, "live": a, "replay": b}
            break
print(json.dumps(res))
if len(sys.argv) > 2:
    Path(sys.argv[2]).write_text(json.dumps(res, indent=1))
