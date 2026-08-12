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

from conftest import GUN, MICROS, csv_for

from db import RaceDB, parse_participant_csv
from simulator import SimulatedReader, make_participants

SKEW_SECONDS = 37.0


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
    baseline_race.set_gun_time(GUN)
    baseline = results_by_bib(baseline_race)

    # The operator presses START at the same instant. The Pi reads GUN off its
    # own clock; the reader has been stamping every read 37 seconds ahead.
    skewed_race, skewed_sim = race_with_skew(tmp_path, SKEW_SECONDS, "Reader Ahead")
    skewed_race.set_gun_time(GUN)

    assert skewed_sim.clock_offset_micros == int(SKEW_SECONDS * MICROS)
    assert_matches_baseline(results_by_bib(skewed_race), baseline, SKEW_SECONDS)


def test_a_reader_running_behind_the_pi_still_times_the_race(tmp_path):
    baseline_race, baseline_sim = race_with_skew(tmp_path, 0.0, "Baseline")
    baseline_race.set_gun_time(GUN)
    baseline = results_by_bib(baseline_race)

    skewed_race, skewed_sim = race_with_skew(tmp_path, -SKEW_SECONDS, "Reader Behind")
    skewed_race.set_gun_time(GUN)

    assert skewed_sim.clock_offset_micros == -int(SKEW_SECONDS * MICROS)
    assert_matches_baseline(results_by_bib(skewed_race), baseline, -SKEW_SECONDS)