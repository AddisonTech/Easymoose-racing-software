"""Score a race by gun time from a copy of its saved data.

    python tools/gun_time_results.py [--race races/<folder>]

A gun-time fail-safe that can score a race from the gun. Every
bib in Data/participants.csv with a qualifying finish read gets a time from
the gun, start read or not.

The live race.db is never opened. Its files are copied into
Data/gun_time_tmp/ and only the copy is read, so the running app and its read
loop are not touched. SQLite in WAL mode can checkpoint while the copy is
taken, so the copy is retried until the database file and the WAL header are
the same before and after it.

Reads are stamped by the reader's clock. The app records the gun in that clock
as gun_time_reader_utc (the Pi's gun time plus the reader offset) and compares
reads against it, so this does too. A race recorded before that column existed
falls back to gun_time_utc, as the app does.

A finish is the first read on a finish antenna at least --min-elapsed seconds
after the gun. Elapsed is that read minus the gun.

Writes, all in Data/, which git ignores:
    gun_time_results.csv   place, bib, first_name, last_name, age, gender, elapsed
    gun_time_awards.csv    top 3 male and top 3 female overall
    gun_time_unnamed.csv   bibs read at the finish with no name, for the sign-up sheet

Only counts and bib numbers are printed, never names. Safe to rerun at any
point during the race: every output is rebuilt from scratch each time.
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pair import format_bibs  # noqa: E402
from reader import FINISH_ANTENNAS, START_ANTENNAS, normalize_epc  # noqa: E402
from timing import MICROS, format_elapsed  # noqa: E402

DATA_DIR = ROOT / "Data"
DEFAULT_RACE = ROOT / "races" / "2026-10-03_panther-prowl-5k"
COPY_DIR = DATA_DIR / "gun_time_tmp"
RESULT_COLUMNS = ["place", "bib", "first_name", "last_name", "age", "gender", "elapsed"]
COPY_ATTEMPTS = 10


class ScoringError(RuntimeError):
    pass


def _fingerprint(db_path: Path, wal_path: Path) -> tuple:
    """What changes if SQLite checkpoints or restarts the WAL mid copy."""
    stat = db_path.stat()
    header = b""
    if wal_path.exists():
        with wal_path.open("rb") as handle:
            header = handle.read(32)
    return stat.st_size, stat.st_mtime_ns, header


def copy_race_db(race_dir: Path, copy_dir: Path) -> Path:
    """Copy race.db and its WAL into copy_dir and return the copy's path."""
    source = race_dir / "race.db"
    wal = race_dir / "race.db-wal"
    if not source.exists():
        raise ScoringError(f"no race.db in {race_dir}")
    copy_dir.mkdir(parents=True, exist_ok=True)
    target = copy_dir / "race.db"

    for _ in range(COPY_ATTEMPTS):
        for stale in (target, copy_dir / "race.db-wal", copy_dir / "race.db-shm"):
            stale.unlink(missing_ok=True)
        before = _fingerprint(source, wal)
        shutil.copyfile(source, target)
        if wal.exists():
            shutil.copyfile(wal, copy_dir / "race.db-wal")
        if _fingerprint(source, wal) == before:
            # The shared memory index is left behind on purpose: SQLite rebuilds
            # it from the copied WAL, keeping only frames that check out.
            conn = sqlite3.connect(target)
            try:
                if conn.execute("PRAGMA quick_check").fetchone()[0] == "ok":
                    return target
            finally:
                conn.close()
        time.sleep(0.5)
    raise ScoringError(f"could not take a consistent copy of {source} in {COPY_ATTEMPTS} tries")


def load_race(db_path: Path) -> tuple[int, list[tuple[str, int, int]]]:
    """The gun in the reader's clock, and every read as (epc, port, time)."""
    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(races)")}
        reader_gun = "gun_time_reader_utc" if "gun_time_reader_utc" in columns else "NULL"
        row = conn.execute(f"SELECT gun_time_utc, {reader_gun} FROM races WHERE id = 1").fetchone()
        if row is None:
            raise ScoringError("the race database has no race row")
        gun = row[1] if row[1] is not None else row[0]
        if gun is None:
            raise ScoringError("the gun has not been fired in this race")
        reads = [
            (normalize_epc(epc), int(port), int(stamp))
            for epc, port, stamp in conn.execute(
                "SELECT epc, antenna_port, first_seen_utc FROM reads ORDER BY first_seen_utc, id"
            )
        ]
    finally:
        conn.close()
    return int(gun), reads


