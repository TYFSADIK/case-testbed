# 4. What the rehearsal found

Every finding below came from running the scenario, in one process
(`tools/offline.py`, for detector tuning) or distributed in Docker
(`local/rehearse.sh test`), not from reading documentation. Exercise 1 was
used for tuning; exercise 2 was written afterwards and held out.

## Detector

| When | What happened | Cause | Fix |
| --- | --- | --- | --- |
| tuning | a bridge gauge reading 13 standard deviations off for an hour scored as normal | IsolationForest saturates: a point far outside the training data, extreme in one feature only, is isolated no faster than the rarest normal sample | the forest flags its rarest 1%, robust thresholds confirm magnitude, and a very large single deviation counts on its own |
| tuning | unflooded gauges were called faulty during the flood | the flood lifted so many gauges that the "all gauges" reference rose with it | the reference excludes sensors already explained by an open event |
| tuning | gauges at the flood's edge were called faulty | compared with flooded neighbours, a barely-affected gauge looks low | sensors outside an event are compared only with comparable neighbours (at least two, within 1.5 km) |
| tuning | the sensor nearest the plume source was called faulty before the plume was confirmed | one sensor rising alone is genuinely ambiguous | a third state, **unexplained, watching**: a fault only if nothing explains it for 8 of 15 minutes |
| seeds 2-4 | the spoofed gauge (replaying pre-flood readings mid-flood) was folded into the flood | its "disagrees with neighbours" score outranked its abrupt jump, and it still read a little above dry gauges | fault signatures (jump, flat, noise) take precedence; a misbehaving sensor is never event evidence; faults stay open while the sensor still misbehaves |
| determinism | alerts opened in the same minute came out in a different order in another process | Python set iteration order depends on the process's hash seed | every set is sorted before iterating; checked identical across hash seeds |
| held out | a weather station drifting colder was hidden by a storm that also cools | it was absorbed as storm evidence | for area-wide events (storm), a member far from the other members is suspect; held-out result 9/10 faults before, 10/10 after |
| held out | the plume in exercise 2 was not detected | it blew east-south-east, away from the grid: its strongest effect was +7 ug/m3 at one sensor, within normal traffic variation | not fixed: the network could not see it. The twin should show sensor coverage; listed as next work |
| run 1 | a drift fault stayed open 34 minutes after the drift stopped | a sensor under a fault stays there while it still looks anomalous, and the flood kept disturbing its neighbours | open: closing faults faster near events trades against reopening them |

## Platform

| When | What happened | Cause | Fix |
| --- | --- | --- | --- |
| run 1 | no sensor reading reached the database or the viewer | the reading's own `type` field (the sensor type) overwrote the pipeline's `"type": "sensor"` tag | tag set last; regression test |
| run 1 | site B's PDUs never reached the twin | the gateway resolved the twin's name before the twin container existed, and the relay thread died | resolve with retry, keep relaying through errors, start after the twin |
| run 1 | the ROS 2 container restarted in a loop | `set -u` before sourcing ROS's setup script, which reads unset variables | strict mode after sourcing; the script now logs which ROS process exited |
| run 1 | a flood's footprint flagged 1,058 buildings; 119 were really flooded | the footprint was a circle around the evidence gauges | floods use the shoreline band (land within 200 m of the water) inside the circle: 320 buildings, all 119 flooded ones inside; outages use the failed substation's service area |
| run 1, final | the ROS 2 robot showed 3 to 5 m of error in exactly the two WAN conditions that come just before a federate restart | not the robot: the truth loggers buffered 16-64 KB (about 20 s), the restart killed the process and lost the buffer, and the analyser interpolated across the gap as if the robot had jumped | truth logs flush every line; the analyser refuses to score a moment with no truth sample within 0.2 s; the matrix counts federate restarts per window. Matrix re-run on the final stack |
| run 1 | the 3D viewer's latency tail was about 3 s | headless Chromium renders the 3D scene in software and stalls the page's main thread | server-path latency measured with a plain WebSocket client; the viewer figure is reported with this caveat |
| run 1 | the ground-truth overlay screenshot showed no overlay | the toggle was clicked before the first truth message (every 10 s) arrived | wait for it |
