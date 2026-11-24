#!/usr/bin/env bash
# Rehearse the CASE testbed on one machine with Docker.
#
#   ./local/rehearse.sh up      build and start (site A at SPEED x real time)
#   ./local/rehearse.sh test    the full rehearsal; results in results/$RUN
#   ./local/rehearse.sh down    stop and delete everything
#
# test runs two phases:
#   A  the whole four-hour exercise at 20x: detection scored against ground
#      truth, the recording's hash chain verified and replayed
#   B  the same exercise run to 07:55 at 20x, then real time: screenshots,
#      latency, a live instructor inject, a hardware sensor node joining, and
#      the WAN matrix (latency and loss on the DIS gateway, dead-reckoning error)
set -euo pipefail
cd "$(dirname "$0")/../stacks"
RUN=${RUN:-rehearsal}
OUT=../results/$RUN
PW=${PLAYWRIGHT:-}          # path to a playwright-core install, for the viewer checks (optional)
API=http://127.0.0.1:8000
mkdir -p "$OUT"

stats() { curl -fsS "$API/api/stats"; }
sim_t() { stats | python3 -c 'import json,sys; print(int(json.load(sys.stdin)["sim_t"]))'; }
wait_sim() {   # wait until simulated time reaches $1
  for _ in $(seq 1 2400); do
    if [ "$(sim_t 2>/dev/null || echo 0)" -ge "$1" ]; then return 0; fi
    sleep 1
  done
  echo "timed out waiting for sim time $1" >&2; return 1
}
fresh() {   # fresh stack with the given environment
  docker compose --profile test down -v --remove-orphans >/dev/null 2>&1 || true
  env "$@" docker compose --profile test up -d --build >/dev/null
  for _ in $(seq 1 120); do curl -fsS "$API/api/health" >/dev/null 2>&1 && break; sleep 1; done
}
tester() { docker compose --profile test exec -T tester "$@"; }

case "${1:-}" in
  up)
    SPEED=${SPEED:-20} docker compose up -d --build
    echo "twin: $API   (sim at ${SPEED:-20}x)" ;;
  down)
    docker compose --profile test down -v --remove-orphans ;;
  test)
    echo "== phase A: exercise 1 at 20x"
    fresh SPEED=20
    wait_sim 14400
    sleep 15
    tester python tools/score_run.py "/results/$RUN/exercise-1" | tee "$OUT/exercise-1.txt"
    REC=$(tester sh -c 'ls -1 /data/twin/rec/*.jsonl | head -1')
    tester python tools/replay.py "$REC" "/results/$RUN/replay.json"
    stats > "$OUT/backend-phase-a.json"
    docker compose exec -T gateway curl -fsS localhost:8080 > "$OUT/gateway-phase-a.json"
    docker compose exec -T postgis psql -U twin -tA -F, -c \
      "select (select count(*) from telemetry), (select count(*) from alert_log), (select count(*) from alert_log where impacts is not null)" \
      > "$OUT/db-phase-a.csv"
    docker compose exec -T postgis psql -U twin -tA -c \
      "select json_agg(x) from (select alert_id, cls, t, impacts from alert_log where kind='event' and op='open' and detector='twin' order by t) x" \
      > "$OUT/event-impacts.json"
    tester sh -c 'tail -1 /data/truth/truth.jsonl' > "$OUT/sim-end.json"

    echo "== phase B: real time from 07:55"
    fresh SPEED=20 SLOW_AT=6900
    wait_sim 6905
    sleep 20
    tester python tools/ws_latency.py 60 | tee "$OUT/ws-latency.json"
    if [ -n "$PW" ]; then
      node ../tools/viewer_check.cjs "$PW" "$OUT" 20 twin-flood | tee "$OUT/viewer.json"
      node ../tools/viewer_check.cjs "$PW" "$OUT" 5 twin-flood-truth >/dev/null
    fi
    tester python tools/hw_node_sim.py mosquitto 40 | tee "$OUT/hw-node.json"
    curl -fsS "$API/api/sensors" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({"registered": any(r["id"] == "hw-node-01" for r in d["registry"]), "latest": d["latest"].get("hw-node-01")}))' \
      | tee "$OUT/hw-node-twin.json"
    docker compose exec -T postgis psql -U twin -tA -F, -c \
      "select s.hardware, count(t.*) from sensor s join telemetry t on t.sensor_id=s.id where s.id='hw-node-01' group by 1" \
      | tee "$OUT/hw-node-db.csv"
    curl -fsS "$API/api/near?lat=44.38905&lon=-79.6865&r=400" > "$OUT/near-hw-node.json"

    echo "== instructor inject: plume at the police station"
    before=$(curl -fsS "$API/api/alerts" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["history"]))')
    t_inj=$(date +%s)
    curl -fsS -X POST "$API/api/inject" -H 'Content-Type: application/json' \
      -d '{"op":"event","kind":"plume","lat":44.38804,"lon":-79.69088,"duration":1800,"params":{"q":9000000,"stack":10,"rise":300,"fall":300}}' >/dev/null
    found=""
    for _ in $(seq 1 120); do
      found=$(curl -fsS "$API/api/alerts" | python3 -c "
import json,sys
h=json.load(sys.stdin)['history'][$before:]
p=[a for a in h if a['op']=='open' and a['kind']=='event' and a['cls']=='plume']
print(p[0]['id'] if p else '')")
      [ -n "$found" ] && break
      sleep 5
    done
    echo "{\"inject\":\"plume\",\"alert\":\"$found\",\"seconds\":$(( $(date +%s) - t_inj ))}" | tee "$OUT/inject.json"
    [ -n "$PW" ] && node ../tools/viewer_check.cjs "$PW" "$OUT" 5 twin-plume >/dev/null

    echo "== WAN matrix on the site B gateway"
    ../tools/wan_matrix.sh "$OUT" lan:0:0:1.0 wan-50ms:50:0:1.0 wan-150ms:150:0:1.0 wan-300ms:300:0:1.0 \
      wan-150ms-loss2:150:2:1.0 wan-150ms-thr0.25:150:0:0.25 wan-150ms-thr3.0:150:0:3.0
    stats > "$OUT/backend-phase-b.json"
    echo "results: $OUT" ;;
  *)
    echo "usage: $0 up|test|down" >&2; exit 2 ;;
esac
