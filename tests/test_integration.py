"""The whole system: a generated race, the live path, and the web endpoints.

The important test in here is the one holding live processing and recompute to
the same answer. Live results are what the operator reads off the screen and
recompute is what gets published afterwards, and the two disagreeing would be
the worst class of bug this software could have.
"""

from __future__ import annotations

import csv
import io
import threading
import time

import pytest
from conftest import GUN, csv_for

from app import Console, LiveSession, create_app
from db import RaceDB, parse_participant_csv
from reader import FINISH_ANTENNAS, START_ANTENNAS, Reader
from simulator import SimulatedReader, make_participants
from timing import STATUS_FINISHED, Participant, compute_results


class ReplayReader(Reader):
    """Hands over a fixed read log as fast as the consumer will take it."""

    def __init__(self, reads):
        self._reads = list(reads)
        self._stopped = threading.Event()

    def reads(self):
        for item in self._reads:
            if self._stopped.is_set():
                return
            yield item

    def stop(self):
        self._stopped.set()


@pytest.fixture
def loaded_race(tmp_path):
    """A race with a field imported and a generated read log ready to play."""
    people = make_participants(30, seed=11)
    sim = SimulatedReader(people, seed=11)
    race = RaceDB.create("Generated 5K", "2026-08-12", "5K", 720, root=tmp_path / "races")
    race.add_participants(parse_participant_csv(csv_for(people)))
    yield race, people, sim
    race.close()


# ------------------------------------------------------- rules over a field


def test_the_generated_race_produces_the_results_it_planted(sim_race):
    sim, people, reads = sim_race
    participants = [
        Participant(index, person.bib, tuple(person.epcs))
        for index, person in enumerate(people)
    ]
    results = compute_results(reads, participants, GUN)

    by_bib = {r.bib: r for r in results}
    for bib in sim.expected_dnf_bibs:
        assert by_bib[bib].status == "dnf", f"bib {bib} should be a DNF"
    for bib in sim.expected_review_bibs:
        assert by_bib[bib].status == "review", f"bib {bib} should need review"

    # Nobody finishes in under the minimum elapsed, and nobody takes all day.
    finished = [r for r in results if r.status == STATUS_FINISHED]
    assert all(720 <= r.elapsed_seconds <= 5400 for r in finished)
    assert len(finished) >= len(people) * 0.8


def test_every_non_finisher_is_explained_by_the_read_log(sim_race):
    """Nobody is dropped for a reason that is not in the reads.

    The planted DNFs and missed start are not the only exceptions: the
    simulator also fails a tag outright now and then, and a runner carrying
    one tag that fails at a line genuinely has no crossing there. What must
    never happen is a non-finisher who had reads at both lines.
    """
    sim, people, reads = sim_race
    participants = [
        Participant(index, person.bib, tuple(person.epcs))
        for index, person in enumerate(people)
    ]
    results = compute_results(reads, participants, GUN)

    by_epc = {}
    for item in reads:
        by_epc.setdefault(item.epc, []).append(item)

    planted = set(sim.expected_dnf_bibs) | set(sim.expected_review_bibs)
    for result, person in zip(results, people):
        if result.status == STATUS_FINISHED or result.bib in planted:
            continue
        owned = [item for epc in person.epcs for item in by_epc.get(epc, [])]
        starts = [
            item for item in owned
            if item.antenna_port in START_ANTENNAS and item.first_seen_utc >= GUN
        ]
        finishes = [item for item in owned if item.antenna_port in FINISH_ANTENNAS]
        assert not starts or not finishes, (
            f"bib {result.bib} was read at both lines but came out {result.status}"
        )


def test_stray_finish_reads_never_become_results(sim_race):
    _, people, reads = sim_race
    participants = [
        Participant(index, person.bib, tuple(person.epcs))
        for index, person in enumerate(people)
    ]
    for result in compute_results(reads, participants, GUN):
        if result.status == STATUS_FINISHED:
            assert result.elapsed_seconds >= 720


# ------------------------------------------------- live processing vs recompute


def test_live_results_match_a_recompute_from_the_read_log(loaded_race):
    race, people, sim = loaded_race
    race.set_gun_time(GUN, 0)

    session = LiveSession(race, ReplayReader(sim.generate(GUN)), "replay")
    session.thread.join(timeout=60)
    assert not session.thread.is_alive(), "the reader thread did not finish"
    live_results, read_count, _, error = session.snapshot()

    assert error is None
    assert read_count == len(sim.generate(GUN))
    assert race.read_count() == read_count

    assert live_results == race.recompute()
    assert any(r.status == STATUS_FINISHED for r in live_results)


def test_a_session_restarted_mid_race_keeps_the_reads_already_on_disk(loaded_race):
    race, people, sim = loaded_race
    race.set_gun_time(GUN, 0)
    all_reads = sim.generate(GUN)
    half = len(all_reads) // 2

    first = LiveSession(race, ReplayReader(all_reads[:half]), "replay")
    first.thread.join(timeout=60)
    first.stop()

    second = LiveSession(race, ReplayReader(all_reads[half:]), "replay")
    second.thread.join(timeout=60)
    second.stop()

    assert race.read_count() == len(all_reads)
    assert second.snapshot()[0] == race.recompute()


# ---------------------------------------------------------------- endpoints


@pytest.fixture
def client(tmp_path):
    console = Console(
        root=tmp_path / "races",
        simulate=True,
        reader_host="127.0.0.1",
        reader_port=5084,
        tx_power_dbm=30.0,
        sim_speed=20000.0,
        sim_seed=3,
    )
    console.root.mkdir(parents=True, exist_ok=True)
    app = create_app(console)
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client, console


