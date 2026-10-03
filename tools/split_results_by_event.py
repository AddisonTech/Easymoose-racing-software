"""Split Data/gun_time_results.csv by registration event for upload.

    python tools/split_results_by_event.py

Events come from Data/registration_bibs.csv, matched by bib. Writes, in Data/:
    results_adult.csv      5K (Adult)
    results_junior.csv     5K (Junior)
    results_unmatched.csv  finishers with no name, no event, or any other event

Columns are bib, first_name, last_name, age, gender, clock_time, with
clock_time as H:MM:SS.ss, in finish order. Only counts and bib numbers are
printed, never names.
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pair import format_bibs  # noqa: E402

DATA_DIR = ROOT / "Data"
COLUMNS = ["bib", "first_name", "last_name", "age", "gender", "clock_time"]
EVENTS = {"5k (adult)": "adult", "5k (junior)": "junior"}


def clock_time(elapsed: str) -> str:
    """m:ss.hh or h:mm:ss.hh as H:MM:SS.ss."""
    parts = elapsed.strip().split(":")
    if len(parts) == 2:
        parts.insert(0, "0")
    if len(parts) != 3:
        raise ValueError(f"unexpected elapsed time {elapsed!r}")
    hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
    return f"{hours}:{minutes:02d}:{seconds:05.2f}"


def write_csv(path: Path, rows: list[list]) -> None:
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        writer.writerows(rows)
    os.replace(temp, path)


def main() -> int:
    with (DATA_DIR / "registration_bibs.csv").open(newline="", encoding="utf-8-sig") as handle:
        events = {row["bib"].strip(): (row.get("event") or "").strip()
                  for row in csv.DictReader(handle) if (row.get("bib") or "").strip()}
    with (DATA_DIR / "gun_time_results.csv").open(newline="", encoding="utf-8-sig") as handle:
        results = sorted(csv.DictReader(handle), key=lambda row: int(row["place"]))

    split: dict[str, list[list]] = {"adult": [], "junior": [], "unmatched": []}
    unmatched = []
    for row in results:
        bib = row["bib"].strip()
        named = row["first_name"].strip() or row["last_name"].strip()
        group = EVENTS.get(events.get(bib, "").casefold()) if named else None
        if group is None:
            group = "unmatched"
            unmatched.append(bib)
        split[group].append([bib, row["first_name"], row["last_name"], row["age"], row["gender"],
                             clock_time(row["elapsed"])])

    for group, rows in split.items():
        write_csv(DATA_DIR / f"results_{group}.csv", rows)

    numbers = sorted(int(b) for b in unmatched if b.isdigit())
    print(f"Finishers in gun_time_results.csv: {len(results)}")
    print(f"Adult finishers: {len(split['adult'])}")
    print(f"Junior finishers: {len(split['junior'])}")
    print(f"Unmatched: {len(unmatched)}: {format_bibs(numbers) if numbers else 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
