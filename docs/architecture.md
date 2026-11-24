# Architecture reference

```
 SITE A (exercise network)                                 SITE B (second training site)
 ┌──────────────────────────────────────────┐              ┌──────────────────────────────────┐
 │ sim: OSM world, 100 vehicles, 95 sensors │              │ siteb: survey UAV, 3-vehicle     │
 │      storm / flood / plume / outage,     │              │        emergency convoy          │
 │      sensor faults, ground-truth log     │              │ ros2:  patrol robot + DIS bridge │
 │   │ MQTT telemetry      │ DIS ESPDUs     │              │   │ DIS ESPDUs (UDP)             │
 │   ▼                     ▼                │     WAN      │   ▼                              │
 │ mosquitto ──► backend ◄─────────────────────(netem)────── gateway (counts PDUs)           │
 │               │  record (hash chain) → pipeline (one ordered stream)                       │
 │               │  detector: IsolationForest + spatial reasoning → events / faults           │
 │               │  PostGIS: world, telemetry, alerts, footprint ∩ assets/roads               │
 │               ▼                                                                             │
 │            WebSocket + REST → CesiumJS 3D viewer (instructor: injects, ground truth)       │
 └──────────────────────────────────────────┘              └──────────────────────────────────┘
          ▲ MQTT (same topics)
   ESP32 field node (BME280, PMS5003, JSN-SR04T)
```

## Messages

| Topic / port | Payload | From |
| --- | --- | --- |
| `case/registry/<id>` (retained) | `{id, type, unit, latlon, period, power, substation, hardware}` | sim, hardware nodes |
| `case/sensor/<id>` | `{id, type, t, v, unit, ts, ...extra}`: `t` simulated seconds, `ts` wall clock at publish | sim, hardware nodes |
| `case/clock` | `{t, speed}` every simulated second | sim |
| `case/control` | instructor inject `{op: event|fault, kind, lat, lon, sensor, duration, params}` | backend |
| `case/truth/state` | ground truth for the instructor overlay; never recorded or seen by the detector | sim |
| UDP 3000 | DIS v7 Entity State PDU, 144 bytes, exercise 7; site 1 = site A traffic, 2 = site B, 3 = ROS 2 | sim, gateway |

## Dead reckoning

Every federate runs the receivers' model (DRM 2, FPW: position plus velocity
times elapsed time) and sends a PDU only when the model would be more than
`DR_THRESHOLD` metres off, or every 5 s (heartbeat). The twin extrapolates
each entity to "now" between PDUs. Site A's PDUs carry simulated time; site B
and ROS 2 carry wall-clock absolute DIS timestamps.

## Detector

Per sensor, per simulated minute: `z_local` (vs the nearest same-type
sensors that are comparable), `z_global` (vs the type's reference, excluding
sensors already explained by an open event), `z_trend` (vs its own last hour),
and flat / spike / noise ratios against its own warm-up behaviour. An
IsolationForest per sensor type (trained on the first 30 minutes) flags
unusual feature combinations; robust thresholds confirm magnitude, and a very
large single deviation counts on its own (the forest saturates there).

Then:

1. A substation whose load collapses, or whose mains sensors fall silent
   together, is an **outage**; those silent sensors are explained.
2. Anomalous sensors of one type that are close, deviate the same way and
   agree with each other form an **event** (flood, plume, storm); sensors on
   its edge deviating the same way join it as evidence. For area-wide events
   (storm), a member far from the others is suspect instead.
3. A sensor with a fault signature (flat, spike, noise), one that goes silent
   with no outage to explain it, or one that stays unexplained for 8 of 15
   minutes, is a **sensor fault**. Until then it is **unexplained, watching**.
   A sensor under an open fault is never evidence for an event.

Each event footprint is buffered in PostGIS and intersected with buildings,
roads and civic assets ("what does this flood threaten?").

## After-action review

Every input is written to `/data/rec/session-*.jsonl` before it is processed,
with a SHA-256 chain. `tools/replay.py` verifies the chain and replays the
inputs through a fresh pipeline; the alerts must match the live run exactly.
Nothing in the pipeline reads the wall clock, and set iteration is sorted, so
this holds across processes.
