import re
import sys
import warnings
from datetime import datetime, timezone

warnings.filterwarnings("ignore", message="urllib3 .* or chardet.*doesn't match a supported version")

import requests

ZARR_BASE_URL = "https://s3.ap-southeast-2.wasabisys.com/spc-zarr-file/"
DATASETS = [
    "d1_temp_salt_uv_z_all.zarr",
]

UNITS_RE = re.compile(r"^(seconds|minutes|hours|days)\s+since\s+(.+)$", re.IGNORECASE)


def get_zarr_model_run_time(dataset: str, base_url: str = ZARR_BASE_URL) -> datetime:
    url = f"{base_url.rstrip('/')}/{dataset}/time/.zattrs"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    units = resp.json()["units"]

    match = UNITS_RE.match(units)
    if not match:
        raise ValueError(f'Cannot parse time units: "{units}"')

    reference = match.group(2).strip()
    return datetime.fromisoformat(reference).replace(tzinfo=timezone.utc)


if __name__ == "__main__":
    for dataset in DATASETS:
        try:
            run_time = get_zarr_model_run_time(dataset)
            print(f"{dataset} - {run_time.isoformat()}")
        except Exception:
            print(f"{dataset} - null")
