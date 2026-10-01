"""Bib pairing, verify and the merge into a participant CSV.

The rules are driven with explicit times, so nothing here waits on a clock.
"""

from __future__ import annotations

import pytest

from db import parse_participant_csv
from merge import load_registration, merge, write_participants
from pair import (
    FIRST_BIB,
    LAST_BIB,
    PairsFileError,
    Pairer,
    PairStore,
    Verifier,
    load_pairs,
    main,
)

TAG_1 = "E28068940000000000000001"
TAG_2 = "E28068940000000000000002"
TAG_3 = "E28068940000000000000003"
WINDOW = 0.75
CLEAR = 1.5

# This event's bibs run 110 to 310.
FIRST = 110
LAST = 310


def present(pairer: Pairer, epcs, at: float, hold: float = 1.0):
    """Hold tags over the antenna from `at` for `hold` seconds, then take them
    away and let the field clear. Returns every event along the way."""
    events = []
    t = at
    while t < at + hold:
        for epc in epcs:
            pairer.on_read(epc, t)
        event = pairer.poll(t)
        if event:
            events.append(event)
        t += 0.05
    end = t + CLEAR + 0.1
    while t < end:
        event = pairer.poll(t)
        if event:
            events.append(event)
        t += 0.05
    return events


def kinds(events):
    return [event.kind for event in events]


@pytest.fixture
def store(tmp_path):
    return PairStore(tmp_path / "pairs.csv")


def test_the_defaults_cover_this_events_bibs():
    assert (FIRST_BIB, LAST_BIB) == (FIRST, LAST)


def test_a_single_tag_is_accepted_and_written_at_once(store):
    pairer = Pairer(store, FIRST, LAST, WINDOW, CLEAR)
    assert pairer.current == 110
    events = present(pairer, [TAG_1], at=0.0)
    assert kinds(events) == ["accepted", "clear"]
    assert events[0].bib == 110 and events[0].epc == TAG_1
    assert pairer.current == 111
    assert load_pairs(store.path) == {110: TAG_1}
    assert store.path.read_text().splitlines() == ["bib,epc1", f"110,{TAG_1}"]


def test_two_tags_in_the_window_are_rejected(store):
    pairer = Pairer(store, FIRST, LAST, WINDOW, CLEAR)
    events = present(pairer, [TAG_1, TAG_2], at=0.0)
    assert kinds(events) == ["multiple", "clear"]
    assert sorted(events[0].epcs) == [TAG_1, TAG_2]
    assert pairer.current == 110
    assert not store.path.exists()


def test_a_second_tag_arriving_late_in_the_window_still_rejects(store):
    pairer = Pairer(store, FIRST, LAST, WINDOW, CLEAR)
    pairer.on_read(TAG_1, 0.0)
    pairer.on_read(TAG_2, 0.7)
    event = pairer.poll(0.8)
    assert event.kind == "multiple"
    assert store.pairs == {}


def test_an_epc_already_paired_is_rejected(store):
    pairer = Pairer(store, FIRST, LAST, WINDOW, CLEAR)
    present(pairer, [TAG_1], at=0.0)
    events = present(pairer, [TAG_1], at=10.0)
    assert events[0].kind == "duplicate"
    assert events[0].other_bib == 110
    assert pairer.current == 111
    assert load_pairs(store.path) == {110: TAG_1}


def test_a_tag_left_in_the_field_does_not_pair_the_next_bib(store):
    pairer = Pairer(store, FIRST, LAST, WINDOW, CLEAR)
    # The bib stays over the antenna for ten seconds after it is accepted.
    events = []
    t = 0.0
    while t < 10.0:
        pairer.on_read(TAG_1, t)
        event = pairer.poll(t)
        if event:
            events.append(event)
        t += 0.05
    assert kinds(events) == ["accepted"]
    assert not pairer.armed
    # A new tag while the old one is still there is not captured either.
    pairer.on_read(TAG_2, 10.0)
    assert pairer.poll(11.0) is None
    assert pairer.current == 111 and 111 not in store.pairs


