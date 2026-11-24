"""Score the twin's alerts against the simulation's ground truth.

Ground truth comes from the site A simulator's truth log: the scenario header
(every event and fault with its time window) and per-sample records saying
which sensors an event really moved and which faults were active.
"""
from __future__ import annotations

from collections import defaultdict

GRACE = 600.0        # an alert may open up to 10 minutes after a fault or event ends and still count
MATCH = {"flood": {"flood"}, "plume": {"plume"}, "storm": {"storm"}, "outage": {"outage"}}


def affected_sets(truth_records):
    """sensor -> set of event kinds that genuinely moved it (beyond noise) at some point."""
    out = defaultdict(set)
    first = {}
    for r in truth_records:
        if r.get("type") != "sample":
            continue
        for kind, c in r["events"].items():
            big = {"flood": 0.03, "storm": 0.0, "plume": 4.0, "outage": 0.5}.get(kind, 0)
            if c > big:
                out[r["sensor"]].add(kind)
                first.setdefault((r["sensor"], kind), r["t"])
        if not r["powered"]:
            out[r["sensor"]].add("outage")
            first.setdefault((r["sensor"], "outage"), r["t"])
    return out, first


def score(header, truth_records, history, label="twin"):
    faults, events = header["faults"], header["events"]
    opens = [h for h in history if h["op"] == "open"]
    fault_alerts = [h for h in opens if h["kind"] == "fault"]
    event_alerts = [h for h in opens if h["kind"] == "event"]
    affected, _ = affected_sets(truth_records)

    # faults: detected if a fault alert on that sensor opened in the window
    fres = []
    used = set()
    for f in faults:
        hit = [a for a in fault_alerts if f["sensor"] in a["sensors"] and f["t0"] - 60 <= a["t"] <= f["t1"] + GRACE]
        if hit:
            a = min(hit, key=lambda a: a["t"])
            used |= {id(x) for x in hit}
            fres.append({"fault": f["id"], "sensor": f["sensor"], "kind": f["kind"], "detected": True,
                         "ttd_s": round(a["t"] - f["t0"]), "as": a["cls"]})
        else:
            flagged = [a for a in opens if a["kind"] == "anomaly" and f["sensor"] in a["sensors"]
                       and f["t0"] - 60 <= a["t"] <= f["t1"] + GRACE]
            fres.append({"fault": f["id"], "sensor": f["sensor"], "kind": f["kind"], "detected": False,
                         "flagged_unexplained_s": round(min(x["t"] for x in flagged) - f["t0"]) if flagged else None})

    # false fault alerts, split by what was really going on at that sensor
    false_faults, events_called_faults = [], []
    for a in fault_alerts:
        if id(a) in used:
            continue
        sid = a["sensors"][0]
        active_event = [e for e in events if e["t0"] <= a["t"] <= e["t1"] + GRACE and e["kind"] in affected.get(sid, set())]
        (events_called_faults if active_event else false_faults).append({"t": a["t"], "sensor": sid, "cls": a["cls"]})

    # events: detected if an event alert of the right class opened during it
    eres = []
    matched = set()
    for e in events:
        hit = [a for a in event_alerts if a["cls"] in MATCH.get(e["kind"], {e["kind"]}) and e["t0"] <= a["t"] <= e["t1"] + GRACE]
        if hit:
            a = min(hit, key=lambda a: a["t"])
            matched |= {id(x) for x in hit}
            ev = [s for s in a["sensors"]]
            eres.append({"event": e["id"], "kind": e["kind"], "detected": True, "ttd_s": round(a["t"] - e["t0"]),
                         "evidence_sensors": len(ev)})
        else:
            eres.append({"event": e["id"], "kind": e["kind"], "detected": False})
    other_events = []
    for a in event_alerts:
        if id(a) in matched:
            continue
        during = [e["id"] for e in events if e["t0"] <= a["t"] <= e["t1"] + GRACE]
        other_events.append({"t": a["t"], "cls": a["cls"], "sensors": len(a["sensors"]), "during": during})

    watch = []
    for a in (h for h in opens if h["kind"] == "anomaly"):
        sid = a["sensors"][0]
        if any(f["sensor"] == sid and f["t0"] - 60 <= a["t"] <= f["t1"] + GRACE for f in faults):
            what = "fault"
        elif any(e["t0"] <= a["t"] <= e["t1"] + GRACE and e["kind"] in affected.get(sid, set()) for e in events):
            what = "event"
        else:
            what = "nothing"
        watch.append({"t": a["t"], "sensor": sid, "really": what})
    det = [r for r in fres if r["detected"]]
    edet = [r for r in eres if r["detected"]]
    return {
        "label": label,
        "alerts_raised": len(opens),
        "faults": {"total": len(faults), "detected": len(det),
                   "median_ttd_s": _median([r["ttd_s"] for r in det]), "max_ttd_s": max([r["ttd_s"] for r in det], default=None),
                   "per_fault": fres},
        "false_fault_alerts": len(false_faults),
        "events_misreported_as_faults": len(events_called_faults),
        "false_fault_detail": false_faults[:30],
        "event_sensors_called_faults_detail": events_called_faults[:30],
        "events": {"total": len(events), "detected": len(edet),
                   "median_ttd_s": _median([r["ttd_s"] for r in edet]), "per_event": eres},
        "other_event_alerts": other_events,
        "watching_alerts": {"total": len(watch), "on_faulty_sensor": sum(w["really"] == "fault" for w in watch),
                            "on_event_sensor": sum(w["really"] == "event" for w in watch),
                            "on_healthy_sensor": sum(w["really"] == "nothing" for w in watch), "detail": watch},
    }


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
