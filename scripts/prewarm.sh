#!/usr/bin/env bash
# Pre-warm the Causal On-Call Cloud Run instance before a demo recording.
#
# Hits GET /warmup every 30 seconds for 5 minutes, then exits. The
# /warmup endpoint is lightweight by design (no LLM, no MCP, no Mongo)
# so the warmup itself never becomes the bottleneck.
#
# Usage:
#   ./scripts/prewarm.sh                         # defaults to live URL
#   ./scripts/prewarm.sh https://other.run.app   # override base URL
#
# Cancel anytime with Ctrl+C; partial warmup is still useful.
set -euo pipefail

BASE_URL="${1:-https://causal-oncall-856589756095.us-central1.run.app}"
TOTAL_SECONDS=300   # 5 minutes
INTERVAL_SECONDS=30

echo "Pre-warming ${BASE_URL}/warmup for ${TOTAL_SECONDS}s every ${INTERVAL_SECONDS}s."

ELAPSED=0
while [ "${ELAPSED}" -lt "${TOTAL_SECONDS}" ]; do
  STAMP="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  HTTP_CODE="$(curl -sS -o /tmp/causal_oncall_warmup_body -w '%{http_code}' \
    "${BASE_URL}/warmup" || echo '000')"
  BODY="$(cat /tmp/causal_oncall_warmup_body 2>/dev/null || echo '{}')"
  echo "[${STAMP}] http=${HTTP_CODE} body=${BODY}"
  sleep "${INTERVAL_SECONDS}"
  ELAPSED=$((ELAPSED + INTERVAL_SECONDS))
done

echo "Pre-warm complete. Container should be hot for the next recording window."
