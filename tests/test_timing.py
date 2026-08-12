"""The timing rules, one rule per group of tests.

These run against handwritten read logs, because the point is to pin down the
exact behaviour at the edges of each rule. The generated race in
test_integration.py checks that the rules hold up over a whole field.
"""

from __future__ import annotations

from conftest import (
    FINISH_PORT,
    GUN,
    OTHER_FINISH_PORT,
    OTHER_START_PORT,
    START_PORT,
    TAG_A,
    TAG_B,
    at,
    burst,
    read,
)

from timing import (
    STATUS_DNF,
    STATUS_FINISHED,
    STATUS_NOT_STARTED,
    STATUS_REVIEW,
    Participant,
    collapse_bursts,
    compute_result,
    compute_results,
    format_elapsed,
    index_reads_by_epc,
    order_finishers,
    places,
    summarize,
)

ONE = Participant(participant_id=1, bib="101", epcs=(TAG_A,))
DUAL = Participant(participant_id=2, bib="102", epcs=(TAG_A, TAG_B))


def result_for(participant, reads, gun=GUN, min_elapsed=720):
    return compute_result(participant, index_reads_by_epc(reads), gun, min_elapsed)


# ----------------------------------------------------------------- bursts


def test_burst_collapses_to_its_earliest_read():
    stamps = [at(10.0 + i * 0.05) for i in range(30)]
    assert collapse_bursts(stamps) == [at(10.0)]


def test_reads_more_than_the_gap_apart_are_separate_crossings():
    stamps = [at(10.0), at(10.5), at(13.0), at(13.4)]
    assert collapse_bursts(stamps) == [at(10.0), at(13.0)]


def test_lingering_in_the_field_stays_one_crossing():
    # Reads every 1.5 seconds for a minute: never a 2 second gap, so it is one
    # long crossing rather than 40 phantom ones.
    stamps = [at(10.0 + i * 1.5) for i in range(40)]
    assert collapse_bursts(stamps) == [at(10.0)]


def test_unordered_input_is_sorted_first():
    assert collapse_bursts([at(13.0), at(10.0), at(10.4)]) == [at(10.0), at(13.0)]


def test_no_reads_no_crossings():
    assert collapse_bursts([]) == []


# -------------------------------------------------------------- pre gun


def test_reads_before_the_gun_are_ignored():
    reads = burst(TAG_A, START_PORT, -120.0) + burst(TAG_A, START_PORT, 4.0)
    result = result_for(ONE, reads + burst(TAG_A, FINISH_PORT, 1500.0))
    assert result.start_utc == at(4.0)


def test_someone_standing_on_the_mat_at_the_gun_starts_at_the_gun():
    # Their burst straddles the gun. The first read at or after it wins, which
    # is within a few hundredths of the gun itself.
    reads = burst(TAG_A, START_PORT, -1.0, count=40, spacing=0.05)
    reads += burst(TAG_A, FINISH_PORT, 1500.0)
    result = result_for(ONE, reads)
    assert result.start_utc is not None
    assert GUN <= result.start_utc <= at(0.06)


def test_a_runner_only_seen_before_the_gun_never_started():
    result = result_for(ONE, burst(TAG_A, START_PORT, -60.0))
    assert result.status == STATUS_NOT_STARTED
    assert result.start_utc is None


# --------------------------------------------------------- minimum elapsed


def test_stray_finish_reads_early_in_the_race_are_rejected():
    reads = burst(TAG_A, START_PORT, 2.0)
    reads += burst(TAG_A, FINISH_PORT, 240.0)      # loitering by the arch
    reads += burst(TAG_A, FINISH_PORT, 1500.0)     # the real finish
    result = result_for(ONE, reads)
    assert result.finish_utc == at(1500.0)


def test_a_burst_that_straddles_the_cutoff_is_rejected_whole():
    # Begins at 719 seconds, still going at 721. Taking the first read after
    # the cutoff would invent a finish at exactly 720; the whole burst goes.
    reads = burst(TAG_A, START_PORT, 0.0)
    reads += burst(TAG_A, FINISH_PORT, 719.0, count=60, spacing=0.05)
    reads += burst(TAG_A, FINISH_PORT, 1500.0)
    result = result_for(ONE, reads)
    assert result.finish_utc == at(1500.0)


def test_the_cutoff_is_measured_from_that_runners_own_start():
    # Starts 300 seconds after the gun at the back of the field, finishes 800
    # seconds later. That is 1100 by the gun clock but only 800 net, and the
    # cutoff must not reject it.
    reads = burst(TAG_A, START_PORT, 300.0)
    reads += burst(TAG_A, FINISH_PORT, 1100.0)
    result = result_for(ONE, reads)
    assert result.finish_utc == at(1100.0)
    assert result.elapsed_seconds == 800.0


