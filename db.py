"""Per race SQLite storage.

One race is one directory under races/, holding the database and every CSV
export taken during that race. A whole event can be copied off the Pi by
copying one folder.

The rule that governs this module: every raw read is written to the reads
table before anything looks at it. Results are derived and are always
reproducible from the read log, so recompute() can rebuild them from scratch
at any point, including long after the race is over.

Timestamps are integer microseconds since the Unix epoch throughout, matching
the reader's own FirstSeenTimestampUTC.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from reader import TagRead, normalize_epc
from timing import (
    DEFAULT_MIN_ELAPSED_SECONDS,
    Participant,
    ParticipantResult,
    compute_results,
    format_elapsed,
    places,
)

logger = logging.getLogger(__name__)

RACES_ROOT = Path("races")

STATUS_SETUP = "setup"
STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"

# The race row is always id 1: one database per race. race_id is carried on the
# child tables anyway so a pile of races could be merged into one file later
# without touching the schema.
RACE_ID = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS races (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    date TEXT NOT NULL,
    distance TEXT,
    gun_time_utc INTEGER,
    gun_time_reader_utc INTEGER,
    status TEXT NOT NULL DEFAULT 'setup',
    min_elapsed_seconds REAL NOT NULL DEFAULT 720
);

CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY,
    race_id INTEGER NOT NULL,
    bib TEXT NOT NULL,
    first_name TEXT,
    last_name TEXT,
    age INTEGER,
    gender TEXT,
    UNIQUE (race_id, bib)
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    participant_id INTEGER NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    epc TEXT NOT NULL,
    UNIQUE (epc)
);

CREATE TABLE IF NOT EXISTS reads (
    id INTEGER PRIMARY KEY,
    race_id INTEGER NOT NULL,
    epc TEXT NOT NULL,
    antenna_port INTEGER NOT NULL,
    first_seen_utc INTEGER NOT NULL,
    rssi REAL
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY,
    race_id INTEGER NOT NULL,
    participant_id INTEGER NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    start_utc INTEGER,
    finish_utc INTEGER,
    elapsed_seconds REAL,
    status TEXT NOT NULL,
    UNIQUE (race_id, participant_id)
);

CREATE INDEX IF NOT EXISTS reads_epc_time ON reads (epc, first_seen_utc);
CREATE INDEX IF NOT EXISTS tags_participant ON tags (participant_id);
"""

CSV_COLUMNS = [
    "place",
    "bib",
    "first_name",
    "last_name",
    "age",
    "gender",
    "start_time",
    "finish_time",
    "elapsed",
    "status",
]


class ParticipantImportError(ValueError):
    """A participant CSV that cannot be used."""


@dataclass(frozen=True)
class RaceInfo:
    """Header details for a race, for the archive list and the console."""

    name: str
    date: str
    distance: str
    gun_time_utc: int | None
    gun_time_reader_utc: int | None
    status: str
    min_elapsed_seconds: float
    directory: Path

    @property
    def slug(self) -> str:
        return self.directory.name

    @property
    def effective_gun_time_utc(self) -> int | None:
        """The gun time to compare reads against, in the reader's clock domain.

        Races timed before the clock domains were separated have no
        gun_time_reader_utc. Their reads and their gun time were compared
        directly at the time, so the Pi domain value is the one that
        reproduces the results they were given, and it is what recompute must
        keep using for them.
        """
        if self.gun_time_reader_utc is not None:
            return self.gun_time_reader_utc
        return self.gun_time_utc


