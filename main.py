from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

from send_email import send_email
from check_zarr import get_zarr_model_run_time

ALERT_SUBJECT = "Model Alert - NIU / COK"
# Default recipients (used when TO_EMAILS env var is not set)
TO_EMAILS = ["kunalsi@spc.int"]

# Choose default checks to run. Valid values: "NIU", "COK", "NIU_CURRENTS"
# Runtime override: set env var ENABLED_CHECKS (comma-separated), e.g. ENABLED_CHECKS="NIU"
ENABLED_CHECKS = ["NIU", "COK"]

ATTACHMENT_PATHS: list[Path] = []

NIU_LOG_DIR = Path(os.environ.get("NIU_LOG_DIR", "niue_logs"))
LOG_FILE_RE = re.compile(r"^forecast_pipeline_(\d{8})_(\d{6})\.log$")

THREDDS_CATALOG_URL = os.environ.get(
    "NIU_THREDDS_CATALOG_URL",
    "https://gemthreddshpc.spc.int/thredds/catalog/POP/model/country/spc/forecast/hourly/NIU/catalog.xml",
)
THREDDS_FILE_NAME = os.environ.get("NIU_THREDDS_FILE_NAME", "ForecastNiue_latest.nc")
THREDDS_NS = {"t": "http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0"}

# ── Shared failure patterns ──────────────────────────────────────────────────
FAIL_PATTERNS = [
    re.compile(r"traceback", re.IGNORECASE),
    re.compile(r"docker forecast model failed", re.IGNORECASE),
    re.compile(r"requests\.exceptions\.[A-Za-z]+", re.IGNORECASE),
    re.compile(r"docker:\s+error response from daemon", re.IGNORECASE),
    re.compile(r"nvidia-container-cli:\s+initialization error", re.IGNORECASE),
]

# ── NIU ──────────────────────────────────────────────────────────────────────
NIU_SUCCESS_PATTERNS = [
    re.compile(r"niue forecast pipeline completed", re.IGNORECASE),
    re.compile(r"results transferred to thredds server", re.IGNORECASE),
    re.compile(r"\[complete\]\s*pipeline complete\b", re.IGNORECASE),
]
NIU_FAIL_PATTERNS = [
    re.compile(r"\bERROR\s+\[", re.IGNORECASE),
]

# ── COK ──────────────────────────────────────────────────────────────────────
COK_LOG_DIR = Path(os.environ.get("COKS_LOG_DIR", "coks_logs"))
COK_THREDDS_POINTS_URL = os.environ.get(
    "COK_THREDDS_POINTS_URL",
    "https://gemthreddshpc.spc.int/thredds/fileServer/POP/model/country/spc/forecast/hourly/COK/risk/points.json",
)
COK_SUCCESS_PATTERNS = [
    re.compile(r"cook islands forecast pipeline completed", re.IGNORECASE),
    re.compile(r"cook islands forecast pipeline started", re.IGNORECASE),
    re.compile(r"tidal predictions saved for\s+\d+\s+points", re.IGNORECASE),
    re.compile(r"writing spectral file", re.IGNORECASE),
]
COK_TRANSFER_OK_RE = re.compile(r"Transfer OK\s*:\s*(true|false)", re.IGNORECASE)
COK_WARNING_RE = re.compile(r"WARNING:.*", re.IGNORECASE)

# ── NIU Currents (CROCO) ────────────────────────────────────────────────────
CROCO_NIU_LOG_DIR = Path(
    os.environ.get("CROCO_NIU_LOG_DIR", "croco_niue")
)
CROCO_LOG_FILE_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})_run_forecast\.log$")
CROCO_ZARR_DATASET = os.environ.get("CROCO_ZARR_DATASET", "d1_temp_salt_uv_z_all.zarr")
CROCO_SUCCESS_PATTERNS = [
    re.compile(r"croco forecast complete", re.IGNORECASE),
    re.compile(r"\bok\s+transfer complete\b", re.IGNORECASE),
]
CROCO_FAIL_PATTERNS = [
    re.compile(r"no space left on device", re.IGNORECASE),
    re.compile(r"netcdf:\s*hdf error", re.IGNORECASE),
    re.compile(r"gfs processing failed", re.IGNORECASE),
    re.compile(r"preprocessing failed", re.IGNORECASE),
    re.compile(r"docker container failed", re.IGNORECASE),
]


