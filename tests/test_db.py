"""Storage: the CSV import, the read log, and the export."""

from __future__ import annotations

import csv

import pytest
from conftest import FINISH_PORT, GUN, START_PORT, TAG_A, TAG_B, at, burst, read

import db as racedb
from db import ParticipantImportError, RaceDB, list_races, parse_participant_csv

HEADER = "bib,first_name,last_name,age,gender,epc1,epc2\n"


# ------------------------------------------------------------------ import


def test_a_normal_participant_file_imports():
    rows = parse_participant_csv(
        HEADER
        + f"101,Ada,Lovelace,36,F,{TAG_A},{TAG_B}\n"
        + "102,Alan,Turing,41,M,E2801170000002000000000A,\n"
    )
    assert [row["bib"] for row in rows] == ["101", "102"]
    assert rows[0]["epcs"] == [TAG_A, TAG_B]
    assert rows[1]["epcs"] == ["E2801170000002000000000A"]
    assert rows[0]["age"] == 36


def test_epcs_are_normalised_on_the_way_in():
    rows = parse_participant_csv(HEADER + "101,Ada,Lovelace,36,F,e280-1170 0000 0200 0000 0001,\n")
    assert rows[0]["epcs"] == ["E28011700000020000000001"]


def test_a_trailing_blank_line_is_not_an_error():
    rows = parse_participant_csv(HEADER + f"101,Ada,Lovelace,36,F,{TAG_A},\n\n")
    assert len(rows) == 1


def test_missing_age_is_allowed():
    rows = parse_participant_csv(HEADER + f"101,Ada,Lovelace,,F,{TAG_A},\n")
    assert rows[0]["age"] is None


@pytest.mark.parametrize(
    "text, message",
    [
        ("name,age\nAda,36\n", "no bib column"),
        ("bib,first_name\n101,Ada\n", "no epc1 or epc2"),
        (HEADER + f"101,Ada,L,36,F,{TAG_A},\n101,Alan,T,41,M,{TAG_B},\n", "appears twice"),
        (HEADER + f"101,Ada,L,36,F,{TAG_A},\n102,Alan,T,41,M,{TAG_A},\n", "already assigned"),
        (HEADER + "101,Ada,L,36,F,,\n", "no EPC"),
        (HEADER, "no participants"),
    ],
)
def test_a_bad_file_is_rejected_with_a_readable_message(text, message):
    with pytest.raises(ParticipantImportError) as caught:
        parse_participant_csv(text)
    assert message in str(caught.value)


def test_importing_the_same_bib_twice_is_refused(race_db):
    rows = parse_participant_csv(HEADER + f"101,Ada,L,36,F,{TAG_A},\n")
    race_db.add_participants(rows)
    with pytest.raises(ParticipantImportError):
        race_db.add_participants(parse_participant_csv(HEADER + f"101,Ada,L,36,F,{TAG_B},\n"))
    assert race_db.participant_count() == 1


# -------------------------------------------------------------------- reads


def test_reads_survive_the_round_trip_to_microseconds(race_db):
    reads = burst(TAG_A, START_PORT, 1.234567, count=3)
    race_db.append_reads(reads)
    stored = race_db.reads()
    assert [r.first_seen_utc for r in stored] == [r.first_seen_utc for r in reads]
    assert stored[0].antenna_port == START_PORT
    assert race_db.read_count() == 3


def test_raw_reads_are_kept_even_when_they_are_useless(race_db):
    # Pre gun reads and reads from a tag nobody registered are still logged.
    race_db.append_reads([read("E28000000000000000009999", START_PORT, -400.0)])
    assert race_db.read_count() == 1
    assert race_db.seen_epcs() == {"E28000000000000000009999"}


# ----------------------------------------------------------------- results


def _one_finisher(race_db):
    race_db.add_participants(parse_participant_csv(HEADER + f"101,Ada,L,36,F,{TAG_A},\n"))
    race_db.append_reads(burst(TAG_A, START_PORT, 2.0) + burst(TAG_A, FINISH_PORT, 1500.0))
    race_db.set_gun_time(GUN)


def test_recompute_writes_results_that_read_back(race_db):
    _one_finisher(race_db)
    computed = race_db.recompute()
    assert [r.status for r in computed] == ["finished"]
    stored = race_db.results()
    assert stored == computed


