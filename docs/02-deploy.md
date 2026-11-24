# 2. Deploy

## On one machine (what the rehearsal does)

```bash
./local/rehearse.sh up            # site A at 20x real time; open http://127.0.0.1:8000
SPEED=1 ./local/rehearse.sh up    # real time
./local/rehearse.sh test          # the full rehearsal, results in results/rehearsal
./local/rehearse.sh down
```

Needs Docker with 6 CPUs and 8 GB for the VM (Colima on a Mac:
`colima start --cpu 6 --memory 8`). The first build pulls about 2 GB
(PostGIS, ROS 2 Jazzy, Python).

Settings (environment variables for `docker compose`):

| Variable | Default | |
| --- | --- | --- |
| `SPEED` | 1 | site A simulation speed (x real time) |
| `SLOW_AT` | inf | switch to real time at this simulated second |
| `SCENARIO` | `scenarios/exercise-1.yaml` | the master scenario events list |
| `SEED` | 1 | traffic and noise seed |
| `VEHICLES` | 100 | site A traffic |
| `DR_THRESHOLD` | 1.0 | metres of dead-reckoning error before site B sends a PDU |

## Two sites

See [1.4](01-physical.md#14-two-sites-on-two-machines). Site B needs UDP to
site A port 3000; nothing else crosses the WAN.

## Proxmox

Two VMs (or LXC containers with nesting), one per site, on separate bridges
with a router VM between them is the closest to a real distributed exercise:
apply netem on the router's interfaces instead of the gateway container.

## Writing a scenario

A scenario is a YAML master scenario events list:

```yaml
duration: 14400                 # seconds of simulated time
wind_toward_deg: 15             # where a plume goes
events:
  - {id: E2-flood, kind: flood, t0: 5400, t1: 9000, at: {sensor: wl-10},
     params: {radius: 650, peak: 0.75, rise: 1200, fall: 1500}}
faults:
  - {id: F07, sensor: wl-04, kind: replay, t0: 6600, t1: 9000, params: {lag: 3000}}
```

Event kinds: `storm`, `flood` (`at`, `radius`, `peak` metres), `plume` (`at`,
`q` source strength, `stack` height), `outage` (`substation`). Fault kinds:
`stuck`, `drift` (`rate_per_h`), `offset` (`delta`), `spike` (`size`, `prob`),
`noise` (`sigma`), `dropout`, `replay` (`lag` seconds). `at` takes a sensor
id, an asset id or `lat`/`lon`. Keep the first 30 minutes quiet: the twin
learns normal behaviour then.

Run it in seconds without Docker to see how the twin would do:

```bash
python tools/offline.py --scenario scenarios/my-exercise.yaml
```
