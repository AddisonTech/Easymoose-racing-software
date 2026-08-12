"""Two clocks, one race.

Read timestamps come from the reader's clock. The gun time is taken from the
Pi's clock when the operator presses the button. Nothing keeps those two in
step, and the start rule compares one against the other, so any offset between
them shifts or destroys every start time in the race.

Simulate mode never showed this, because a simulator that stamps reads from
the same clock the gun comes from has quietly assumed the bug away. So the
simulator can now be given a skew, and these tests run a whole race with the
reader's clock wrong and demand the results come out right anyway.

The skew is deliberately large and awkward. A reader 37 seconds ahead is not
subtle, and results computed against the wrong clock domain do not survive it.
"""

from __future__ import annotations

import io
import threading
import time

import pytest
from conftest import GUN, MICROS, csv_for

from app import ClockOffsetUnknown, Console, LiveSession, build_state, create_app
from db import RaceDB, parse_participant_csv
from reader import Reader
from simulator import SimulatedReader, make_participants

SKEW_SECONDS = 37.0


class SilentReader(Reader):
    """A reader that produces nothing and knows nothing about its clock.

    Stands in for a real reader between connecting and the first measurement
    landing: attached, but with no offset to record a gun time against yet.
    """

    def __init__(self, clock_offset_micros=None):
        self._offset = clock_offset_micros
        self._stopped = threading.Event()

    def reads(self):
        while not self._stopped.is_set():
            self._stopped.wait(0.05)
        return
        yield  # pragma: no cover - makes this a generator

    def stop(self):
        self._stopped.set()

    @property
    def clock_offset_micros(self):
        return self._offset


def race_with_skew(tmp_path, skew_seconds: float, name: str):
    """A full generated race whose reads carry the given clock skew."""
    people = make_participants(30, seed=11)
    sim = SimulatedReader(people, seed=11, clock_skew_seconds=skew_seconds)
    race = RaceDB.create(name, "2026-08-12", "5K", 720, root=tmp_path / "races")
    race.add_participants(parse_participant_csv(csv_for(people)))
    race.append_reads(sim.generate(GUN))
    return race, sim


def results_by_bib(race):
    return {result.bib: result for result in race.recompute()}


def assert_matches_baseline(skewed, baseline, skew_seconds: float):
    """Same race, same answers, with every clock instant shifted by the skew.

    Statuses and elapsed times must be identical: the skew is a property of
    the clock, not of the running. The recorded instants move with the
    reader's clock, because that is the domain the reads were stamped in.
    """
    skew_micros = int(round(skew_seconds * MICROS))
    assert set(skewed) == set(baseline)
    for bib, expected in baseline.items():
        actual = skewed[bib]
        assert actual.status == expected.status, f"bib {bib} status"
        assert actual.elapsed_seconds == expected.elapsed_seconds, f"bib {bib} elapsed"
        if expected.start_utc is not None:
            assert actual.start_utc == expected.start_utc + skew_micros, f"bib {bib} start"
        if expected.finish_utc is not None:
            assert actual.finish_utc == expected.finish_utc + skew_micros, f"bib {bib} finish"


def test_a_reader_running_ahead_of_the_pi_still_times_the_race(tmp_path):
    baseline_race, baseline_sim = race_with_skew(tmp_path, 0.0, "Baseline")
    baseline_race.set_gun_time(GUN, baseline_sim.clock_offset_micros)
    baseline = results_by_bib(baseline_race)

    # The operator presses START at the same instant. The Pi reads GUN off its
    # own clock; the reader has been stamping every read 37 seconds ahead.
    skewed_race, skewed_sim = race_with_skew(tmp_path, SKEW_SECONDS, "Reader Ahead")
    skewed_race.set_gun_time(GUN, skewed_sim.clock_offset_micros)

    assert skewed_sim.clock_offset_micros == int(SKEW_SECONDS * MICROS)
    assert_matches_baseline(results_by_bib(skewed_race), baseline, SKEW_SECONDS)