@dataclass
class CheckResult:
    errors: list[str]
    notes: list[str]


def _get_enabled_checks() -> list[str]:
    env_checks = os.environ.get("ENABLED_CHECKS", "").strip()
    if env_checks:
        return [name.strip().upper() for name in env_checks.split(",") if name.strip()]

    return [name.strip().upper() for name in ENABLED_CHECKS if name.strip()]


def _get_recipients() -> list[str]:
    env_recipients = os.environ.get("TO_EMAILS", "").strip()
    if env_recipients:
        return [email.strip() for email in env_recipients.split(",") if email.strip()]

    return [email.strip() for email in TO_EMAILS if email.strip()]


def _find_latest_log_for_today(log_dir: Path, local_today: str) -> Path | None:
    latest_match: tuple[str, Path] | None = None

    if not log_dir.exists() or not log_dir.is_dir():
        return None

    for candidate in log_dir.iterdir():
        if not candidate.is_file():
            continue
        matched = LOG_FILE_RE.match(candidate.name)
        if not matched:
            continue

        date_part, run_part = matched.groups()
        if date_part != local_today:
            continue

        if latest_match is None or run_part > latest_match[0]:
            latest_match = (run_part, candidate)

    return latest_match[1] if latest_match else None


def _extract_failure_lines(log_text: str, max_items: int = 8) -> list[str]:
    findings: list[str] = []

    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if any(pattern.search(line) for pattern in FAIL_PATTERNS):
            findings.append(line)
            if len(findings) >= max_items:
                break

    return findings


def _extract_failure_lines_with_patterns(
    log_text: str,
    patterns: list[re.Pattern[str]],
    max_items: int = 8,
) -> list[str]:
    findings: list[str] = []

    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if any(pattern.search(line) for pattern in patterns):
            findings.append(line)
            if len(findings) >= max_items:
                break

    return findings


def _find_croco_log_for_today(log_dir: Path, local_today: datetime) -> Path | None:
    latest_file: Path | None = None
    latest_mtime: float | None = None

    if not log_dir.exists() or not log_dir.is_dir():
        return None

    expected_name = local_today.strftime("%d-%m-%Y")
    for candidate in log_dir.iterdir():
        if not candidate.is_file():
            continue

        matched = CROCO_LOG_FILE_RE.match(candidate.name)
        if not matched:
            continue

        day, month, year = matched.groups()
        name_date = f"{day}-{month}-{year}"
        if name_date != expected_name:
            continue

        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue

        if latest_mtime is None or mtime > latest_mtime:
            latest_mtime = mtime
            latest_file = candidate

    return latest_file


