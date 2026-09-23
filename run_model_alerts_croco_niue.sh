#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_FILE="/tmp/model_alerts_croco_niue.lock"
LOG_DIR="/data/model_alerts/logs"
LOG_FILE="${LOG_DIR}/model_alerts_croco_niue_cron.log"

mkdir -p "${LOG_DIR}"

cd "${SCRIPT_DIR}"

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Starting NIU_CURRENTS model alert run"
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Using ENABLED_CHECKS=NIU_CURRENTS"
  /usr/bin/flock -n "${LOCK_FILE}" /usr/bin/docker compose run --rm -e ENABLED_CHECKS="NIU_CURRENTS" email_sender
  EXIT_CODE=$?
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Finished NIU_CURRENTS model alert run with exit code ${EXIT_CODE}"
  exit "${EXIT_CODE}"
} >> "${LOG_FILE}" 2>&1
