#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Pull GPS-tracked exercises from the Google Health API and drop them as TCX
files into Dreeve's watch folder for automatic import.

Wraps the `ghealth` CLI (https://github.com/rudrankriyam/Google-Health-CLI)
for the actual REST calls; this script handles token refresh, date
windowing (the API caps a single query at 90 days), state tracking to
avoid re-exporting the same activity, and writing into the shared volume.

Configuration is entirely via environment variables -- see README.md.
"""
import json
import logging
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

GHEALTH_BIN = os.environ.get("GHEALTH_BIN", "/usr/local/bin/ghealth")
GHEALTH_HOME = Path(os.environ.get("GHEALTH_HOME", "/tmp/ghealth-home"))
TOKEN_DIR = Path(os.environ.get("TOKEN_DIR", "/data"))
WATCH_DIR = Path(os.environ.get("WATCH_DIR", "/watch"))

CLIENT_ID = os.environ.get("GHEALTH_CLIENT_ID")
CLIENT_SECRET = os.environ.get("GHEALTH_CLIENT_SECRET")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "30"))
FROM_DATE = os.environ.get("FROM_DATE")  # optional explicit backfill window
TO_DATE = os.environ.get("TO_DATE")

MAX_QUERY_DAYS = 90  # API limit for the "exercise" data type
TOKEN_URL = "https://oauth2.googleapis.com/token"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

STATE_FILE = TOKEN_DIR / "exported_ids.json"
TOKEN_FILE = TOKEN_DIR / "token.json"


def require_config():
    missing = [name for name, val in [
        ("GHEALTH_CLIENT_ID", CLIENT_ID),
        ("GHEALTH_CLIENT_SECRET", CLIENT_SECRET),
    ] if not val]
    if missing:
        sys.exit(f"missing required env var(s): {', '.join(missing)}")
    if not TOKEN_FILE.exists():
        sys.exit(
            f"{TOKEN_FILE} not found. Run bootstrap_login.py locally on a machine "
            "with a browser and mount the resulting token.json into TOKEN_DIR."
        )


def refresh_access_token():
    token = json.loads(TOKEN_FILE.read_text())

    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": token["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()

    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req) as resp:
        new_token = json.loads(resp.read())

    token["access_token"] = new_token["access_token"]
    token["expires_in"] = new_token["expires_in"]
    token["expiry"] = (datetime.now(timezone.utc) + timedelta(seconds=new_token["expires_in"])).isoformat()
    if "refresh_token" in new_token:  # Google rotates it occasionally
        token["refresh_token"] = new_token["refresh_token"]

    TOKEN_FILE.write_text(json.dumps(token, indent=2))
    return token


def setup_ghealth_home(token):
    """ghealth reads ~/.config/ghealth/{config,token}.json; point $HOME at a
    scratch dir we control so we don't need extra CLI flags."""
    config_dir = GHEALTH_HOME / ".config/ghealth"
    config_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "baseURL": "https://health.googleapis.com",
        "user": "users/me",
        "clientID": CLIENT_ID,
    }
    (config_dir / "config.json").write_text(json.dumps(config, indent=2))
    (config_dir / "token.json").write_text(json.dumps(token, indent=2))


def run_ghealth(*args):
    env = dict(os.environ, HOME=str(GHEALTH_HOME))
    result = subprocess.run([GHEALTH_BIN, *args], capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"ghealth {' '.join(args)} failed: {result.stderr or result.stdout}")
    return result.stdout


def load_state():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_state(ids):
    STATE_FILE.write_text(json.dumps(sorted(ids)))


def list_exercises_window(since_dt, until_dt):
    since = since_dt.strftime("%Y-%m-%dT%H:%M:%S")
    until = until_dt.strftime("%Y-%m-%dT%H:%M:%S")
    points = []
    page_token = None
    while True:
        args = ["data", "list", "exercise", "--from", since, "--to", until, "--limit", "25"]
        if page_token:
            args += ["--page-token", page_token]
        data = json.loads(run_ghealth(*args))
        points.extend(data.get("dataPoints", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return points


def list_exercises_range(start, end):
    points = []
    window_start = start
    while window_start < end:
        window_end = min(window_start + timedelta(days=MAX_QUERY_DAYS), end)
        logging.info(f"querying {window_start.date()} .. {window_end.date()}")
        points.extend(list_exercises_window(window_start, window_end))
        window_start = window_end
    return points


def resolve_range():
    if FROM_DATE:
        start = datetime.fromisoformat(FROM_DATE).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(TO_DATE).replace(tzinfo=timezone.utc) if TO_DATE else datetime.now(timezone.utc)
        return start, end
    now = datetime.now(timezone.utc)
    return now - timedelta(days=LOOKBACK_DAYS), now


def safe_filename(point):
    exercise = point.get("exercise", {})
    start = exercise.get("interval", {}).get("startTime", "unknown")
    start = re.sub(r"[^0-9A-Za-z]", "-", start)
    ex_type = exercise.get("exerciseType", "activity").lower()
    point_id = point["name"].rsplit("/", 1)[-1]
    return f"{start}_{ex_type}_{point_id}.tcx"


def export_tcx(point_id):
    return run_ghealth("data", "export-tcx", f"users/me/dataTypes/exercise/dataPoints/{point_id}")


def main():
    require_config()
    token = refresh_access_token()
    setup_ghealth_home(token)

    processed = load_state()
    start, end = resolve_range()
    points = list_exercises_range(start, end)

    new_count = 0
    for point in points:
        point_id = point["name"].rsplit("/", 1)[-1]
        if point_id in processed:
            continue

        has_gps = point.get("exercise", {}).get("exerciseMetadata", {}).get("hasGps", False)
        if not has_gps:
            processed.add(point_id)
            save_state(processed)
            continue

        try:
            tcx = export_tcx(point_id)
        except RuntimeError as e:
            logging.warning(f"skip {point_id}: {e}")
            continue

        if "<Trackpoint>" not in tcx:
            processed.add(point_id)
            save_state(processed)
            continue

        filename = safe_filename(point)
        WATCH_DIR.mkdir(parents=True, exist_ok=True)
        (WATCH_DIR / filename).write_text(tcx)

        processed.add(point_id)
        save_state(processed)
        new_count += 1
        logging.info(f"exported {filename}")

    logging.info(f"done: {new_count} new TCX file(s) written")


if __name__ == "__main__":
    main()
