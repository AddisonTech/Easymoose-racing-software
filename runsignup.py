"""Results to RunSignup, five minutes after they settle.

The board at the timing tent is live; RunSignup is not. RunSignup emails or
texts a runner the moment their result lands, so a result is only sent once it
has gone SETTLE_SECONDS without changing. A bib swap fixed at the tent inside
that window never reaches anyone's phone. A correction made later is sent the
same way, as an update to the result already there, so nobody is listed twice.

Each finisher goes to one result set by category: juniors (12 and under) to
the junior set, everyone else to the adult set. Places are counted within the
set by gun time, because overall places and awards go by gun time. Both times
are always sent: clock_time is the gun time, chip_time the runner's own start
to finish. A runner timed from the gun, with no start read, has the same value
in both.

Credentials come from .env next to this file, which git ignores:
    RUNSIGNUP_API_KEY=...
    RUNSIGNUP_API_SECRET=...
and, once RunSignup requires API caller registration (1 January 2027):
    RUNSIGNUP_API_REG=...
    RUNSIGNUP_API_REG_SECRET=...
Secrets go in headers, never in the URL.

The uploader only reads results. It never touches the reader loop, and a
failed send is retried on the next pass.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import db as racedb
from timing import (
    CATEGORY_JUNIOR,
    STATUS_FINISHED,
    category,
    order_finishers,
)

logger = logging.getLogger(__name__)

API = "https://api.runsignup.com/rest/race/{race_id}/results/{action}"
ENV_FILE = Path(__file__).resolve().parent / ".env"
SETTLE_SECONDS = 300
POLL_SECONDS = 15
MICROS = 1_000_000
PAGE_SIZE = 100


class RunSignupError(RuntimeError):
    pass


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
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


def clock_text(seconds: float) -> str:
    """Seconds as H:MM:SS.ss, the form RunSignup takes."""
    hundredths = int(round(seconds * 100))
    whole, hundredths = divmod(hundredths, 100)
    minutes, secs = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}.{hundredths:02d}"


class Client:
    """The two results calls the uploader and the repair tool need."""

    def __init__(self, key: str, secret: str, reg: str = "", reg_secret: str = "",
                 opener=urllib.request.urlopen):
        self.key, self.secret = key, secret
        self.reg, self.reg_secret = reg, reg_secret
        self.opener = opener

    @classmethod
    def from_env(cls, path: Path = ENV_FILE) -> "Client":
        env = load_env(path)

        def value(name: str) -> str:
            return os.environ.get(name) or env.get(name, "")

        key, secret = value("RUNSIGNUP_API_KEY"), value("RUNSIGNUP_API_SECRET")
        if not key or not secret:
            raise RunSignupError(f"RUNSIGNUP_API_KEY and RUNSIGNUP_API_SECRET must be set in {path}")
        return cls(key, secret, value("RUNSIGNUP_API_REG"), value("RUNSIGNUP_API_REG_SECRET"))

    def call(self, race_id: int, action: str, query: dict, form: dict | None = None) -> dict:
        params = {**query, "rsu_api_key": self.key, "format": "json"}
        headers = {"X-RSU-API-SECRET": self.secret}
        if self.reg and self.reg_secret:
            params["rsu_api_reg"] = self.reg
            headers["X-RSU-API-REG-SECRET"] = self.reg_secret
        url = API.format(race_id=race_id, action=action) + "?" + urllib.parse.urlencode(params)
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with self.opener(request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RunSignupError(
                f"{action}: HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:300]}"
            ) from None
        if isinstance(body, dict) and "error" in body:
            raise RunSignupError(f"{action}: {json.dumps(body['error'])}")
        return body

    def get_results(self, race_id: int, event_id: int, result_set_id: int) -> list[dict]:
        results: list[dict] = []
        page = 1
        while True:
            body = self.call(race_id, "get-results", {
                "event_id": event_id,
                "individual_result_set_id": result_set_id,
                "results_per_page": PAGE_SIZE,
                "page": page,
            })
            sets = [s for s in body.get("individual_results_sets", [])
                    if int(s.get("individual_result_set_id", 0)) == result_set_id]
            if not sets:
                raise RunSignupError(f"result set {result_set_id} not found for event {event_id}")
            batch = sets[0].get("results", [])
            results.extend(batch)
            if len(batch) < PAGE_SIZE:
                return results
            page += 1

    def post_results(self, race_id: int, event_id: int, result_set_id: int,
                     results: list[dict]) -> list[int | None]:
        """Add or update results; returns each one's result_id, in order."""
        body = self.call(race_id, "full-results", {}, {
            "event_id": event_id,
            "individual_result_set_id": result_set_id,
            "request_format": "json",
            "request": json.dumps({"results": results}),
        })
        returned = body.get("results") if isinstance(body, dict) else None
        if isinstance(returned, list) and len(returned) == len(results):
            ids = [item.get("result_id") if isinstance(item, dict) else None for item in returned]
            if all(ids):
                return [int(i) for i in ids]
        # No ids in the reply: read the set back and match by bib.
        by_bib = {str(r.get("bib")): r.get("result_id")
                  for r in self.get_results(race_id, event_id, result_set_id)}
        return [by_bib.get(str(r["bib_num"])) for r in results]


