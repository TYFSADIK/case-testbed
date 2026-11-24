#!/usr/bin/env python3
"""Score a distributed run: the sim's truth log against the twin's alert history.

Runs inside the tester container (truth volume at /data/truth, backend at
http://backend:8000). Writes results/<run>/detection.json.
"""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from case.evaluate import score  # noqa: E402

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
lines = [json.loads(x) for x in open("/data/truth/truth.jsonl")]
header = lines[0]
truth = [r for r in lines[1:] if r.get("type") == "sample"]
end = next((r for r in lines if r.get("type") == "end"), None)
api = "http://backend:8000"
twin = json.load(urllib.request.urlopen(f"{api}/api/alerts?detector=twin"))["history"]
naive = json.load(urllib.request.urlopen(f"{api}/api/alerts?detector=naive"))["history"]
stats = json.load(urllib.request.urlopen(f"{api}/api/stats"))
res = {"scenario_end": end, "backend": stats, "twin": score(header, truth, twin, "twin"),
       "naive": score(header, truth, naive, "naive")}
(out / "detection.json").write_text(json.dumps(res, indent=1))
(out / "alerts-twin.json").write_text(json.dumps(twin, indent=1))
for k in ("twin", "naive"):
    r = res[k]
    print(f"{k:6} alerts {r['alerts_raised']:3}  faults {r['faults']['detected']}/{r['faults']['total']} (median {r['faults']['median_ttd_s']} s)"
          f"  events {r['events']['detected']}/{r['events']['total']} (median {r['events']['median_ttd_s']} s)"
          f"  false faults {r['false_fault_alerts']}  events called faults {r['events_misreported_as_faults']}")