def make_race(client, name="Turkey Trot", date="2026-11-26"):
    response = client.post("/races", data={"name": name, "date": date,
                                           "distance": "5K", "min_elapsed_seconds": "720"})
    assert response.status_code == 302
    return response.headers["Location"].rsplit("/", 1)[-1]


def import_field(client, slug, people):
    data = {"file": (io.BytesIO(csv_for(people).encode()), "participants.csv")}
    response = client.post(f"/races/{slug}/participants", data=data,
                           content_type="multipart/form-data")
    assert response.status_code == 302
    return response.headers["Location"]


def test_the_archive_page_loads_with_no_races(client):
    test_client, _ = client
    assert test_client.get("/").status_code == 200


def test_creating_a_race_lands_on_its_console(client):
    test_client, console = client
    slug = make_race(test_client)
    assert slug == "2026-11-26_turkey-trot"
    assert test_client.get(f"/races/{slug}").status_code == 200
    assert b"Turkey Trot" in test_client.get("/").data


def test_an_unknown_race_is_a_404(client):
    test_client, _ = client
    assert test_client.get("/races/no-such-race").status_code == 404


def test_importing_participants_reports_the_count(client):
    test_client, _ = client
    slug = make_race(test_client)
    location = import_field(test_client, slug, make_participants(5, seed=2))
    assert "imported=5" in location

    state = test_client.get(f"/api/races/{slug}/state").get_json()
    assert state["summary"]["registered"] == 5
    assert len(state["participants"]) == 5


def test_a_bad_import_comes_back_as_an_error_not_a_crash(client):
    test_client, _ = client
    slug = make_race(test_client)
    data = {"file": (io.BytesIO(b"name,age\nAda,36\n"), "wrong.csv")}
    response = test_client.post(f"/races/{slug}/participants", data=data,
                                content_type="multipart/form-data")
    assert "error=" in response.headers["Location"]


def test_the_gun_is_refused_when_no_reader_is_attached(client):
    test_client, _ = client
    slug = make_race(test_client)
    response = test_client.post(f"/races/{slug}/start")
    assert response.status_code == 409
    assert response.get_json()["ok"] is False


def test_simulate_mode_needs_a_field_before_it_can_start(client):
    test_client, _ = client
    slug = make_race(test_client)
    response = test_client.post(f"/races/{slug}/live")
    assert "error=" in response.headers["Location"]


def test_only_one_race_can_hold_the_reader(client):
    test_client, console = client
    first = make_race(test_client, "Race One", "2026-05-01")
    second = make_race(test_client, "Race Two", "2026-05-02")
    import_field(test_client, first, make_participants(4, seed=4))
    import_field(test_client, second, make_participants(4, seed=5))

    test_client.post(f"/races/{first}/live")
    try:
        response = test_client.post(f"/races/{second}/live")
        assert "error=" in response.headers["Location"]
    finally:
        console.stop_live()


def test_a_race_runs_start_to_finish_in_simulate_mode(client):
    test_client, console = client
    slug = make_race(test_client, "Simulated 5K", "2026-08-12")
    import_field(test_client, slug, make_participants(12, seed=6))

    test_client.post(f"/races/{slug}/live")
    assert console.session is not None
    try:
        gun = test_client.post(f"/races/{slug}/start").get_json()
        assert gun["ok"] is True

        # The gun cannot be fired twice.
        assert test_client.post(f"/races/{slug}/start").status_code == 409

        # At 20000x, an hour of race plays out in well under a second.
        # Playback is over when the read count stops moving; waiting for
        # on_course to reach zero would hang forever, because a DNF is on
        # course until the end of time.
        deadline = time.monotonic() + 60
        settled = 0
        previous = -1
        while time.monotonic() < deadline and settled < 5:
            count = test_client.get(f"/api/races/{slug}/state").get_json()["reader"]["read_count"]
            settled = settled + 1 if count == previous and count > 0 else 0
            previous = count
            time.sleep(0.2)

        state = test_client.get(f"/api/races/{slug}/state").get_json()
        assert state["live"] is True
        assert state["reader"]["error"] is None
        assert state["reader"]["read_count"] > 0
        assert state["summary"]["finished"] > 0
        assert state["race"]["gun_time_utc"] is not None

        # Finishers come back newest first, with places counting up from the
        # fastest.
        finish_times = [row["finish_utc"] for row in state["finishers"]]
        assert finish_times == sorted(finish_times, reverse=True)
        by_place = sorted(state["finishers"], key=lambda row: row["place"])
        elapsed = [row["elapsed_seconds"] for row in by_place]
        assert elapsed == sorted(elapsed)
        assert [row["place"] for row in by_place] == list(range(1, len(by_place) + 1))

        # Exporting mid race writes a file and does not disturb anything.
        before = state["reader"]["read_count"]
        export = test_client.post(f"/races/{slug}/export").get_json()
        assert export["ok"] is True
        after = test_client.get(f"/api/races/{slug}/state").get_json()
        assert after["reader"]["read_count"] >= before
        assert export["file"] in after["exports"]

        download = test_client.get(f"/races/{slug}/exports/{export['file']}")
        assert download.status_code == 200
        rows = list(csv.reader(io.StringIO(download.data.decode())))
        assert rows[0][0] == "place"
        assert len(rows) > 1
    finally:
        console.stop_live()

    # After the reader is detached the stored results still serve the page.
    state = test_client.get(f"/api/races/{slug}/state").get_json()
    assert state["live"] is False
    assert state["summary"]["finished"] > 0

    recomputed = test_client.post(f"/races/{slug}/recompute").get_json()
    assert recomputed["ok"] is True


def test_an_export_path_outside_the_race_folder_is_refused(client):
    test_client, _ = client
    slug = make_race(test_client)
    assert test_client.get(f"/races/{slug}/exports/../race.db").status_code == 404