def _check_thredds_has_today_utc() -> tuple[bool, str]:
    today_utc = datetime.now(UTC).date()
    yesterday_utc = today_utc - timedelta(days=1)

    try:
        response = requests.get(THREDDS_CATALOG_URL, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        return False, f"Could not reach THREDDS catalog: {exc}"

    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as exc:
        return (
            False,
            (
                f"THREDDS catalog response could not be parsed as XML: {exc}"
            ),
        )

    dataset = root.find(f".//t:dataset[@name='{THREDDS_FILE_NAME}']", THREDDS_NS)
    if dataset is None:
        return False, f"{THREDDS_FILE_NAME} not found in THREDDS catalog"

    modified_node = dataset.find("t:date[@type='modified']", THREDDS_NS)
    if modified_node is None or not modified_node.text:
        return False, f"No modified date found for {THREDDS_FILE_NAME}"

    modified_raw = modified_node.text.strip()
    try:
        modified_dt = datetime.fromisoformat(modified_raw.replace("Z", "+00:00"))
    except ValueError:
        return (
            False,
            f"Could not parse modified date for {THREDDS_FILE_NAME}: {modified_raw}",
        )

    modified_utc_date = modified_dt.astimezone(UTC).date()
    if modified_utc_date < yesterday_utc:
        return (
            False,
            (
                f"{THREDDS_FILE_NAME} exists but modified UTC date is "
                f"{modified_utc_date} (expected {yesterday_utc} or {today_utc})"
            ),
        )

    return True, f"THREDDS file present (modified UTC: {modified_utc_date})"


def _build_combined_alert_body(results: dict[str, CheckResult]) -> str:
    sections = ""
    for model, result in results.items():
        if result.errors:
            error_list = "".join(f"<li>{item}</li>" for item in result.errors)
            note_list = "".join(f"<li>{item}</li>" for item in result.notes)
            sections += (
                f"<h3>{model}</h3>"
                f"<h4>Errors</h4><ul>{error_list}</ul>"
                f"<h4>Context</h4><ul>{note_list}</ul>"
            )
    return "<h2>Model Alert</h2><p>One or more checks failed.</p>" + sections


def _check_cok_thredds_has_today_utc() -> tuple[bool, str]:
    today_utc = datetime.now(UTC).date()
    yesterday_utc = today_utc - timedelta(days=1)

    try:
        response = requests.get(COK_THREDDS_POINTS_URL, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        return False, f"Could not reach COK points.json: {exc}"

    try:
        data = response.json()
    except ValueError as exc:
        return False, f"Could not parse COK points.json as JSON: {exc}"

    model_run = data.get("metadata", {}).get("model_run", "")
    if not model_run:
        return False, "No model_run field found in COK points.json metadata"

    try:
        model_run_dt = datetime.fromisoformat(model_run.replace("Z", "+00:00"))
    except ValueError:
        return False, f"Could not parse model_run date in COK points.json: {model_run}"

    model_run_date = model_run_dt.astimezone(UTC).date()
    if model_run_date < yesterday_utc:
        return (
            False,
            f"COK points.json model_run date is {model_run_date} (expected {yesterday_utc} or {today_utc})",
        )

    return True, f"COK THREDDS points.json model_run date is {model_run_date}"


def _check_croco_zarr_has_today_utc() -> tuple[bool, str]:
    today_utc = datetime.now(UTC).date()
    yesterday_utc = today_utc - timedelta(days=1)

    try:
        run_time = get_zarr_model_run_time(CROCO_ZARR_DATASET)
    except Exception as exc:
        return False, f"Could not read {CROCO_ZARR_DATASET} zarr time attrs: {exc}"

    run_date = run_time.astimezone(UTC).date()
    if run_date < yesterday_utc:
        return (
            False,
            (
                f"{CROCO_ZARR_DATASET} model run UTC date is "
                f"{run_date} (expected {yesterday_utc} or {today_utc})"
            ),
        )

    return True, f"NIU_Currents zarr model run date is {run_date}"


def CheckNiu() -> CheckResult:
    errors: list[str] = []
    notes: list[str] = []

    local_today = datetime.now().strftime("%Y%m%d")
    log_dir = NIU_LOG_DIR
    notes.append(f"Log directory: {log_dir}")

    is_running = False
    latest_log = _find_latest_log_for_today(log_dir=log_dir, local_today=local_today)
    if latest_log is None:
        errors.append(
            f"NIUE log not created for today ({local_today}) in {log_dir}"
        )
    else:
        print(f"Found latest NIUE log for today: {latest_log}")
        notes.append(f"Latest NIUE log: {latest_log.name}")

        try:
            log_text = latest_log.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append(f"Failed to read NIUE log {latest_log}: {exc}")
            log_text = ""

        if log_text:
            fail_lines = _extract_failure_lines_with_patterns(
                log_text=log_text,
                patterns=FAIL_PATTERNS + NIU_FAIL_PATTERNS,
            )
            has_success_marker = any(
                pattern.search(log_text) for pattern in NIU_SUCCESS_PATTERNS
            )

            if has_success_marker:
                print("Model ran successfully")
                notes.append("Model log indicates successful completion")
            elif fail_lines:
                errors.append(
                    "Model run failure indicators found in NIUE log: "
                    + " | ".join(fail_lines)
                )
            else:
                age_seconds = datetime.now().timestamp() - latest_log.stat().st_mtime
                if age_seconds < 7200:
                    is_running = True
                    print("NIU log recently modified, assuming script is still running.")
                    notes.append(f"Script is likely still running (log updated {age_seconds/60:.1f} mins ago).")
                else:
                    errors.append(
                        "NIUE log found but no clear success marker was detected and log is stale."
                    )

    thredds_ok, thredds_message = _check_thredds_has_today_utc()
    if thredds_ok:
        print("THREDDS file there")
        notes.append(thredds_message)
    else:
        if is_running:
            notes.append(f"THREDDS check skipped because script is still running: {thredds_message}")
        else:
            errors.append(f"No THREDDS file found or date mismatch: {thredds_message}")

    return CheckResult(errors=errors, notes=notes)


def CheckCok() -> CheckResult:
    errors: list[str] = []
    notes: list[str] = []

    local_today = datetime.now().strftime("%Y%m%d")
    log_dir = COK_LOG_DIR
    notes.append(f"Log directory: {log_dir}")

    is_running = False
    latest_log = _find_latest_log_for_today(log_dir=log_dir, local_today=local_today)
    if latest_log is None:
        errors.append(
            f"COK log not created for today ({local_today}) in {log_dir}"
        )
    else:
        print(f"Found latest COK log for today: {latest_log}")
        notes.append(f"Latest COK log: {latest_log.name}")

        try:
            log_text = latest_log.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append(f"Failed to read COK log {latest_log}: {exc}")
            log_text = ""

        if log_text:
            fail_lines = _extract_failure_lines(log_text)
            has_success_marker = any(
                pattern.search(log_text) for pattern in COK_SUCCESS_PATTERNS
            )
            transfer_match = COK_TRANSFER_OK_RE.search(log_text)

            if transfer_match:
                if transfer_match.group(1).lower() == "true":
                    print("COK model ran successfully")
                    notes.append("COK pipeline summary reports Transfer OK: true")
                else:
                    warning_lines = COK_WARNING_RE.findall(log_text)[:8]
                    detail = " | ".join(warning_lines) if warning_lines else "see log for details"
                    errors.append(f"COK pipeline reported Transfer OK: false ({detail})")
            elif fail_lines:
                errors.append(
                    "Model run failure indicators found in COK log: "
                    + " | ".join(fail_lines)
                )
            elif has_success_marker:
                print("COK model ran successfully")
                notes.append("COK model log indicates successful completion")
            else:
                age_seconds = datetime.now().timestamp() - latest_log.stat().st_mtime
                if age_seconds < 7200:
                    is_running = True
                    print("COK log recently modified, assuming script is still running.")
                    notes.append(f"Script is likely still running (log updated {age_seconds/60:.1f} mins ago).")
                else:
                    notes.append(
                        "COK log has no explicit success marker, but no failure indicators were found"
                    )

    thredds_ok, thredds_message = _check_cok_thredds_has_today_utc()
    if thredds_ok:
        print("COK THREDDS points.json is current")
        notes.append(thredds_message)
    else:
        if is_running:
            notes.append(f"THREDDS check skipped because script is still running: {thredds_message}")
        else:
            errors.append(f"COK THREDDS check failed: {thredds_message}")

    return CheckResult(errors=errors, notes=notes)


def CheckCrocoNiu() -> CheckResult:
    errors: list[str] = []
    notes: list[str] = []

    local_now = datetime.now()
    log_dir = CROCO_NIU_LOG_DIR
    notes.append(f"Log directory: {log_dir}")

    is_running = False
    latest_log = _find_croco_log_for_today(log_dir=log_dir, local_today=local_now)
    if latest_log is None:
        expected_name = local_now.strftime("%d-%m-%Y")
        errors.append(
            f"NIU_Currents log not created for today ({expected_name}) in {log_dir}"
        )
    else:
        print(f"Found latest NIU_Currents log for today: {latest_log}")
        notes.append(f"Latest NIU_Currents log: {latest_log.name}")

        try:
            log_text = latest_log.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append(f"Failed to read NIU_Currents log {latest_log}: {exc}")
            log_text = ""

        if log_text:
            croco_patterns = FAIL_PATTERNS + CROCO_FAIL_PATTERNS
            fail_lines = _extract_failure_lines_with_patterns(
                log_text=log_text,
                patterns=croco_patterns,
            )
            has_success_marker = any(
                pattern.search(log_text) for pattern in CROCO_SUCCESS_PATTERNS
            )

            if has_success_marker and not fail_lines:
                print("NIU_Currents model ran successfully")
                notes.append("NIU_Currents log indicates successful completion")
            elif fail_lines:
                errors.append(
                    "Failure indicators found in NIU_Currents log: "
                    + " | ".join(fail_lines)
                )
            else:
                age_seconds = datetime.now().timestamp() - latest_log.stat().st_mtime
                if age_seconds < 7200:
                    is_running = True
                    print("NIU_Currents log recently modified, assuming script is still running.")
                    notes.append(f"Script is likely still running (log updated {age_seconds/60:.1f} mins ago).")
                else:
                    errors.append(
                        "NIU_Currents log found but no clear success marker was detected and log is stale."
                    )

    zarr_ok, zarr_message = _check_croco_zarr_has_today_utc()
    if zarr_ok:
        print("NIU_Currents zarr file is current")
        notes.append(zarr_message)
    else:
        if is_running:
            notes.append(f"Zarr check skipped because script is still running: {zarr_message}")
        else:
            errors.append(f"NIU_Currents zarr check failed: {zarr_message}")

    return CheckResult(errors=errors, notes=notes)


def main():
    recipients = _get_recipients()
    if not recipients:
        raise ValueError("TO_EMAILS cannot be empty")

    available_checks: dict[str, Callable[[], CheckResult]] = {
        "NIU": CheckNiu,
        "COK": CheckCok,
        "NIU_CURRENTS": CheckCrocoNiu,
    }
    selected_checks = _get_enabled_checks()
    if not selected_checks:
        raise ValueError("ENABLED_CHECKS cannot be empty")

    unknown_checks = [name for name in selected_checks if name not in available_checks]
    if unknown_checks:
        valid = ", ".join(sorted(available_checks))
        unknown = ", ".join(unknown_checks)
        raise ValueError(f"Unknown check(s): {unknown}. Valid values: {valid}")

    failed: dict[str, CheckResult] = {}
    for check_name in selected_checks:
        print(f"Running check: {check_name}")
        result = available_checks[check_name]()
        if result.errors:
            failed[check_name] = result

    if failed:
        body_html = _build_combined_alert_body(failed)
        if ATTACHMENT_PATHS:
            send_email(
                to_emails=recipients,
                subject=ALERT_SUBJECT,
                body_html=body_html,
                attachment_paths=ATTACHMENT_PATHS,
            )
        else:
            send_email(
                to_emails=recipients,
                subject=ALERT_SUBJECT,
                body_html=body_html,
            )
        print("Alert email sent.")
    else:
        checks_text = " + ".join(selected_checks)
        print(f"All checks passed ({checks_text}). No alert email sent.")


if __name__ == "__main__":
    main()

