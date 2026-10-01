"""Merge the registration list with pairs.csv into a participant CSV.

    python merge.py Data/registration_bibs.csv Data/pairs.csv --out Data/participants.csv

The registration CSV has bib,first_name,last_name,age,gender. The output is the
participant CSV the console imports: bib,first_name,last_name,age,gender,epc1,epc2.

Every paired bib goes in, named or not, so spare bibs and day-of signups still
time. Registered bibs with no pair cannot time at all, so they are left out
and listed.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from pair import PairsFileError, format_bibs, load_pairs

REGISTRATION_FIELDS = ["first_name", "last_name", "age", "gender"]
PARTICIPANT_COLUMNS = ["bib", "first_name", "last_name", "age", "gender", "epc1", "epc2"]


class RegistrationError(ValueError):
    pass


def header_key(name: str | None) -> str:
    """'First Name' and 'first_name' are the same column. Registration
    exports use the first form, the participant CSV the second."""
    return (name or "").strip().lower().replace(" ", "_").replace("-", "_")


def load_registration(path: Path) -> tuple[dict[int, dict], int]:
    """Read the registration CSV into {bib: {first_name, last_name, age, gender}}.

    Also returns how many registrations have no bib yet. Those cannot be
    matched to a pair, so the caller has to say so rather than drop them
    quietly. Columns other than the ones needed are ignored.
    """
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise RegistrationError(f"{path} is empty")
        headers = {header_key(name): name for name in reader.fieldnames}
        if "bib" not in headers:
            raise RegistrationError(f"{path} has no bib column")

        people: dict[int, dict] = {}
        no_bib = 0
        for line_number, row in enumerate(reader, start=2):
            text = (row.get(headers["bib"]) or "").strip()
            if not text:
                if any((value or "").strip() for value in row.values() if isinstance(value, str)):
                    no_bib += 1
                continue
            try:
                bib = int(text)
            except ValueError:
                raise RegistrationError(f"{path} line {line_number}: bib {text!r} is not a number")
            if bib in people:
                raise RegistrationError(f"{path} line {line_number}: bib {bib} appears twice")
            people[bib] = {
                key: (row.get(headers[key]) or "").strip() if key in headers else ""
                for key in REGISTRATION_FIELDS
            }
    return people, no_bib


def merge(registration: dict[int, dict], pairs: dict[int, str]) -> tuple[list[dict], list[int]]:
    """Participant rows for every paired bib, and the registered bibs left unpaired."""
    rows = []
    for bib in sorted(pairs):
        person = registration.get(bib, {})
        rows.append(
            {
                "bib": bib,
                **{key: person.get(key, "") for key in REGISTRATION_FIELDS},
                "epc1": pairs[bib],
                "epc2": "",
            }
        )
    unpaired = sorted(bib for bib in registration if bib not in pairs)
    return rows, unpaired


def write_participants(path: Path, rows: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PARTICIPANT_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Merge registration and pairs.csv into a participant CSV.")
    parser.add_argument("registration", type=Path, help="bib,first_name,last_name,age,gender")
    parser.add_argument("pairs", type=Path, help="pairs.csv from pair.py")
    parser.add_argument("--out", type=Path, default=Path("Data") / "participants.csv",
                        help="participant CSV to write; keep it in Data/, which git ignores")
    args = parser.parse_args(argv)

    try:
        registration, no_bib = load_registration(args.registration)
        pairs = load_pairs(args.pairs)
    except (RegistrationError, PairsFileError, OSError) as exc:
        print(f"Error: {exc}")
        return 1
    if not pairs:
        print(f"Error: no pairs in {args.pairs}")
        return 1

    rows, unpaired = merge(registration, pairs)
    write_participants(args.out, rows)

    named = sum(1 for row in rows if row["bib"] in registration)
    print(f"Wrote {len(rows)} participants to {args.out}: "
          f"{named} registered, {len(rows) - named} paired with no registration.")
    if no_bib:
        print(f"WARNING: {no_bib} registrations have no bib number and were not matched. "
              "Assign bibs in registration and export again.")
    if unpaired:
        print(f"Registered but not paired, left out ({len(unpaired)}): {format_bibs(unpaired)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