def test_resume_starts_at_the_first_missing_bib(tmp_path):
    path = tmp_path / "pairs.csv"
    path.write_text(f"bib,epc1\n110,{TAG_1}\n111,{TAG_2}\n113,E28068940000000000000004\n")
    pairer = Pairer(PairStore(path), FIRST, LAST, WINDOW, CLEAR)
    assert pairer.current == 112
    assert pairer.paired_in_range == 3
    present(pairer, [TAG_3], at=0.0)
    assert pairer.current == 114  # 113 was already done
    assert load_pairs(path)[112] == TAG_3


def test_resume_rejects_a_tag_paired_in_an_earlier_session(tmp_path):
    path = tmp_path / "pairs.csv"
    path.write_text(f"bib,epc1\n110,{TAG_1}\n")
    pairer = Pairer(PairStore(path), FIRST, LAST, WINDOW, CLEAR)
    assert pairer.current == 111
    events = present(pairer, [TAG_1], at=0.0)
    assert events[0].kind == "duplicate" and events[0].other_bib == 110


def test_skip_and_back(store):
    pairer = Pairer(store, FIRST, LAST, WINDOW, CLEAR)
    present(pairer, [TAG_1], at=0.0)
    pairer.skip()
    assert pairer.current == 112
    present(pairer, [TAG_3], at=10.0)
    assert pairer.current == 113
    assert pairer.back() == 112
    assert load_pairs(store.path) == {110: TAG_1}
    assert pairer.back() == 111
    assert pairer.back() == 110
    assert load_pairs(store.path) == {}
    assert pairer.back() == 110  # nothing before the first bib


def test_the_last_bib_finishes_the_session(store):
    pairer = Pairer(store, 309, LAST, WINDOW, CLEAR)
    present(pairer, [TAG_1], at=0.0)
    present(pairer, [TAG_2], at=10.0)
    assert pairer.done
    assert pairer.back() == 310
    assert load_pairs(store.path) == {309: TAG_1}


def test_a_contradictory_pairs_file_is_refused(tmp_path):
    path = tmp_path / "pairs.csv"
    path.write_text(f"bib,epc1\n110,{TAG_1}\n111,{TAG_1}\n")
    with pytest.raises(PairsFileError):
        load_pairs(path)


def test_simulate_mode_runs_with_no_reader(tmp_path, capsys):
    # Every bib already paired: the session ends before it needs a reader read.
    path = tmp_path / "pairs.csv"
    path.write_text(f"bib,epc1\n110,{TAG_1}\n")
    assert main(["--simulate", "--start", "110", "--end", "110", "--out", str(path)]) == 0
    assert "already" in capsys.readouterr().out


# ------------------------------------------------------------------ verify


def test_verify_names_known_tags_and_flags_unknown_ones():
    verifier = Verifier({110: TAG_1, 111: TAG_2, 112: TAG_3}, CLEAR)
    event = verifier.on_read(TAG_2, 0.0)
    assert event.kind == "confirmed" and event.bib == 111
    assert verifier.on_read(TAG_2, 0.1) is None  # the same pass
    unknown = verifier.on_read("E200AAAA", 0.2)
    assert unknown.kind == "unknown"
    verifier.on_read(TAG_1, 1.0)
    assert len(verifier.confirmed) == 2 and verifier.total == 3
    assert verifier.never_seen() == [112]
    # Back over the antenna after leaving the field is announced again.
    assert verifier.on_read(TAG_2, 5.0).bib == 111
    assert len(verifier.confirmed) == 2


# ------------------------------------------------------------------ merge