def test_recompute_is_repeatable_and_replaces_rather_than_appends(race_db):
    _one_finisher(race_db)
    first = race_db.recompute()
    second = race_db.recompute()
    assert first == second
    assert len(race_db.results()) == 1


def test_recompute_reflects_a_corrected_gun_time(race_db):
    _one_finisher(race_db)
    race_db.recompute()
    # The operator hit the button four seconds late and fixes the record.
    race_db.set_gun_time(at(-4.0))
    assert race_db.recompute()[0].elapsed_seconds == 1498.0


# ------------------------------------------------------------------ export


def test_export_writes_the_agreed_columns(race_db):
    _one_finisher(race_db)
    results = race_db.recompute()
    path = race_db.export_csv(results)

    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == racedb.CSV_COLUMNS
    assert rows[1][:6] == ["1", "101", "Ada", "L", "36", "F"]
    assert rows[1][8] == "24:58.00"
    assert rows[1][9] == "finished"
    assert path.parent == race_db.directory


def test_finishers_come_first_and_exceptions_follow(race_db):
    race_db.add_participants(
        parse_participant_csv(
            HEADER
            + f"101,Ada,L,36,F,{TAG_A},\n"
            + f"102,Alan,T,41,M,{TAG_B},\n"
        )
    )
    race_db.append_reads(burst(TAG_A, START_PORT, 2.0) + burst(TAG_A, FINISH_PORT, 1500.0))
    race_db.append_reads(burst(TAG_B, START_PORT, 2.0))  # never finishes
    race_db.set_gun_time(GUN)

    path = race_db.export_csv(race_db.recompute())
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))[1:]
    assert [row[1] for row in rows] == ["101", "102"]
    assert rows[0][0] == "1"
    assert rows[1][0] == ""       # no place for a DNF
    assert rows[1][9] == "dnf"


def test_two_exports_in_the_same_second_do_not_collide(race_db):
    _one_finisher(race_db)
    race_db.recompute()
    first = race_db.export_csv()
    second = race_db.export_csv()
    assert first != second
    assert len(race_db.exports()) == 2


def test_an_export_taken_mid_race_holds_what_was_known_then(race_db):
    race_db.add_participants(
        parse_participant_csv(HEADER + f"101,Ada,L,36,F,{TAG_A},\n102,Alan,T,41,M,{TAG_B},\n")
    )
    race_db.set_gun_time(GUN)
    race_db.append_reads(burst(TAG_A, START_PORT, 2.0) + burst(TAG_B, START_PORT, 2.0))
    race_db.append_reads(burst(TAG_A, FINISH_PORT, 1500.0))
    mid = race_db.export_csv(race_db.recompute())

    race_db.append_reads(burst(TAG_B, FINISH_PORT, 1800.0))
    final = race_db.export_csv(race_db.recompute())

    def finishers(path):
        with open(path, newline="", encoding="utf-8") as handle:
            return [row[1] for row in list(csv.reader(handle))[1:] if row[9] == "finished"]

    assert finishers(mid) == ["101"]
    assert finishers(final) == ["101", "102"]


# ----------------------------------------------------------------- archive


def test_the_archive_lists_races_newest_first(tmp_path):
    root = tmp_path / "races"
    RaceDB.create("Spring Five", "2026-03-01", "5K", root=root).close()
    RaceDB.create("Turkey Trot", "2026-11-26", "5K", root=root).close()
    listed = list_races(root)
    assert [race["name"] for race in listed] == ["Turkey Trot", "Spring Five"]
    assert listed[0]["slug"] == "2026-11-26_turkey-trot"


def test_two_races_with_the_same_name_and_date_get_their_own_folders(tmp_path):
    root = tmp_path / "races"
    first = RaceDB.create("Turkey Trot", "2026-11-26", root=root)
    second = RaceDB.create("Turkey Trot", "2026-11-26", root=root)
    assert first.directory != second.directory
    first.close()
    second.close()


def test_a_stray_folder_does_not_break_the_archive(tmp_path):
    root = tmp_path / "races"
    RaceDB.create("Turkey Trot", "2026-11-26", root=root).close()
    (root / "half-copied-folder").mkdir()
    assert len(list_races(root)) == 1


def test_a_race_outside_the_races_directory_cannot_be_opened(tmp_path):
    with pytest.raises(ValueError):
        racedb.open_race("../..", tmp_path / "races")
