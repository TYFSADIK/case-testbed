# Results

| Folder | What |
| --- | --- |
| `rehearsal-run1/` | first full rehearsal in Docker; every problem it found is in docs/04-findings.md. Its `dr.json` still contains the truth-logging artefact (ROS 2 robot error in two windows) found later |
| `rehearsal-final/` | the published numbers: phase A (exercise 1 at 20x, scoring, replay), phase B (real time: latency, viewer, hardware node, instructor inject), and the WAN matrix re-run on the same stack after the truth-logging fix |
| `offline/` | the same engine and detector in one process: exercise 1 on four seeds, held-out exercise 2 before and after the one rule added after seeing it |

`exercise-1/detection.json` has every fault and event with its time to
detect; `alerts-twin.json` is the twin's full alert history; `replay.json`
the after-action replay check; `dr.json` the dead-reckoning errors per
condition and entity; the PNGs are headless screenshots of the 3D viewer
(their on-screen latency readout includes software 3D rendering).