def test_merge_output_imports_cleanly(tmp_path):
    registration = tmp_path / "registration.csv"
    registration.write_text(
        "﻿Bib,First_Name,Last_Name,Age,Gender\n"
        "110,Ada,Lovelace,36,F\n"
        "111,Alan,Turing,41,M\n"
        "114,Grace,Hopper,,F\n"
        "\n",
        encoding="utf-8",
    )
    pairs = {110: TAG_1, 111: TAG_2, 112: TAG_3}  # 112 is a spare bib, 114 never got a tag

    people, no_bib = load_registration(registration)
    assert no_bib == 0
    rows, unpaired = merge(people, pairs)
    assert unpaired == [114]
    out = tmp_path / "participants.csv"
    write_participants(out, rows)

    text = out.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "bib,first_name,last_name,age,gender,epc1,epc2"
    imported = parse_participant_csv(text)
    assert [row["bib"] for row in imported] == ["110", "111", "112"]
    assert imported[0]["first_name"] == "Ada" and imported[0]["age"] == 36
    assert imported[0]["epcs"] == [TAG_1]
    assert imported[2]["first_name"] == "" and imported[2]["epcs"] == [TAG_3]


def test_merge_cli_reports_unpaired_bibs(tmp_path, capsys):
    from merge import main as merge_main

    registration = tmp_path / "registration.csv"
    registration.write_text("bib,first_name,last_name,age,gender\n110,Ada,Lovelace,36,F\n310,Kay,Austen,29,F\n")
    pairs = tmp_path / "pairs.csv"
    pairs.write_text(f"bib,epc1\n110,{TAG_1}\n111,{TAG_2}\n")
    out = tmp_path / "participants.csv"
    assert merge_main([str(registration), str(pairs), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "1 registered, 1 paired with no registration" in printed
    assert "not paired, left out (1): 310" in printed
    assert len(parse_participant_csv(out.read_text())) == 2


def test_a_bib_lying_a_few_feet_away_is_ignored(store):
    # The live R420 at 12 dBm: held bib near -30 dBm, a neighbour near -55 dBm.
    pairer = Pairer(store, FIRST, LAST, WINDOW, CLEAR, min_rssi=-45.0)
    t = 0.0
    events = []
    while t < 12.0:
        pairer.on_read(TAG_2, t, rssi=-55.0)  # there the whole time
        if 1.0 <= t < 2.0 or 6.0 <= t < 7.0:
            pairer.on_read(TAG_1 if t < 5 else TAG_3, t, rssi=-30.0)
        event = pairer.poll(t)
        if event:
            events.append(event)
        t += 0.05
    assert kinds(events) == ["accepted", "clear", "accepted", "clear"]
    assert load_pairs(store.path) == {110: TAG_1, 111: TAG_3}


def test_a_neighbour_above_the_floor_still_rejects(store):
    pairer = Pairer(store, FIRST, LAST, WINDOW, CLEAR, min_rssi=-45.0)
    pairer.on_read(TAG_1, 0.0, rssi=-30.0)
    pairer.on_read(TAG_2, 0.1, rssi=-40.0)
    assert pairer.poll(0.8).kind == "multiple"


def test_a_registration_export_with_spaced_headers_and_missing_bibs(tmp_path, capsys):
    from merge import main as merge_main

    registration = tmp_path / "export.csv"
    registration.write_text(
        "Registration ID,First Name,Middle Name,Last Name,Bib,Gender,Age,T-Shirt,Event\n"
        "9001,Ada,,Lovelace,110,F,36,M,5K\n"
        "9002,Alan,,Turing,,M,41,L,5K\n"
    )
    people, no_bib = load_registration(registration)
    assert people == {110: {"first_name": "Ada", "last_name": "Lovelace", "age": "36", "gender": "F"}}
    assert no_bib == 1

    pairs = tmp_path / "pairs.csv"
    pairs.write_text(f"bib,epc1\n110,{TAG_1}\n")
    out = tmp_path / "participants.csv"
    assert merge_main([str(registration), str(pairs), "--out", str(out)]) == 0
    assert "1 registrations have no bib number" in capsys.readouterr().out
    assert parse_participant_csv(out.read_text())[0]["last_name"] == "Lovelace"