def slugify(text: str) -> str:
    """Folder safe version of a race name."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text or "").strip("-").lower()
    return cleaned or "race"


def format_clock(utc_micros: int | None) -> str:
    """A timestamp as local ISO 8601 with hundredths.

    Local rather than UTC because a race director reads these against a wall
    clock, and the offset is on the string so it stays unambiguous.
    """
    if utc_micros is None:
        return ""
    moment = datetime.fromtimestamp(utc_micros / 1_000_000, tz=timezone.utc).astimezone()
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 10000:02d}" + moment.strftime("%z")


def parse_participant_csv(text: str) -> list[dict]:
    """Parse a participant CSV into rows ready for add_participants().

    Required columns are bib and at least one of epc1/epc2. Everything else is
    optional so a minimal file still imports. Raises ParticipantImportError with a
    message meant to be shown to the operator.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ParticipantImportError("the file is empty")

    headers = {(name or "").strip().lower(): name for name in reader.fieldnames}
    if "bib" not in headers:
        raise ParticipantImportError("no bib column found; expected bib,first_name,last_name,age,gender,epc1,epc2")
    if "epc1" not in headers and "epc2" not in headers:
        raise ParticipantImportError("no epc1 or epc2 column found")

    def field(row, key):
        source = headers.get(key)
        return (row.get(source) or "").strip() if source else ""

    rows: list[dict] = []
    seen_bibs: set[str] = set()
    seen_epcs: set[str] = set()
    for line_number, raw in enumerate(reader, start=2):
        bib = field(raw, "bib")
        if not bib:
            continue  # blank line at the end of the file, not an error

        if bib in seen_bibs:
            raise ParticipantImportError(f"line {line_number}: bib {bib} appears twice")
        seen_bibs.add(bib)

        epcs = []
        for key in ("epc1", "epc2"):
            epc = normalize_epc(field(raw, key))
            if not epc:
                continue
            if epc in seen_epcs:
                raise ParticipantImportError(f"line {line_number}: EPC {epc} is already assigned to another bib")
            seen_epcs.add(epc)
            epcs.append(epc)
        if not epcs:
            raise ParticipantImportError(f"line {line_number}: bib {bib} has no EPC")

        age = field(raw, "age")
        rows.append(
            {
                "bib": bib,
                "first_name": field(raw, "first_name"),
                "last_name": field(raw, "last_name"),
                "age": int(age) if age.isdigit() else None,
                "gender": field(raw, "gender"),
                "epcs": epcs,
            }
        )

    if not rows:
        raise ParticipantImportError("no participants found in the file")
    return rows


