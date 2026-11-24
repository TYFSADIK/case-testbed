# 3. Before anything real

This is a lab. Things that are deliberately simple here and must change
before it carries anything that matters:

| Area | Here | For real |
| --- | --- | --- |
| MQTT | anonymous, plain TCP, bound to localhost | per-device credentials or client certificates, TLS on 8883, ACLs so a sensor can only publish its own topic |
| DIS | plain UDP, no authentication (the standard has none) | keep exercise traffic on an isolated network or VPN between sites; DIS has no integrity, so a gateway should filter by site and exercise IDs |
| HLA | not used | large federations often use HLA (IEEE 1516) with a run-time infrastructure; the DIS gateway is where an HLA bridge would sit |
| Time | containers share one clock | dead reckoning from PDU timestamps needs synchronised clocks across sites: NTP at minimum, PTP for tight coupling (see the WAN results in the README) |
| PostGIS | default password in compose | secrets, TLS, a read-only role for the viewer API, backups |
| Viewer | no login | put it behind the identity provider (see my ICAM testbed) |
| Recording | hash chain on local disk | ship the chain head to a separate system regularly so a rewritten file is detectable |
| Ground truth | published on MQTT for the instructor overlay | separate instructor network or role; trainees must not see it |
| Detector | thresholds tuned on exercise 1 | tune per site, keep a held-out exercise, track false alarms with operators |
| Map data | OpenStreetMap extract (ODbL) | keep the attribution; real sites need surveyed data and terrain |
