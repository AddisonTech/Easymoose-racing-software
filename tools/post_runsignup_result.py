"""Add finishers missing from a RunSignup result set, one result at a time.

    python tools/post_runsignup_result.py                  # list missing bibs
    python tools/post_runsignup_result.py --bib 123        # show what would be sent
    python tools/post_runsignup_result.py --bib 123 --send # send it

RunSignup's dashboard has no way to add one in-person finisher to a result set
that is already published, and uploading again makes a second set. The results
API does add a single result to an existing set, so this posts just that one.

Credentials come from .env in the repo root, which git ignores:
    RUNSIGNUP_API_KEY=...
    RUNSIGNUP_API_SECRET=...
Generate them on the race dashboard under Race > Secure Access / Info Sharing.
The secret goes in a header, never in the URL.

The set on RunSignup is compared by bib against Data/results_<event>.csv, the
file split_results_by_event.py writes and that was uploaded. With no --bib the
bibs in the file but not on RunSignup are listed and nothing is sent. With
--bib, the place is worked out from the clock times already on RunSignup
(after any tie) unless --place is given, and the request is printed. Only
--send posts it, and the set is read back afterwards to confirm the bib is on.

The API does not move anyone else down a place. After sending, open the new
row in the results editor, keep "Update impacted places" ticked and save, then
Recompute Division Placements and Recompute Pace on the set.

Only counts, bibs, places and times are printed, never names.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "Data"
ENV_FILE = ROOT / ".env"
API = "https://api.runsignup.com/rest/race/{race_id}/results/{action}"
RACE_ID = 205792
# Panther Prowl 5K 2026.
EVENTS = {
    "adult": {"event_id": 1150739, "result_set": 728616},
    "junior": {"event_id": 1150740, "result_set": 728619},
}
PAGE_SIZE = 100


class RunSignupError(RuntimeError):
    pass


def load_env(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and # comments skipped, quotes stripped."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def credentials() -> tuple[str, str]:
    env = load_env(ENV_FILE)
    key = os.environ.get("RUNSIGNUP_API_KEY") or env.get("RUNSIGNUP_API_KEY", "")
    secret = os.environ.get("RUNSIGNUP_API_SECRET") or env.get("RUNSIGNUP_API_SECRET", "")
    if not key or not secret:
        raise RunSignupError(f"RUNSIGNUP_API_KEY and RUNSIGNUP_API_SECRET must be set in {ENV_FILE}")
    return key, secret


def seconds(clock: str) -> float:
    """m:ss.hh or h:mm:ss.hh as seconds."""
    parts = clock.strip().split(":")
    if not 2 <= len(parts) <= 3:
        raise ValueError(f"unexpected time {clock!r}")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return total


def call(action: str, key: str, secret: str, query: dict, form: dict | None = None) -> dict:
    url = API.format(race_id=RACE_ID, action=action) + "?" + urllib.parse.urlencode(
        {**query, "rsu_api_key": key, "format": "json"}
    )
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    request = urllib.request.Request(url, data=data, headers={"X-RSU-API-SECRET": secret})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RunSignupError(f"{action}: HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:500]}")
    if isinstance(body, dict) and "error" in body:
        raise RunSignupError(f"{action}: {json.dumps(body['error'])}")
    return body


def fetch_results(key: str, secret: str, event_id: int, result_set: int) -> list[dict]:
    results: list[dict] = []
    page = 1
    while True:
        body = call("get-results", key, secret, {
            "event_id": event_id,
            "individual_result_set_id": result_set,
            "results_per_page": PAGE_SIZE,
            "page": page,
        })
        sets = [s for s in body.get("individual_results_sets", [])
                if int(s.get("individual_result_set_id", 0)) == result_set]
        if not sets:
            raise RunSignupError(f"result set {result_set} not found for event {event_id}")
        batch = sets[0].get("results", [])
        results.extend(batch)
        if len(batch) < PAGE_SIZE:
            return results
        page += 1


def local_results(path: Path) -> dict[str, dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return {row["bib"].strip(): row for row in csv.DictReader(handle) if row.get("bib", "").strip()}


def place_for(clock: str, existing: list[dict]) -> int:
    """One more than everyone at or under this clock time."""
    mine = seconds(clock)
    return 1 + sum(1 for r in existing if r.get("clock_time") and seconds(r["clock_time"]) <= mine)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--event", choices=sorted(EVENTS), default="adult")
    parser.add_argument("--event-id", type=int, help="RunSignup event ID, overrides --event")
    parser.add_argument("--result-set", type=int, help="RunSignup result set ID, overrides --event")
    parser.add_argument("--results", type=Path, help="local results CSV (default Data/results_<event>.csv)")
    parser.add_argument("--bib", help="the bib to add")
    parser.add_argument("--clock-time", help="clock time, default the one in the local results CSV")
    parser.add_argument("--chip-time", help="chip time, left off if not given")
    parser.add_argument("--place", type=int, help="overall place, default worked out from clock times")
    parser.add_argument("--send", action="store_true", help="post the result; without it nothing is sent")
    args = parser.parse_args(argv)

    event_id = args.event_id or EVENTS[args.event]["event_id"]
    result_set = args.result_set or EVENTS[args.event]["result_set"]
    if event_id is None:
        parser.error(f"--event-id is needed for {args.event}")
    results_path = args.results or DATA_DIR / f"results_{args.event}.csv"

    try:
        key, secret = credentials()
        existing = fetch_results(key, secret, event_id, result_set)
        on_runsignup = {str(r.get("bib", "")).strip() for r in existing}
        local = local_results(results_path) if results_path.exists() else {}
        print(f"RunSignup set {result_set}: {len(existing)} results. {results_path.name}: {len(local)} rows.")

        if not args.bib:
            missing = [bib for bib in local if bib not in on_runsignup]
            if not missing:
                print("No bibs missing.")
            for bib in missing:
                print(f"  missing bib {bib}  clock {local[bib]['clock_time']}")
            return 0

        bib = args.bib.strip()
        if bib in on_runsignup:
            raise RunSignupError(f"bib {bib} is already in result set {result_set}; edit that row instead")
        clock = args.clock_time or local.get(bib, {}).get("clock_time")
        if not clock:
            raise RunSignupError(f"no clock time for bib {bib}; pass --clock-time")
        seconds(clock)
        result = {"bib_num": int(bib), "place": args.place or place_for(clock, existing), "clock_time": clock}
        if args.chip_time:
            seconds(args.chip_time)
            result["chip_time"] = args.chip_time
        form = {
            "event_id": event_id,
            "individual_result_set_id": result_set,
            "request_format": "json",
            "request": json.dumps({"results": [result]}),
        }
        print(f"POST race {RACE_ID} event {event_id} set {result_set}: {form['request']}")
        if not args.send:
            print("Not sent. Add --send to post it.")
            return 0

        call("full-results", key, secret, {}, form)
        after = fetch_results(key, secret, event_id, result_set)
        if bib not in {str(r.get("bib", "")).strip() for r in after}:
            raise RunSignupError(f"sent, but bib {bib} is not in result set {result_set} on reading it back")
        print(f"Sent. Set {result_set} now has {len(after)} results including bib {bib}.")
        print('Next: open the new row in the results editor, keep "Update impacted places" ticked and save,')
        print("then Recompute Division Placements and Recompute Pace on the set.")
        return 0
    except (RunSignupError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
