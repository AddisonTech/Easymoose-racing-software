"""Assign bib numbers to a registration export and build the pickup sheet.

    python assign_bibs.py Data/<registration export>.csv

Runners are sorted by last name, then first name, ignoring case, and given
bibs from 110 up. That writes Data/registration_bibs.csv, which pair.py shows
names from and merge.py reads, and Data/pickup_sheet.html, a printable list
for the bib pickup table. It also writes Data/runsignup_bib_import.csv,
Registration ID and Bib, for loading the bibs back into registration.

Once bibs are assigned they are printed on the sheet and handed out, so
running this again does not reassign them. It rebuilds the pickup sheet from
the existing registration_bibs.csv instead. --force reassigns from scratch.
A roster written before registration_id was recorded gets it filled in from
the export, matched row by row, with every bib left where it is.

The export itself is never modified. Only counts are printed here, never
names: everything with a name in it stays in Data/, which git ignores.
"""

from __future__ import annotations

import argparse
import csv
import html
import sys
from datetime import date
from pathlib import Path

from merge import header_key
from pair import DATA_DIR, DEFAULT_ROSTER, FIRST_BIB, LAST_BIB, format_bibs

DEFAULT_SHEET = DATA_DIR / "pickup_sheet.html"
DEFAULT_RUNSIGNUP = DATA_DIR / "runsignup_bib_import.csv"
ROSTER_COLUMNS = ["bib", "first_name", "last_name", "age", "gender", "event", "tshirt", "registration_id"]

# What identifies a runner when an older roster has to be matched back to the
# export to recover registration IDs.
MATCH_FIELDS = ("first_name", "last_name", "age", "gender", "event", "tshirt")

# Export header -> roster column. Matched after header_key(), so "First Name"
# and "first_name" are the same.
EXPORT_COLUMNS = {
    "first_name": "first_name",
    "last_name": "last_name",
    "age": "age",
    "gender": "gender",
    "event": "event",
    "t_shirt": "tshirt",
    "tshirt": "tshirt",
    "registration_id": "registration_id",
}


class AssignError(ValueError):
    pass


def read_export(path: Path) -> tuple[list[dict], int]:
    """Runners from a registration export, and how many rows had no name."""
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise AssignError(f"{path} is empty")
        columns = {}
        for name in reader.fieldnames:
            target = EXPORT_COLUMNS.get(header_key(name))
            if target and target not in columns:
                columns[target] = name
        missing = {"first_name", "last_name"} - set(columns)
        if missing:
            raise AssignError(f"{path} has no {' or '.join(sorted(missing))} column")

        runners, nameless = [], 0
        for row in reader:
            runner = {
                key: (row.get(columns[key]) or "").strip() if key in columns else ""
                for key in ROSTER_COLUMNS if key != "bib"
            }
            if not runner["first_name"] and not runner["last_name"]:
                if any((value or "").strip() for value in row.values() if isinstance(value, str)):
                    nameless += 1
                continue
            runners.append(runner)
    return runners, nameless


def assign(runners: list[dict], first_bib: int = FIRST_BIB, last_bib: int = LAST_BIB) -> list[dict]:
    """Sort by last name then first name, ignoring case, and number from first_bib."""
    capacity = last_bib - first_bib + 1
    if len(runners) > capacity:
        raise AssignError(f"{len(runners)} runners but only {capacity} bibs ({first_bib}-{last_bib})")
    ordered = sorted(runners, key=lambda r: (r["last_name"].casefold(), r["first_name"].casefold()))
    return [{"bib": first_bib + index, **runner} for index, runner in enumerate(ordered)]