class RaceDB:
    """The database for one race.

    Connections are per thread. The reader loop writes reads from its own
    thread while Flask serves pages from others, and SQLite in WAL mode lets
    those readers run without waiting on the writer. That is what keeps an
    export from ever blocking the read loop.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.path = self.directory / "race.db"
        self._local = threading.local()
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            # Reads are appended constantly and every one of them matters, but
            # a fsync per read is not worth it: WAL plus NORMAL survives an
            # application crash, and only a power cut can lose the last commit.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @classmethod
    def create(
        cls,
        name: str,
        date: str,
        distance: str = "",
        min_elapsed_seconds: float = DEFAULT_MIN_ELAPSED_SECONDS,
        root: Path = RACES_ROOT,
    ) -> "RaceDB":
        """Make races/<date>_<slug>/ and the database inside it."""
        root = Path(root)
        directory = root / f"{date}_{slugify(name)}"
        suffix = 2
        while directory.exists():
            directory = root / f"{date}_{slugify(name)}-{suffix}"
            suffix += 1
        directory.mkdir(parents=True)

        db = cls(directory)
        conn = db.connection
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO races (id, name, date, distance, gun_time_utc, status, min_elapsed_seconds)"
            " VALUES (?, ?, ?, ?, NULL, ?, ?)",
            (RACE_ID, name, date, distance, STATUS_SETUP, float(min_elapsed_seconds)),
        )
        conn.commit()
        return db

    @classmethod
    def open(cls, directory: Path) -> "RaceDB":
        directory = Path(directory)
        db = cls(directory)
        if not db.path.exists():
            raise FileNotFoundError(f"no race database in {directory}")
        db.connection.executescript(SCHEMA)  # harmless, and upgrades old files
        db._migrate()
        return db

    def _migrate(self) -> None:
        """Bring an older race.db up to the current schema.

        CREATE TABLE IF NOT EXISTS does nothing to a table that already exists,
        so a column added after a race was timed has to be added by hand. The
        value is left null; effective_gun_time_utc decides what a null means.
        """
        conn = self.connection
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(races)")}
        if "gun_time_reader_utc" not in columns:
            logger.info("adding gun_time_reader_utc to %s", self.path)
            conn.execute("ALTER TABLE races ADD COLUMN gun_time_reader_utc INTEGER")
            conn.commit()

    # ------------------------------------------------------------------
    # race record
    # ------------------------------------------------------------------

    def info(self) -> RaceInfo:
        row = self.connection.execute(
            "SELECT name, date, distance, gun_time_utc, gun_time_reader_utc, status,"
            " min_elapsed_seconds FROM races WHERE id = ?",
            (RACE_ID,),
        ).fetchone()
        return RaceInfo(
            name=row["name"],
            date=row["date"],
            distance=row["distance"] or "",
            gun_time_utc=row["gun_time_utc"],
            gun_time_reader_utc=row["gun_time_reader_utc"],
            status=row["status"],
            min_elapsed_seconds=row["min_elapsed_seconds"],
            directory=self.directory,
        )

    def set_gun_time(self, gun_time_utc: int, reader_offset_micros: int) -> None:
        """Record the gun in both clock domains.

        gun_time_utc is what the Pi's clock said when the operator confirmed
        START, kept because it is the honest record of when the button was
        pressed. gun_time_reader_utc is that instant expressed in the reader's
        clock, and it is the one every timing rule compares reads against.
        """
        with self._write_lock:
            self.connection.execute(
                "UPDATE races SET gun_time_utc = ?, gun_time_reader_utc = ?, status = ?"
                " WHERE id = ?",
                (
                    int(gun_time_utc),
                    int(gun_time_utc) + int(reader_offset_micros),
                    STATUS_RUNNING,
                    RACE_ID,
                ),
            )
            self.connection.commit()

    def set_status(self, status: str) -> None:
        with self._write_lock:
            self.connection.execute(
                "UPDATE races SET status = ? WHERE id = ?", (status, RACE_ID)
            )
            self.connection.commit()

    # ------------------------------------------------------------------
    # participants
    # ------------------------------------------------------------------

    def add_participants(self, rows: Iterable[dict]) -> int:
        """Insert participants and their tags. Returns the number added.

        An import replaces nothing: importing twice raises rather than
        silently duplicating a field halfway through registration.
        """
        added = 0
        with self._write_lock:
            conn = self.connection
            try:
                for row in rows:
                    cursor = conn.execute(
                        "INSERT INTO participants (race_id, bib, first_name, last_name, age, gender)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            RACE_ID,
                            str(row["bib"]),
                            row.get("first_name") or "",
                            row.get("last_name") or "",
                            row.get("age"),
                            row.get("gender") or "",
                        ),
                    )
                    for epc in row.get("epcs") or []:
                        conn.execute(
                            "INSERT INTO tags (participant_id, epc) VALUES (?, ?)",
                            (cursor.lastrowid, normalize_epc(epc)),
                        )
                    added += 1
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise ParticipantImportError(f"import rejected: {exc}") from exc
            conn.commit()
        return added

    def participants(self) -> list[Participant]:
        """Participants in bib order, with their EPCs, for the timing rules."""
        rows = self.connection.execute(
            "SELECT p.id, p.bib, t.epc FROM participants p"
            " LEFT JOIN tags t ON t.participant_id = p.id"
            " WHERE p.race_id = ?"
            " ORDER BY CAST(p.bib AS INTEGER), p.bib, t.id",
            (RACE_ID,),
        ).fetchall()

        ordered: dict[int, list[str]] = {}
        bibs: dict[int, str] = {}
        for row in rows:
            ordered.setdefault(row["id"], [])
            bibs[row["id"]] = row["bib"]
            if row["epc"]:
                ordered[row["id"]].append(row["epc"])
        return [
            Participant(participant_id=pid, bib=bibs[pid], epcs=tuple(epcs))
            for pid, epcs in ordered.items()
        ]

    def participant_details(self) -> dict[int, dict]:
        """Names and demographics, keyed by participant id, for display."""
        rows = self.connection.execute(
            "SELECT id, bib, first_name, last_name, age, gender FROM participants"
            " WHERE race_id = ?",
            (RACE_ID,),
        ).fetchall()
        return {row["id"]: dict(row) for row in rows}

    def participant_count(self) -> int:
        return self.connection.execute(
            "SELECT COUNT(*) FROM participants WHERE race_id = ?", (RACE_ID,)
        ).fetchone()[0]

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def append_reads(self, reads: Sequence[TagRead]) -> int:
        """Write raw reads. Nothing filters or interprets them on the way in."""
        if not reads:
            return 0
        with self._write_lock:
            self.connection.executemany(
                "INSERT INTO reads (race_id, epc, antenna_port, first_seen_utc, rssi)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (RACE_ID, r.epc, r.antenna_port, r.first_seen_utc, r.rssi)
                    for r in reads
                ],
            )
            self.connection.commit()
        return len(reads)

    def reads(self) -> list[TagRead]:
        rows = self.connection.execute(
            "SELECT epc, antenna_port, first_seen_utc, rssi FROM reads"
            " WHERE race_id = ? ORDER BY first_seen_utc, id",
            (RACE_ID,),
        ).fetchall()
        return [
            TagRead(
                epc=row["epc"],
                antenna_port=row["antenna_port"],
                first_seen_utc=row["first_seen_utc"],
                rssi=row["rssi"] if row["rssi"] is not None else 0.0,
            )
            for row in rows
        ]

    def read_count(self) -> int:
        return self.connection.execute(
            "SELECT COUNT(*) FROM reads WHERE race_id = ?", (RACE_ID,)
        ).fetchone()[0]

    def last_read_utc(self) -> int | None:
        return self.connection.execute(
            "SELECT MAX(first_seen_utc) FROM reads WHERE race_id = ?", (RACE_ID,)
        ).fetchone()[0]

    def seen_epcs(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT DISTINCT epc FROM reads WHERE race_id = ?", (RACE_ID,)
        ).fetchall()
        return {row["epc"] for row in rows}

    # ------------------------------------------------------------------
    # results
    # ------------------------------------------------------------------

    def save_results(self, results: Iterable[ParticipantResult]) -> None:
        """Replace the stored results. Derived data, safe to overwrite."""
        with self._write_lock:
            conn = self.connection
            conn.execute("DELETE FROM results WHERE race_id = ?", (RACE_ID,))
            conn.executemany(
                "INSERT INTO results (race_id, participant_id, start_utc, finish_utc,"
                " elapsed_seconds, status) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        RACE_ID,
                        r.participant_id,
                        r.start_utc,
                        r.finish_utc,
                        r.elapsed_seconds,
                        r.status,
                    )
                    for r in results
                ],
            )
            conn.commit()

    def results(self) -> list[ParticipantResult]:
        """Stored results, in bib order."""
        rows = self.connection.execute(
            "SELECT r.participant_id, p.bib, r.start_utc, r.finish_utc,"
            " r.elapsed_seconds, r.status"
            " FROM results r JOIN participants p ON p.id = r.participant_id"
            " WHERE r.race_id = ?"
            " ORDER BY CAST(p.bib AS INTEGER), p.bib",
            (RACE_ID,),
        ).fetchall()
        return [
            ParticipantResult(
                participant_id=row["participant_id"],
                bib=row["bib"],
                start_utc=row["start_utc"],
                finish_utc=row["finish_utc"],
                elapsed_seconds=row["elapsed_seconds"],
                status=row["status"],
            )
            for row in rows
        ]

    def recompute(self) -> list[ParticipantResult]:
        """Rebuild the results table from the raw read log.

        This is the authoritative path. Live processing is an optimisation of
        it, and the test suite holds the two to the same answer.
        """
        info = self.info()
        results = compute_results(
            self.reads(),
            self.participants(),
            info.effective_gun_time_utc,
            info.min_elapsed_seconds,
        )
        self.save_results(results)
        return results

    # ------------------------------------------------------------------
    # export
    # ------------------------------------------------------------------

    def export_csv(self, results: Sequence[ParticipantResult] | None = None) -> Path:
        """Write results next to the database and return the path.

        The filename carries a timestamp so an export taken at 20 minutes and
        another at the finish sit side by side. This only reads from SQLite,
        so it can run mid race without touching the reader loop.
        """
        if results is None:
            results = self.results()
        details = self.participant_details()
        place_by_participant = places(results)

        # Finishers in order, then everyone else by bib, so the top of the file
        # is the result and the tail is the exceptions to chase down.
        def sort_key(result: ParticipantResult):
            place = place_by_participant.get(result.participant_id)
            if place is not None:
                return (0, place, "")
            bib = details.get(result.participant_id, {}).get("bib", "")
            return (1, 0, f"{int(bib):09d}" if str(bib).isdigit() else str(bib))

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.directory / f"results_{stamp}.csv"
        # Two exports inside the same second still get their own file.
        suffix = 2
        while path.exists():
            path = self.directory / f"results_{stamp}-{suffix}.csv"
            suffix += 1
        # newline="" is required or csv writes \r\r\n on Windows.
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(CSV_COLUMNS)
            for result in sorted(results, key=sort_key):
                person = details.get(result.participant_id, {})
                place = place_by_participant.get(result.participant_id)
                writer.writerow(
                    [
                        place if place is not None else "",
                        person.get("bib", result.bib),
                        person.get("first_name", ""),
                        person.get("last_name", ""),
                        person.get("age") if person.get("age") is not None else "",
                        person.get("gender", ""),
                        format_clock(result.start_utc),
                        format_clock(result.finish_utc),
                        format_elapsed(result.elapsed_seconds),
                        result.status,
                    ]
                )
        return path

    def exports(self) -> list[Path]:
        """Every export taken for this race, newest first."""
        return sorted(self.directory.glob("results_*.csv"), reverse=True)


# ----------------------------------------------------------------------
# archive
# ----------------------------------------------------------------------


def list_races(root: Path = RACES_ROOT) -> list[dict]:
    """Every race on disk, newest date first, for the launch screen.

    A directory that is not a readable race is skipped rather than crashing
    the archive: a half copied folder should not take the console down on
    race morning.
    """
    root = Path(root)
    if not root.exists():
        return []

    races = []
    for directory in sorted(root.iterdir(), reverse=True):
        if not directory.is_dir() or not (directory / "race.db").exists():
            continue
        try:
            db = RaceDB.open(directory)
            info = db.info()
            finishers = db.connection.execute(
                "SELECT COUNT(*) FROM results WHERE race_id = ? AND status = 'finished'",
                (RACE_ID,),
            ).fetchone()[0]
            registered = db.participant_count()
            db.close()
        except (sqlite3.DatabaseError, FileNotFoundError, TypeError):
            continue
        races.append(
            {
                "slug": info.slug,
                "name": info.name,
                "date": info.date,
                "distance": info.distance,
                "status": info.status,
                "registered": registered,
                "finishers": finishers,
            }
        )
    races.sort(key=lambda item: (item["date"], item["name"]), reverse=True)
    return races


def open_race(slug: str, root: Path = RACES_ROOT) -> RaceDB:
    """Open a race by folder name, refusing anything outside races/."""
    directory = (Path(root) / slug).resolve()
    if Path(root).resolve() not in directory.parents:
        raise ValueError("race is outside the races directory")
    return RaceDB.open(directory)


__all__ = [
    "CSV_COLUMNS",
    "ParticipantImportError",
    "RACES_ROOT",
    "RaceDB",
    "RaceInfo",
    "STATUS_FINISHED",
    "STATUS_RUNNING",
    "STATUS_SETUP",
    "format_clock",
    "list_races",
    "open_race",
    "parse_participant_csv",
    "slugify",
]
