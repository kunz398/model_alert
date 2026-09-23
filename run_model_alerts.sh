#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_FILE="/tmp/model_alerts.lock"
LOG_DIR="/data/model_alerts/logs"
LOG_FILE="${LOG_DIR}/model_alerts_cron.log"

mkdir -p "${LOG_DIR}"

cd "${SCRIPT_DIR}"

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Starting model alert run"
  /usr/bin/flock -n "${LOCK_FILE}" /usr/bin/docker compose run --rm email_sender
  EXIT_CODE=$?
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Finished model alert run with exit code ${EXIT_CODE}"
  exit "${EXIT_CODE}"
} >> "${LOG_FILE}" 2>&1