def write_roster(path: Path, rows: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ROSTER_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_roster(path: Path) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        rows = []
        for row in csv.DictReader(handle):
            row = {key: (row.get(key) or "").strip() for key in ROSTER_COLUMNS}
            if row["bib"].isdigit():
                row["bib"] = int(row["bib"])
                rows.append(row)
    return rows


SHEET_STYLE = """
  * { box-sizing: border-box; }
  body { font-family: Arial, Helvetica, sans-serif; color: #000; background: #fff;
         margin: 0; padding: 16px; font-size: 17pt; }
  h1 { font-size: 22pt; margin: 0 0 4px; }
  .meta { font-size: 12pt; margin: 0 0 12px; }
  table { width: 100%; border-collapse: collapse; }
  thead { display: table-header-group; }
  th { text-align: left; font-size: 13pt; text-transform: uppercase; letter-spacing: 0.05em;
       border-bottom: 2px solid #000; padding: 6px 8px; }
  td { padding: 7px 8px; border-bottom: 1px solid #999; vertical-align: middle; }
  tr { page-break-inside: avoid; break-inside: avoid; }
  tbody tr:nth-child(even) td { background: #f0f0f0; }
  td.bib { font-weight: bold; font-size: 20pt; white-space: nowrap; }
  td.event, td.tshirt { font-size: 14pt; white-space: nowrap; }
  .box { display: inline-block; width: 26px; height: 26px; border: 2px solid #000; }
  tr.blank td { height: 44px; background: #fff; }
  @media print {
    body { padding: 0; }
    @page { margin: 12mm; }
    tbody tr:nth-child(even) td { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  }
"""


def pickup_sheet(rows: list[dict], title: str, blank_rows: int, spare: str) -> str:
    """A printable pickup list, sorted by last name, with blank rows for signups."""
    ordered = sorted(rows, key=lambda r: (r["last_name"].casefold(), r["first_name"].casefold()))
    body = []
    for row in ordered:
        body.append(
            "<tr>"
            f"<td>{html.escape(row['last_name'])}</td>"
            f"<td>{html.escape(row['first_name'])}</td>"
            f"<td class=\"bib\">{row['bib']}</td>"
            f"<td class=\"event\">{html.escape(row['event'])}</td>"
            f"<td class=\"tshirt\">{html.escape(row['tshirt'])}</td>"
            "<td><span class=\"box\"></span></td>"
            "</tr>"
        )
    for _ in range(blank_rows):
        body.append("<tr class=\"blank\"><td></td><td></td><td></td><td></td><td></td>"
                    "<td><span class=\"box\"></span></td></tr>")
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{html.escape(title)}</title>\n<style>{SHEET_STYLE}</style>\n</head>\n<body>\n"
        f"<h1>{html.escape(title)}</h1>\n"
        f"<p class=\"meta\">{len(rows)} registered, sorted by last name. "
        f"Day-of signups: spare bibs {html.escape(spare)}. Printed {date.today().isoformat()}.</p>\n"
        "<table>\n<thead><tr><th>Last</th><th>First</th><th>Bib</th><th>Event</th>"
        "<th>T-Shirt</th><th>Picked up</th></tr></thead>\n<tbody>\n"
        + "\n".join(body)
        + "\n</tbody>\n</table>\n</body>\n</html>\n"
    )


def match_key(row: dict) -> tuple:
    return tuple(row[field].strip().casefold() for field in MATCH_FIELDS)


def backfill_registration_ids(rows: list[dict], runners: list[dict]) -> int:
    """Fill in missing registration IDs on roster rows from the export.

    Bibs are not touched. Each roster row has to match exactly one export row
    that is not already claimed, or nothing is changed and the bib is named in
    the error. Returns how many rows were filled.
    """
    claimed = {row["registration_id"] for row in rows if row["registration_id"]}
    candidates: dict[tuple, list[str]] = {}
    for runner in runners:
        if runner["registration_id"] and runner["registration_id"] not in claimed:
            candidates.setdefault(match_key(runner), []).append(runner["registration_id"])

    filled = {}
    for row in rows:
        if row["registration_id"]:
            continue
        found = candidates.get(match_key(row), [])
        if len(found) != 1:
            problem = "no row" if not found else "more than one row"
            raise AssignError(f"bib {row['bib']} matches {problem} in the export; no IDs were filled")
        filled[row["bib"]] = found[0]
    for row in rows:
        if row["bib"] in filled:
            row["registration_id"] = filled[row["bib"]]
    return len(filled)