def test_min_elapsed_is_configurable_per_race():
    reads = burst(TAG_A, START_PORT, 0.0) + burst(TAG_A, FINISH_PORT, 300.0)
    assert result_for(ONE, reads, min_elapsed=720).status == STATUS_DNF
    assert result_for(ONE, reads, min_elapsed=120).status == STATUS_FINISHED


# ---------------------------------------------------------------- dual tags


def test_the_earlier_of_two_tags_wins_at_both_lines():
    reads = burst(TAG_A, START_PORT, 5.0) + burst(TAG_B, OTHER_START_PORT, 3.0)
    reads += burst(TAG_A, FINISH_PORT, 1500.0) + burst(TAG_B, OTHER_FINISH_PORT, 1490.0)
    result = result_for(DUAL, reads)
    assert result.start_utc == at(3.0)
    assert result.finish_utc == at(1490.0)


def test_one_dead_tag_still_produces_a_result():
    # Tag A never reads at the finish. The second tag carries the result,
    # which is the entire reason a runner wears two.
    reads = burst(TAG_A, START_PORT, 3.0) + burst(TAG_B, FINISH_PORT, 1500.0)
    result = result_for(DUAL, reads)
    assert result.status == STATUS_FINISHED
    assert result.start_utc == at(3.0)
    assert result.finish_utc == at(1500.0)


# ------------------------------------------------------------------ status


def test_start_without_finish_is_dnf():
    result = result_for(ONE, burst(TAG_A, START_PORT, 2.0))
    assert result.status == STATUS_DNF
    assert result.finish_utc is None
    assert result.elapsed_seconds is None


def test_finish_without_start_goes_to_review_and_is_not_guessed():
    result = result_for(ONE, burst(TAG_A, FINISH_PORT, 1500.0))
    assert result.status == STATUS_REVIEW
    assert result.start_utc is None
    assert result.elapsed_seconds is None
    assert result.finish_utc == at(1500.0)


def test_a_tag_never_seen_at_all_is_not_started():
    assert result_for(ONE, []).status == STATUS_NOT_STARTED


def test_no_gun_time_means_nobody_has_a_result():
    reads = burst(TAG_A, START_PORT, 2.0) + burst(TAG_A, FINISH_PORT, 1500.0)
    results = compute_results(reads, [ONE], None)
    assert [r.status for r in results] == [STATUS_NOT_STARTED]


# ---------------------------------------------------------------- elapsed


def test_elapsed_is_reported_to_hundredths():
    reads = burst(TAG_A, START_PORT, 0.0, count=1)
    reads += burst(TAG_A, FINISH_PORT, 1234.567, count=1)
    result = result_for(ONE, reads)
    assert result.elapsed_seconds == 1234.57


def test_format_elapsed_switches_to_hours_only_when_needed():
    assert format_elapsed(1680.0) == "28:00.00"
    assert format_elapsed(1234.57) == "20:34.57"
    assert format_elapsed(3725.5) == "1:02:05.50"
    assert format_elapsed(None) == ""


# ----------------------------------------------------------- field results


def test_places_follow_elapsed_time_not_finish_order():
    # The back marker started late and ran faster, so they win on net time
    # even though they crossed the line second.
    fast = Participant(1, "101", (TAG_A,))
    slow = Participant(2, "102", (TAG_B,))
    reads = burst(TAG_A, START_PORT, 0.0) + burst(TAG_A, FINISH_PORT, 1500.0)
    reads += burst(TAG_B, START_PORT, 200.0) + burst(TAG_B, FINISH_PORT, 1600.0)
    results = compute_results(reads, [fast, slow], GUN)
    assert places(results) == {2: 1, 1: 2}
    assert [r.bib for r in order_finishers(results)] == ["102", "101"]


def test_summary_counts_add_up():
    finisher = Participant(1, "101", (TAG_A,))
    dnf = Participant(2, "102", (TAG_B,))
    absent = Participant(3, "103", ("E28000000000000000000009",))
    reads = burst(TAG_A, START_PORT, 1.0) + burst(TAG_A, FINISH_PORT, 1500.0)
    reads += burst(TAG_B, START_PORT, 1.0)
    summary = summarize(compute_results(reads, [finisher, dnf, absent], GUN))
    assert summary == {
        "registered": 3,
        "started": 2,
        "finished": 1,
        "on_course": 1,
        "review": 0,
        "not_started": 1,
    }
