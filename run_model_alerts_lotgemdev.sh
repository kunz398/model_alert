#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_FILE="/tmp/model_alerts.lock"
LOG_DIR="/mnt/DATA/model_alerts/logs"
LOG_FILE="${LOG_DIR}/model_alerts_cron.log"
# Default checks for lotgemdev (override by exporting MODEL_ALERT_CHECKS)
# Example override: MODEL_ALERT_CHECKS="NIU" ./run_model_alerts_lotgemdev.sh
MODEL_ALERT_CHECKS="${MODEL_ALERT_CHECKS:-NIU,COK}"

mkdir -p "${LOG_DIR}"

cd "${SCRIPT_DIR}"

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Starting model alert run"
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Using ENABLED_CHECKS=${MODEL_ALERT_CHECKS}"
  /usr/bin/flock -n "${LOCK_FILE}" /usr/bin/docker compose run --rm -e ENABLED_CHECKS="${MODEL_ALERT_CHECKS}" email_sender
  EXIT_CODE=$?
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Finished model alert run with exit code ${EXIT_CODE}"
  exit "${EXIT_CODE}"
} >> "${LOG_FILE}" 2>&1
