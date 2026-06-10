#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="/data/model_alerts/model_alert"
LOCK_FILE="/tmp/model_alerts.lock"
LOG_DIR="/data/model_alerts/logs"
LOG_FILE="${LOG_DIR}/model_alerts_cron.log"
# Default checks for dataprod (override by exporting MODEL_ALERT_CHECKS)
MODEL_ALERT_CHECKS="${MODEL_ALERT_CHECKS:-NIU_CURRENTS}"

mkdir -p "${LOG_DIR}"

cd "${ROOT_DIR}"

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Starting model alert run"
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Using ENABLED_CHECKS=${MODEL_ALERT_CHECKS}"
  /usr/bin/flock -n "${LOCK_FILE}" env ENABLED_CHECKS="${MODEL_ALERT_CHECKS}" /usr/bin/docker compose up --no-build --force-recreate --abort-on-container-exit --exit-code-from email_sender
  EXIT_CODE=$?
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Finished model alert run with exit code ${EXIT_CODE}"
  exit "${EXIT_CODE}"
} >> "${LOG_FILE}" 2>&1
