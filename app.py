"""Flask server and the live race session.

Run it:

    python app.py --simulate                  demo with no hardware
    python app.py --reader-host 192.168.1.50  a real R420

The server holds at most one live session, because there is one reader. Any
other race can still be opened at the same time, read only, to review or
re-export it.

Threading, in one paragraph: the reader loop runs in its own thread, writes
every read straight to SQLite, and recomputes results from its own in memory
copy of the read log. Flask request threads never touch that list; they ask
for a snapshot under a lock and get a plain dict back. Exports read the
database through a separate connection, so pressing EXPORT during the race
cannot stall the reads.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from pathlib import Path

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

import db as racedb
from db import RACES_ROOT, ParticipantImportError, RaceDB
from reader import ALL_ANTENNAS, LLRP_DEFAULT_PORT, LLRPReader, Reader
from simulator import SimulatedReader, participants_from_rows
from timing import (
    STATUS_FINISHED,
    compute_results,
    format_elapsed,
    places,
    summarize,
)

logger = logging.getLogger(__name__)

MICROS = 1_000_000

# How often the reader thread rebuilds results, and how often it persists them.
# Recomputing is cheap and gives the operator a live screen; writing the
# results table is the part worth rationing.
RECOMPUTE_INTERVAL = 1.0
PERSIST_INTERVAL = 10.0
FLUSH_INTERVAL = 0.25
FLUSH_SIZE = 200


def now_utc() -> int:
    return int(time.time() * MICROS)


class LiveSession:
    """One race, one reader, one consumer thread."""

    def __init__(self, race: RaceDB, reader: Reader, mode: str):
        self.race = race
        self.reader = reader
        self.mode = mode
        self.slug = race.directory.name

        self._lock = threading.Lock()
        self._reads: list = []
        self._results: list = []
        self._error: str | None = None
        self._stopping = threading.Event()
        self._read_count = 0
        self._last_read_utc: int | None = None
        self._dirty = True

        # Reads already on disk count towards the live picture, so a restarted
        # session mid race picks up where it left off instead of showing an
        # empty screen with everyone unstarted.
        self._reads = race.reads()
        self._read_count = len(self._reads)

        self.thread = threading.Thread(target=self._run, name="reader", daemon=True)
        self.thread.start()

    # ------------------------------------------------------------------

    def _run(self) -> None:
        pending: list = []
        last_flush = time.monotonic()
        last_recompute = 0.0
        last_persist = time.monotonic()
        try:
            for read in self.reader.reads():
                if self._stopping.is_set():
                    break
                pending.append(read)
                now = time.monotonic()

                if len(pending) >= FLUSH_SIZE or now - last_flush >= FLUSH_INTERVAL:
                    self._flush(pending)
                    pending = []
                    last_flush = now

                if now - last_recompute >= RECOMPUTE_INTERVAL:
                    self._refresh(persist=now - last_persist >= PERSIST_INTERVAL)
                    if now - last_persist >= PERSIST_INTERVAL:
                        last_persist = now
                    last_recompute = now
        except Exception as exc:  # a dead reader must not take the console down
            logger.exception("reader loop failed")
            with self._lock:
                self._error = str(exc)
        finally:
            if pending:
                self._flush(pending)
            self._refresh(persist=True)

    def _flush(self, pending: list) -> None:
        """Raw reads to disk first, then into the live picture."""
        self.race.append_reads(pending)
        with self._lock:
            self._reads.extend(pending)
            self._read_count = len(self._reads)
            self._last_read_utc = max(r.first_seen_utc for r in pending)
            self._dirty = True

    def _refresh(self, persist: bool = False) -> None:
        with self._lock:
            if not self._dirty and not persist:
                return
            reads = list(self._reads)
            self._dirty = False

        info = self.race.info()
        results = compute_results(
            reads,
            self.race.participants(),
            info.gun_time_utc,
            info.min_elapsed_seconds,
        )
        with self._lock:
            self._results = results
        if persist:
            self.race.save_results(results)

    # ------------------------------------------------------------------

    def fire_gun(self) -> int:
        gun = now_utc()
        self.race.set_gun_time(gun)
        # The simulator needs to know; a real reader is already running and
        # does not care when the operator pressed the button.
        trigger = getattr(self.reader, "trigger_start", None)
        if callable(trigger):
            trigger(gun)
        with self._lock:
            self._dirty = True
        self._refresh()
        return gun

    def snapshot(self) -> tuple[list, int, int | None, str | None]:
        with self._lock:
            return list(self._results), self._read_count, self._last_read_utc, self._error

    def persist(self) -> list:
        """Force results to disk and return them. Used before an export."""
        self._refresh(persist=True)
        results, _, _, _ = self.snapshot()
        return results

    def stop(self) -> None:
        self._stopping.set()
        self.reader.stop()
        self.thread.join(timeout=5.0)
        self._refresh(persist=True)


class Console:
    """Application state: the races on disk plus at most one live session."""

    def __init__(self, root: Path, simulate: bool, reader_host: str, reader_port: int,
                 tx_power_dbm: float, sim_speed: float, sim_seed: int):
        self.root = Path(root)
        self.simulate = simulate
        self.reader_host = reader_host
        self.reader_port = reader_port
        self.tx_power_dbm = tx_power_dbm
        self.sim_speed = sim_speed
        self.sim_seed = sim_seed
        self.session: LiveSession | None = None
        self._lock = threading.Lock()

    @property
    def mode(self) -> str:
        return "simulate" if self.simulate else "reader"

    def open_race(self, slug: str) -> RaceDB:
        try:
            return racedb.open_race(slug, self.root)
        except (FileNotFoundError, ValueError):
            abort(404)

    def build_reader(self, race: RaceDB) -> Reader:
        if self.simulate:
            people = participants_from_rows(
                [{"bib": p.bib, "epcs": list(p.epcs)} for p in race.participants()]
            )
            if not people:
                raise ValueError("import participants before starting simulate mode")
            return SimulatedReader(people, seed=self.sim_seed, speed=self.sim_speed)
        return LLRPReader(
            host=self.reader_host,
            port=self.reader_port,
            antennas=ALL_ANTENNAS,
            tx_power_dbm=self.tx_power_dbm,
        )

    def go_live(self, slug: str) -> LiveSession:
        with self._lock:
            if self.session is not None:
                if self.session.slug == slug:
                    return self.session
                raise ValueError(
                    f"the reader is already attached to {self.session.slug}; stop that race first"
                )
            race = self.open_race(slug)
            session = LiveSession(race, self.build_reader(race), self.mode)
            self.session = session
            return session

    def stop_live(self) -> None:
        with self._lock:
            if self.session is None:
                return
            self.session.stop()
            self.session.race.set_status(racedb.STATUS_FINISHED)
            self.session = None

    def session_for(self, slug: str) -> LiveSession | None:
        session = self.session
        return session if session is not None and session.slug == slug else None


# ----------------------------------------------------------------------
# view helpers
# ----------------------------------------------------------------------


def _full_name(person: dict) -> str:
    return " ".join(part for part in (person.get("first_name"), person.get("last_name")) if part)


def build_state(race: RaceDB, session: LiveSession | None) -> dict:
    """Everything the page polls for, in one payload."""
    info = race.info()

    if session is not None:
        results, read_count, last_read_utc, error = session.snapshot()
    else:
        results = race.results()
        read_count = race.read_count()
        last_read_utc = race.last_read_utc()
        error = None

    details = race.participant_details()
    place_by_participant = places(results)
    seen = race.seen_epcs() if session is None else None

    finishers = [
        {
            "place": place_by_participant.get(r.participant_id),
            "bib": r.bib,
            "name": _full_name(details.get(r.participant_id, {})),
            "elapsed": format_elapsed(r.elapsed_seconds),
            "elapsed_seconds": r.elapsed_seconds,
            "finish_utc": r.finish_utc,
        }
        for r in results
        if r.status == STATUS_FINISHED
    ]
    # Newest across the line at the top: that is where the operator looks.
    finishers.sort(key=lambda item: item["finish_utc"], reverse=True)

    participants = [
        {
            "bib": r.bib,
            "name": _full_name(details.get(r.participant_id, {})),
            "age": details.get(r.participant_id, {}).get("age"),
            "gender": details.get(r.participant_id, {}).get("gender", ""),
            "status": r.status,
            "place": place_by_participant.get(r.participant_id),
            "start": racedb.format_clock(r.start_utc),
            "finish": racedb.format_clock(r.finish_utc),
            "elapsed": format_elapsed(r.elapsed_seconds),
        }
        for r in results
    ]
    if not results:
        # Before the gun there are no results yet, but registration should
        # still be visible for spot checking.
        participants = [
            {
                "bib": person["bib"],
                "name": _full_name(person),
                "age": person.get("age"),
                "gender": person.get("gender", ""),
                "status": "not_started",
                "place": None,
                "start": "",
                "finish": "",
                "elapsed": "",
            }
            for person in sorted(
                details.values(),
                key=lambda p: (len(str(p["bib"])), str(p["bib"])),
            )
        ]

    return {
        "race": {
            "slug": info.slug,
            "name": info.name,
            "date": info.date,
            "distance": info.distance,
            "status": info.status,
            "gun_time_utc": info.gun_time_utc,
            "min_elapsed_seconds": info.min_elapsed_seconds,
        },
        "live": session is not None,
        "reader": {
            "mode": session.mode if session else None,
            "read_count": read_count,
            "last_read_utc": last_read_utc,
            "error": error,
        },
        "summary": summarize(results) if results else {
            "registered": len(details),
            "started": 0,
            "finished": 0,
            "on_course": 0,
            "review": 0,
            "not_started": len(details),
        },
        "finishers": finishers,
        "participants": participants,
        "unseen_epcs": None if seen is None else len(seen),
        "server_now_utc": now_utc(),
        "exports": [path.name for path in race.exports()],
    }


# ----------------------------------------------------------------------
# app
# ----------------------------------------------------------------------


def create_app(console: Console) -> Flask:
    app = Flask(__name__)
    app.console = console

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            races=racedb.list_races(console.root),
            mode=console.mode,
            reader_host=console.reader_host,
            live_slug=console.session.slug if console.session else None,
        )

    @app.post("/races")
    def create_race():
        name = (request.form.get("name") or "").strip()
        date = (request.form.get("date") or "").strip()
        if not name or not date:
            return redirect(url_for("index"))
        distance = (request.form.get("distance") or "").strip()
        try:
            min_elapsed = float(request.form.get("min_elapsed_seconds") or 720)
        except ValueError:
            min_elapsed = 720.0
        race = RaceDB.create(name, date, distance, min_elapsed, root=console.root)
        slug = race.directory.name
        race.close()
        return redirect(url_for("race_console", slug=slug))

    @app.get("/races/<slug>")
    def race_console(slug):
        race = console.open_race(slug)
        info = race.info()
        return render_template(
            "race.html",
            race=info,
            slug=slug,
            live=console.session_for(slug) is not None,
            other_race_live=console.session is not None and console.session.slug != slug,
            live_slug=console.session.slug if console.session else None,
            mode=console.mode,
        )

    @app.post("/races/<slug>/participants")
    def import_participants(slug):
        race = console.open_race(slug)
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return redirect(url_for("race_console", slug=slug) + "?error=no+file+chosen")
        try:
            text = upload.read().decode("utf-8-sig")
            rows = racedb.parse_participant_csv(text)
            added = race.add_participants(rows)
        except UnicodeDecodeError:
            return redirect(url_for("race_console", slug=slug) + "?error=file+is+not+text")
        except ParticipantImportError as exc:
            return redirect(url_for("race_console", slug=slug) + f"?error={exc}")
        return redirect(url_for("race_console", slug=slug) + f"?imported={added}")

    @app.post("/races/<slug>/live")
    def go_live(slug):
        try:
            console.go_live(slug)
        except ValueError as exc:
            return redirect(url_for("race_console", slug=slug) + f"?error={exc}")
        except Exception as exc:  # reader refused to connect
            logger.exception("could not start the reader")
            return redirect(url_for("race_console", slug=slug) + f"?error=reader: {exc}")
        return redirect(url_for("race_console", slug=slug))

    @app.post("/races/<slug>/stop")
    def stop_live(slug):
        if console.session_for(slug) is not None:
            console.stop_live()
        return redirect(url_for("race_console", slug=slug))

    @app.post("/races/<slug>/start")
    def fire_gun(slug):
        session = console.session_for(slug)
        if session is None:
            return jsonify({"ok": False, "error": "the reader is not attached to this race"}), 409
        if session.race.info().gun_time_utc is not None:
            return jsonify({"ok": False, "error": "the gun has already been fired"}), 409
        gun = session.fire_gun()
        return jsonify({"ok": True, "gun_time_utc": gun})

    @app.get("/api/races/<slug>/state")
    def race_state(slug):
        race = console.open_race(slug)
        return jsonify(build_state(race, console.session_for(slug)))

    @app.post("/races/<slug>/export")
    def export(slug):
        race = console.open_race(slug)
        session = console.session_for(slug)
        results = session.persist() if session is not None else race.results()
        path = race.export_csv(results)
        return jsonify({"ok": True, "file": path.name})

    @app.get("/races/<slug>/exports/<filename>")
    def download_export(slug, filename):
        race = console.open_race(slug)
        if not filename.startswith("results_") or not filename.endswith(".csv"):
            abort(404)
        return send_from_directory(race.directory, filename, as_attachment=True)

    @app.post("/races/<slug>/recompute")
    def recompute(slug):
        race = console.open_race(slug)
        if console.session_for(slug) is not None:
            return jsonify({"ok": False, "error": "stop the reader before recomputing"}), 409
        results = race.recompute()
        return jsonify({"ok": True, "results": len(results)})

    return app


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Easymoose race timing")
    parser.add_argument("--simulate", action="store_true",
                        help="use the simulated reader instead of hardware")
    parser.add_argument("--reader-host", default="192.168.1.50",
                        help="IP address of the Speedway R420")
    parser.add_argument("--reader-port", type=int, default=LLRP_DEFAULT_PORT)
    parser.add_argument("--tx-power", type=float, default=30.0,
                        help="reader transmit power in dBm")
    parser.add_argument("--host", default="0.0.0.0",
                        help="address the web server binds to")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--races-dir", default=str(RACES_ROOT))
    parser.add_argument("--sim-speed", type=float, default=60.0,
                        help="simulate mode playback speed multiplier")
    parser.add_argument("--sim-seed", type=int, default=1)
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    root = Path(args.races_dir)
    root.mkdir(parents=True, exist_ok=True)

    console = Console(
        root=root,
        simulate=args.simulate,
        reader_host=args.reader_host,
        reader_port=args.reader_port,
        tx_power_dbm=args.tx_power,
        sim_speed=args.sim_speed,
        sim_seed=args.sim_seed,
    )
    app = create_app(console)

    mode = "SIMULATE (no hardware)" if args.simulate else f"reader at {args.reader_host}"
    print(f"Easymoose race timing, {mode}")
    print(f"Open http://localhost:{args.port}/  (or this machine's IP from a phone on the same network)")
    try:
        app.run(host=args.host, port=args.port, debug=args.debug, threaded=True,
                use_reloader=False)
    finally:
        console.stop_live()


if __name__ == "__main__":
    main()