def write_runsignup(path: Path, rows: list[dict]) -> None:
    """Registration ID and Bib, one row per runner, for the bib import."""
    missing = [row["bib"] for row in rows if not row["registration_id"]]
    if missing:
        raise AssignError(f"no registration ID for bibs {format_bibs(missing)}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["Registration ID", "Bib"])
        for row in sorted(rows, key=lambda r: r["bib"]):
            writer.writerow([row["registration_id"], row["bib"]])


def spare_range(rows: list[dict], first_bib: int, last_bib: int) -> tuple[int | None, str]:
    """The highest bib used, and the unused range as text."""
    used = {row["bib"] for row in rows}
    last_used = max(used) if used else None
    spare = [bib for bib in range(first_bib, last_bib + 1) if bib not in used]
    return last_used, format_bibs(spare) if spare else "none"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Assign bibs to a registration export and build the pickup sheet.")
    parser.add_argument("export", type=Path, help="registration export CSV (left unchanged)")
    parser.add_argument("--out", type=Path, default=DEFAULT_ROSTER, help="bib assignments to write")
    parser.add_argument("--sheet", type=Path, default=DEFAULT_SHEET, help="printable pickup sheet to write")
    parser.add_argument("--runsignup", type=Path, default=DEFAULT_RUNSIGNUP,
                        help="Registration ID,Bib file to write for the bib import")
    parser.add_argument("--title", default="Bib pickup", help="heading on the pickup sheet")
    parser.add_argument("--first", type=int, default=FIRST_BIB)
    parser.add_argument("--last", type=int, default=LAST_BIB)
    parser.add_argument("--blank-rows", type=int, default=None,
                        help="empty rows for day-of signups; default one per spare bib")
    parser.add_argument("--force", action="store_true",
                        help="reassign even though bibs were already assigned")
    args = parser.parse_args(argv)

    try:
        if args.out.exists() and not args.force:
            rows = read_roster(args.out)
            print(f"Bibs already assigned in {args.out}; keeping them and rebuilding the sheet. "
                  "Use --force to reassign.")
            runners, _ = read_export(args.export)
            if any(not row["registration_id"] for row in rows):
                filled = backfill_registration_ids(rows, runners)
                write_roster(args.out, rows)
                print(f"Filled in {filled} registration IDs from {args.export.name}; no bib changed.")
            assigned = {row["registration_id"] for row in rows}
            unassigned = sum(1 for r in runners if r["registration_id"] and r["registration_id"] not in assigned)
            if unassigned:
                print(f"NOTE: {unassigned} runners in the export have no bib yet.")
        else:
            runners, nameless = read_export(args.export)
            rows = assign(runners, args.first, args.last)
            write_roster(args.out, rows)
            print(f"Assigned {len(rows)} runners from {args.export.name} to {args.out}.")
            if nameless:
                print(f"WARNING: {nameless} rows had no name and were not given a bib.")
        write_runsignup(args.runsignup, rows)
    except (AssignError, OSError) as exc:
        print(f"Error: {exc}")
        return 1

    last_used, spare = spare_range(rows, args.first, args.last)
    spare_count = (args.last - args.first + 1) - len(rows)
    blank_rows = spare_count if args.blank_rows is None else args.blank_rows
    args.sheet.parent.mkdir(parents=True, exist_ok=True)
    args.sheet.write_text(pickup_sheet(rows, args.title, blank_rows, spare), encoding="utf-8")

    events: dict[str, int] = {}
    for row in rows:
        events[row["event"] or "(none)"] = events.get(row["event"] or "(none)", 0) + 1
    print(f"Last bib used: {last_used}. Spare for day-of signups: {spare} ({spare_count} bibs).")
    print("By event: " + ", ".join(f"{name} {count}" for name, count in sorted(events.items())))
    print(f"Pickup sheet: {args.sheet} ({len(rows)} runners, {blank_rows} blank rows).")
    print(f"Bib import: {args.runsignup} ({len(rows)} rows).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