def load_participants(path: Path) -> tuple[dict[str, dict], dict[str, str]]:
    """Participants by bib, and EPC -> bib."""
    people, by_epc = {}, {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            bib = (row.get("bib") or "").strip()
            if not bib:
                continue
            people[bib] = {key: (row.get(key) or "").strip() for key in RESULT_COLUMNS[2:6]}
            for key in ("epc1", "epc2"):
                epc = normalize_epc((row.get(key) or "").strip())
                if epc:
                    by_epc[epc] = bib
    return people, by_epc


def bib_key(bib: str) -> tuple:
    return (0, int(bib), "") if bib.isdigit() else (1, 0, bib)


def gender_of(person: dict) -> str | None:
    first = person.get("gender", "")[:1].upper()
    return first if first in ("M", "F") else None


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    """Write next to the target and swap it in, so a reader never sees half a file."""
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    try:
        os.replace(temp, path)
    except PermissionError:
        temp.unlink(missing_ok=True)
        raise ScoringError(f"{path.name} is open in another program; close it and rerun")


def score(race_dir: Path, participants_csv: Path, min_elapsed: float) -> int:
    people, by_epc = load_participants(participants_csv)
    gun, reads = load_race(copy_race_db(race_dir, COPY_DIR))
    cutoff = gun + int(round(min_elapsed * MICROS))

    started, at_finish, early, finish = set(), set(), set(), {}
    unknown_epcs = set()
    for epc, port, stamp in reads:
        bib = by_epc.get(epc)
        if bib is None:
            unknown_epcs.add(epc)
            continue
        if stamp < gun:
            continue
        if port in START_ANTENNAS:
            started.add(bib)
        elif port in FINISH_ANTENNAS:
            at_finish.add(bib)
            if stamp < cutoff:
                early.add(bib)
            elif bib not in finish:
                finish[bib] = stamp  # reads come in time order, so this is the first

    order = sorted(finish, key=lambda bib: (finish[bib], bib_key(bib)))
    results = []
    for place, bib in enumerate(order, start=1):
        elapsed = round((finish[bib] - gun) / MICROS, 2)
        results.append((place, bib, people[bib], elapsed))

    write_csv(
        DATA_DIR / "gun_time_results.csv",
        RESULT_COLUMNS,
        [[place, bib, *(person[key] for key in RESULT_COLUMNS[2:6]), format_elapsed(elapsed)]
         for place, bib, person, elapsed in results],
    )

    awards = []
    for label, code in (("Male", "M"), ("Female", "F")):
        top = [r for r in results if gender_of(r[2]) == code][:3]
        awards += [[label, rank, place, bib, person["first_name"], person["last_name"], format_elapsed(elapsed)]
                   for rank, (place, bib, person, elapsed) in enumerate(top, start=1)]
    write_csv(DATA_DIR / "gun_time_awards.csv",
              ["category", "rank", "place", "bib", "first_name", "last_name", "elapsed"], awards)

    elapsed_by_bib = {bib: elapsed for _, bib, _, elapsed in results}
    unnamed = sorted((bib for bib in at_finish
                      if not people[bib]["first_name"] and not people[bib]["last_name"]), key=bib_key)
    write_csv(DATA_DIR / "gun_time_unnamed.csv", ["bib", "elapsed"],
              [[bib, format_elapsed(elapsed_by_bib.get(bib))] for bib in unnamed])

    def bibs(values) -> str:
        numbers = sorted(int(b) for b in values if b.isdigit())
        return format_bibs(numbers) if numbers else "none"

    ignored = early - set(finish)
    no_gender = sum(1 for _, _, person, _ in results if gender_of(person) is None)
    print(f"Reads in copy: {len(reads)}. Gun (reader clock) to last read: "
          f"{format_elapsed(max((s for _, _, s in reads), default=gun) / MICROS - gun / MICROS)}.")
    print(f"Distinct bibs read at the start (after the gun): {len(started)}")
    print(f"Distinct bibs read at the finish so far: {len(at_finish)}")
    print(f"Finishers scored: {len(results)}")
    print(f"Bibs seen at the finish before {min_elapsed:g} s (ignored): {len(early)}: {bibs(early)}")
    print(f"  of those, no qualifying finish yet: {len(ignored)}: {bibs(ignored)}")
    print(f"Finishers with no start read: {len(set(finish) - started)}")
    print(f"Bibs at the finish with no name: {len(unnamed)}: {bibs(unnamed)}")
    print(f"Awards: {sum(1 for a in awards if a[0] == 'Male')} male, "
          f"{sum(1 for a in awards if a[0] == 'Female')} female. Finishers with no M/F gender: {no_gender}")
    if unknown_epcs:
        print(f"WARNING: {len(unknown_epcs)} EPCs read that are not in {participants_csv.name}")
    print(f"Wrote {DATA_DIR / 'gun_time_results.csv'}, gun_time_awards.csv, gun_time_unnamed.csv")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Score a race by gun time from a copy of its data.")
    parser.add_argument("--race", type=Path, default=DEFAULT_RACE, help="race folder (read only)")
    parser.add_argument("--participants", type=Path, default=DATA_DIR / "participants.csv")
    parser.add_argument("--min-elapsed", type=float, default=720, help="seconds after the gun")
    args = parser.parse_args(argv)
    try:
        return score(args.race, args.participants, args.min_elapsed)
    except (ScoringError, OSError, sqlite3.DatabaseError) as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