def wanted(race: racedb.RaceDB, results: list) -> tuple[dict[int, tuple], list[str]]:
    """What RunSignup should show for each finisher.

    Returns {participant_id: (result_set_id, event_id, payload)} and the
    finishers that cannot be sent, as messages for the console.
    """
    settings = race.runsignup_settings()
    gun = race.info().effective_gun_time_utc
    details = race.participant_details()
    targets = {
        "adult": (settings["adult_result_set_id"], settings["adult_event_id"]),
        "junior": (settings["junior_result_set_id"], settings["junior_event_id"]),
    }
    out: dict[int, tuple] = {}
    problems: list[str] = []
    place_in_set: dict[int, int] = {}
    for result in order_finishers(r for r in results if r.status == STATUS_FINISHED):
        person = details.get(result.participant_id, {})
        group = "junior" if category(person.get("age"), person.get("gender")) == CATEGORY_JUNIOR else "adult"
        result_set_id, event_id = targets[group]
        if not result_set_id or not event_id:
            problems.append(f"bib {result.bib}: no {group} result set set up")
            continue
        if not str(result.bib).isdigit():
            problems.append(f"bib {result.bib}: RunSignup bibs are numbers")
            continue
        place_in_set[result_set_id] = place_in_set.get(result_set_id, 0) + 1
        clock_seconds = result.gun_seconds
        if clock_seconds is None:
            clock_seconds = ((result.finish_utc - gun) / MICROS
                             if gun is not None and result.finish_utc else result.elapsed_seconds)
        payload = {
            "bib_num": int(result.bib),
            "place": place_in_set[result_set_id],
            "clock_time": clock_text(clock_seconds),
            "chip_time": clock_text(result.elapsed_seconds),
        }
        out[result.participant_id] = (result_set_id, event_id, payload)
    return out, problems


class Uploader:
    """Sends settled results for every race with sending turned on."""

    def __init__(self, console, client: Client | None = None, settle_seconds: float = SETTLE_SECONDS,
                 clock=time.time):
        self.console = console
        self.client = client
        self.settle_seconds = settle_seconds
        self.clock = clock
        self._since: dict[tuple[str, int], tuple[str, float]] = {}
        self._status: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="runsignup", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            for entry in racedb.list_races(self.console.root):
                try:
                    race = racedb.open_race(entry["slug"], self.console.root)
                    if race.runsignup_settings()["enabled"]:
                        self.cycle(race)
                except Exception:  # one bad race folder must not stop the others
                    logger.exception("runsignup pass failed for %s", entry.get("slug"))

    def status(self, slug: str) -> dict:
        with self._lock:
            return dict(self._status.get(slug, {}))

    def cycle(self, race: racedb.RaceDB) -> dict:
        """One pass over one race: send whatever has settled."""
        slug = race.directory.name
        settings = race.runsignup_settings()
        now = self.clock()
        want, problems = wanted(race, self.console.current_results(race))
        sent = race.runsignup_sent()

        ready: dict[tuple[int, int], list[tuple[int, dict, str]]] = {}
        waiting = []
        for participant_id, (result_set_id, event_id, payload) in want.items():
            text = json.dumps(payload, sort_keys=True)
            previous = sent.get(participant_id)
            key = (slug, participant_id)
            if previous and previous["payload"] == text and previous["result_set_id"] == result_set_id:
                self._since.pop(key, None)
                continue
            since = self._since.get(key)
            if since is None or since[0] != text:
                self._since[key] = (text, now)
                since = self._since[key]
            if now - since[1] < self.settle_seconds:
                waiting.append(since[1] + self.settle_seconds - now)
                continue
            body = dict(payload)
            if previous and previous["result_set_id"] == result_set_id:
                body["result_id"] = previous["result_id"]
            elif previous:
                problems.append(f"bib {payload['bib_num']} moved result sets; "
                                "delete it from the old set on RunSignup")
            ready.setdefault((result_set_id, event_id), []).append((participant_id, body, text))

        for participant_id in sent:
            if participant_id not in want:
                bib = race.connection.execute(
                    "SELECT bib FROM participants WHERE id = ?", (participant_id,)
                ).fetchone()
                problems.append(f"bib {bib[0] if bib else participant_id} is on RunSignup but is "
                                "no longer a finisher here; remove it on RunSignup")

        error = None
        sent_now = 0
        for (result_set_id, event_id), batch in ready.items():
            try:
                client = self.client or Client.from_env()
                ids = client.post_results(settings["race_id"], event_id, result_set_id,
                                          [body for _, body, _ in batch])
            except (RunSignupError, OSError, ValueError) as exc:
                error = str(exc)
                logger.warning("runsignup send failed: %s", exc)
                continue
            rows = []
            for (participant_id, body, text), result_id in zip(batch, ids):
                if result_id is None:
                    problems.append(f"bib {body['bib_num']} was sent but RunSignup returned no result id")
                    continue
                rows.append((participant_id, result_set_id, int(result_id), text))
                self._since.pop((slug, participant_id), None)
            race.record_runsignup_sent(rows, int(now * MICROS))
            sent_now += len(rows)

        status = {
            "sent": len(race.runsignup_sent()),
            "waiting": len(waiting),
            "next_send_seconds": int(min(waiting)) if waiting else None,
            "sent_this_pass": sent_now,
            "error": error,
            "problems": problems,
            "checked_utc": int(now * MICROS),
        }
        with self._lock:
            self._status[slug] = status
        return status


def status_for(race: racedb.RaceDB, uploader: Uploader | None) -> dict:
    """RunSignup settings and progress for the console."""
    settings = race.runsignup_settings()
    status = uploader.status(race.directory.name) if uploader is not None else {}
    return {
        "settings": settings,
        "running": uploader is not None,
        "settle_seconds": int(uploader.settle_seconds) if uploader is not None else SETTLE_SECONDS,
        **status,
    }
