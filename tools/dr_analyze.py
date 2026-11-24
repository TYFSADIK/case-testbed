#!/usr/bin/env python3
"""Dead-reckoning error across the WAN, per condition.

Truth: site B's 20 Hz position log (/data/dr-truth/truth*.csv).
Estimate: the twin's 20 Hz log for the same window (/data/twin/dr/<label>.csv)
with three estimators side by side:
  hold     show the last reported position (no dead reckoning)
  dr_recv  extrapolate from the time the PDU was RECEIVED
  dr_ts    extrapolate from the PDU's own timestamp (needs synchronised clocks)
PDU counts come from the gateway's counters, sampled before and after.
"""
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

KIND = {"2-1": "uav", "2-10": "convoy", "2-11": "convoy", "2-12": "convoy", "3-1": "ros2_robot"}


def load_truth(d):
    tr = defaultdict(list)
    for f in sorted(Path(d).glob("truth*.csv")):
        for row in csv.reader(open(f)):
            if len(row) != 4 or row[0] == "t":          # header lines (the ROS bridge writes none)
                continue
            tr[row[1]].append((float(row[0]), float(row[2]), float(row[3])))
    out = {}
    for k, rows in tr.items():
        a = np.array(sorted(rows))
        out[k] = a
    return out


def analyze(label, truth, est_dir, skip=3.0):
    rows = list(csv.DictReader(open(Path(est_dir) / f"{label}.csv")))
    if not rows:
        return {}
    t0 = float(rows[0]["t"]) + skip
    err = defaultdict(lambda: defaultdict(list))
    gaps = defaultdict(int)
    for r in rows:
        t, e = float(r["t"]), r["entity"]
        if t < t0 or e not in truth:
            continue
        a = truth[e]
        if t > a[-1, 0] or t < a[0, 0]:
            continue
        k = int(np.searchsorted(a[:, 0], t))
        near = min(abs(a[min(k, len(a) - 1), 0] - t), abs(a[max(k - 1, 0), 0] - t))
        if near > 0.2:                      # no truth sample close by (e.g. a federate restarted): do not score
            gaps[e] += 1
            continue
        x, y = np.interp(t, a[:, 0], a[:, 1]), np.interp(t, a[:, 0], a[:, 2])
        for m in ("hold", "dr_recv", "dr_ts"):
            err[KIND.get(e, e)][m].append(float(np.hypot(float(r[f"{m}_x"]) - x, float(r[f"{m}_y"]) - y)))
    out = {}
    for kind, d in err.items():
        out[kind] = {m: {"mean_m": round(float(np.mean(v)), 2), "p95_m": round(float(np.percentile(v, 95)), 2),
                         "max_m": round(float(np.max(v)), 2)} for m, v in d.items()}
    if gaps:
        out["unscored_rows_no_truth"] = dict(gaps)
    return out


if __name__ == "__main__":
    plan = json.load(open(sys.argv[1]))         # [{"label":..., "delay_ms":..., "loss":..., "threshold":..., "pdus":{...}, "seconds":...}]
    truth = load_truth("/data/dr-truth")
    res = []
    for c in plan:
        r = dict(c)
        r["error"] = analyze(c["label"], truth, "/data/twin/dr")
        res.append(r)
    Path(sys.argv[2]).write_text(json.dumps(res, indent=1))
    for r in res:
        e = r["error"]
        print(r["label"], json.dumps({k: {m: v["mean_m"] for m, v in d.items()} for k, d in e.items() if k != "unscored_rows_no_truth"}),
              "pdu/s", r.get("pdu_rate"), "unscored", e.get("unscored_rows_no_truth", {}))