def test_a_reader_running_behind_the_pi_still_times_the_race(tmp_path):
    baseline_race, baseline_sim = race_with_skew(tmp_path, 0.0, "Baseline")
    baseline_race.set_gun_time(GUN, baseline_sim.clock_offset_micros)
    baseline = results_by_bib(baseline_race)

    skewed_race, skewed_sim = race_with_skew(tmp_path, -SKEW_SECONDS, "Reader Behind")
    skewed_race.set_gun_time(GUN, skewed_sim.clock_offset_micros)

    assert skewed_sim.clock_offset_micros == -int(SKEW_SECONDS * MICROS)
    assert_matches_baseline(results_by_bib(skewed_race), baseline, -SKEW_SECONDS)


def test_the_gun_time_is_stored_in_both_clock_domains(tmp_path):
    race, sim = race_with_skew(tmp_path, SKEW_SECONDS, "Both Domains")
    race.set_gun_time(GUN, sim.clock_offset_micros)

    info = race.info()
    assert info.gun_time_utc == GUN, "the Pi domain record of the button press"
    assert info.gun_time_reader_utc == GUN + int(SKEW_SECONDS * MICROS)
    assert info.effective_gun_time_utc == info.gun_time_reader_utc


def test_a_race_timed_before_the_column_existed_falls_back_to_the_pi_gun(tmp_path):
    """Old race.db files have no gun_time_reader_utc and must still resolve.

    Their reads and their gun time were compared directly when they were
    timed, so the Pi domain value is what reproduces the results those runners
    were handed.
    """
    race, sim = race_with_skew(tmp_path, 0.0, "Legacy Race")
    race.set_gun_time(GUN, 0)
    expected = results_by_bib(race)

    # Exactly what an untouched older database looks like.
    race.connection.execute("UPDATE races SET gun_time_reader_utc = NULL WHERE id = 1")
    race.connection.commit()

    info = race.info()
    assert info.gun_time_reader_utc is None
    assert info.effective_gun_time_utc == GUN
    assert results_by_bib(race) == expected


def test_firing_the_gun_converts_it_with_the_readers_offset(tmp_path):
    race, _ = race_with_skew(tmp_path, 0.0, "Live Gun")
    session = LiveSession(race, SilentReader(int(SKEW_SECONDS * MICROS)), "test")
    try:
        gun = session.fire_gun()
        info = race.info()
        assert info.gun_time_utc == gun
        assert info.gun_time_reader_utc == gun + int(SKEW_SECONDS * MICROS)
    finally:
        session.stop()


def test_the_gun_is_refused_while_the_offset_is_unknown(tmp_path):
    race, _ = race_with_skew(tmp_path, 0.0, "No Offset Yet")
    session = LiveSession(race, SilentReader(None), "test")
    try:
        with pytest.raises(ClockOffsetUnknown):
            session.fire_gun()
        # Nothing was recorded, so the race can still be started properly.
        assert race.info().gun_time_utc is None
        assert race.info().gun_time_reader_utc is None
    finally:
        session.stop()


@pytest.mark.parametrize(
    "offset_seconds, expect_warning",
    [(0.0, False), (1.9, False), (2.0, False), (2.1, True), (-2.1, True), (-37.0, True)],
)
def test_the_console_warns_only_past_two_seconds(tmp_path, offset_seconds, expect_warning):
    race, _ = race_with_skew(tmp_path, 0.0, f"Warn {offset_seconds}")
    session = LiveSession(race, SilentReader(int(offset_seconds * MICROS)), "test")
    try:
        reader_state = build_state(race, session)["reader"]
        assert reader_state["clock_offset_seconds"] == pytest.approx(offset_seconds)
        assert reader_state["clock_warning"] is expect_warning
    finally:
        session.stop()


def test_an_unmeasured_offset_shows_as_unknown_rather_than_zero(tmp_path):
    race, _ = race_with_skew(tmp_path, 0.0, "Unknown Offset")
    session = LiveSession(race, SilentReader(None), "test")
    try:
        reader_state = build_state(race, session)["reader"]
        assert reader_state["clock_offset_seconds"] is None
        assert reader_state["clock_warning"] is False
    finally:
        session.stop()


