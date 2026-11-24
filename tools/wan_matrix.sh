#!/usr/bin/env bash
# WAN matrix: impair the site B gateway's WAN link with tc/netem, log the twin's
# dead-reckoning estimates for 60 s per condition, and count PDUs at the gateway.
#   tools/wan_matrix.sh <results dir> [conditions...]
# A condition is label:delay_ms:loss_pct:threshold_m. Run from stacks/.
set -euo pipefail
OUT=$1; shift
API=http://127.0.0.1:8000
DEV=$(docker compose exec -T gateway sh -c 'ip route get "$(getent hosts backend | cut -d" " -f1)" | grep -o "dev [^ ]*" | cut -d" " -f2')
current_thr=""
plan="["
for cond in "$@"; do
  IFS=: read -r label delay loss thr <<< "$cond"
  if [ "$thr" != "$current_thr" ]; then
    DR_THRESHOLD=$thr docker compose up -d siteb ros2 >/dev/null 2>&1
    current_thr=$thr
    sleep 20
  fi
  if [ "$delay" = 0 ] && [ "$loss" = 0 ]; then
    docker compose exec -T gateway tc qdisc del dev "$DEV" root 2>/dev/null || true
  else
    docker compose exec -T gateway tc qdisc replace dev "$DEV" root netem delay "${delay}ms" "$((delay / 10))ms" loss "${loss}%"
  fi
  sleep 5
  r0=$(docker inspect case-ros2-1 case-siteb-1 --format '{{.RestartCount}}' | paste -sd+ - | bc)
  a=$(docker compose exec -T gateway curl -fsS localhost:8080)
  curl -fsS -X POST "$API/api/dr" -H 'Content-Type: application/json' -d "{\"label\":\"$label\"}" >/dev/null
  sleep 60
  curl -fsS -X POST "$API/api/dr" -H 'Content-Type: application/json' -d '{"label":null}' >/dev/null
  b=$(docker compose exec -T gateway curl -fsS localhost:8080)
  r1=$(docker inspect case-ros2-1 case-siteb-1 --format '{{.RestartCount}}' | paste -sd+ - | bc)
  rate=$(python3 -c "
import json,sys
a,b=json.loads(sys.argv[1]),json.loads(sys.argv[2]); dt=b['now']-a['now']
print(json.dumps({k: round((b['pdus'].get(k,0)-a['pdus'].get(k,0))/dt,2) for k in b['pdus']}))" "$a" "$b")
  plan="$plan{\"label\":\"$label\",\"delay_ms\":$delay,\"loss_pct\":$loss,\"threshold_m\":$thr,\"pdu_rate\":$rate,\"federate_restarts\":$((r1 - r0))},"
  echo "  $label done (federate restarts during window: $((r1 - r0)))"
done
docker compose exec -T gateway tc qdisc del dev "$DEV" root 2>/dev/null || true
echo "${plan%,}]" > "$OUT/dr-plan.json"
docker compose cp "$OUT/dr-plan.json" tester:/tmp/dr-plan.json >/dev/null
docker compose --profile test exec -T tester python tools/dr_analyze.py /tmp/dr-plan.json "/results/$(basename "$OUT")/dr.json"