def test_a_whole_simulate_run_survives_a_skewed_reader_clock(tmp_path):
    """The operator path, end to end, with the reader's clock 37 seconds out.

    This is the case the old simulator could not express at all, which is why
    the defect lived through a green suite and a working demo.
    """
    console = Console(
        root=tmp_path / "races",
        simulate=True,
        reader_host="127.0.0.1",
        reader_port=5084,
        tx_power_dbm=30.0,
        sim_speed=20000.0,
        sim_seed=6,
        sim_clock_skew=SKEW_SECONDS,
    )
    console.root.mkdir(parents=True, exist_ok=True)
    app = create_app(console)
    app.config.update(TESTING=True)

    with app.test_client() as client:
        response = client.post("/races", data={"name": "Skewed Clock", "date": "2026-08-12",
                                               "distance": "5K", "min_elapsed_seconds": "720"})
        slug = response.headers["Location"].rsplit("/", 1)[-1]

        people = make_participants(12, seed=6)
        client.post(
            f"/races/{slug}/participants",
            data={"file": (io.BytesIO(csv_for(people).encode()), "field.csv")},
            content_type="multipart/form-data",
        )
        client.post(f"/races/{slug}/live")
        simulated_reader = console.session.reader
        race = console.session.race
        try:
            assert client.post(f"/races/{slug}/start").get_json()["ok"] is True

            deadline = time.monotonic() + 60
            settled, previous = 0, -1
            while time.monotonic() < deadline and settled < 5:
                count = client.get(f"/api/races/{slug}/state").get_json()["reader"]["read_count"]
                settled = settled + 1 if count == previous and count > 0 else 0
                previous = count
                time.sleep(0.2)

            state = client.get(f"/api/races/{slug}/state").get_json()
            assert state["reader"]["clock_offset_seconds"] == pytest.approx(SKEW_SECONDS)
            assert state["reader"]["clock_warning"] is True
            assert state["race"]["gun_time_reader_utc"] == (
                state["race"]["gun_time_utc"] + int(SKEW_SECONDS * MICROS)
            )

            # The point of the whole exercise: the field is timed correctly
            # despite the clocks disagreeing. Against the unfixed code a skew
            # this size wrecks starts wholesale and the review pile fills up,
            # so the runner the simulator deliberately denied a start should
            # be the only one in it.
            assert state["summary"]["finished"] > 0
            assert all(row["elapsed_seconds"] >= 720 for row in state["finishers"])

            reviewed = {p["bib"] for p in state["participants"] if p["status"] == "review"}
            assert reviewed == set(simulated_reader.expected_review_bibs)

            # Statuses alone are too blunt to catch this: a 37 second error
            # moves every start without pushing anyone across a status
            # boundary. Check the instants. Every runner crosses the start
            # line within a few seconds of the gun, and the pre-gun milling
            # reads a skewed clock drags forward would land before it.
            live_results, _, _, _ = console.session.snapshot()
            gun_reader = race.info().gun_time_reader_utc
            starts = [r.start_utc for r in live_results if r.start_utc is not None]
            assert starts, "nobody started"
            for start_utc in starts:
                assert 0 <= start_utc - gun_reader <= 20 * MICROS
        finally:
            console.stop_live()


def test_the_column_is_added_to_a_database_that_predates_it(tmp_path):
    race, _ = race_with_skew(tmp_path, 0.0, "Older Schema")
    directory = race.directory
    race.connection.execute("ALTER TABLE races DROP COLUMN gun_time_reader_utc")
    race.connection.commit()
    race.close()

    reopened = RaceDB.open(directory)
    columns = {
        row["name"] for row in reopened.connection.execute("PRAGMA table_info(races)")
    }
    assert "gun_time_reader_utc" in columns
    assert reopened.info().gun_time_reader_utc is None
    reopened.close()
