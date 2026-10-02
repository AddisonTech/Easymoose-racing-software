"""Bib assignment, the pickup sheet, and runner names on the pairing screen.

Every name here is made up. Nothing reads the real registration export.
"""

from __future__ import annotations

import csv
import re

import pytest

from assign_bibs import AssignError, assign, main, pickup_sheet, read_export, read_roster
from pair import bib_label, load_roster, resolve_roster

@pytest.fixture(autouse=True)
def run_in_a_temp_folder(tmp_path, monkeypatch):
    """main() defaults to writing in Data/. Never let a test touch the real one."""
    monkeypatch.chdir(tmp_path)


EXPORT_HEADER = "Registration ID,First Name,Middle Name,Last Name,Bib,Gender,Age,T-Shirt,Event\n"


def runner(first, last, event="5K (Adult)", tshirt="M"):
    return {"first_name": first, "last_name": last, "age": "30", "gender": "F",
            "event": event, "tshirt": tshirt}


def write_export(path, rows):
    path.write_text(
        EXPORT_HEADER
        + "".join(f"{9000 + i},{first},,{last},,F,30,M,5K (Adult)\n" for i, (first, last) in enumerate(rows)),
        encoding="utf-8",
    )


# ------------------------------------------------------------------ assignment


def test_bibs_go_by_last_name_then_first_ignoring_case_from_110():
    rows = assign([
        runner("Wren", "Oakley"),
        runner("bram", "abbott"),
        runner("Ana", "Abbott"),
        runner("Cole", "de Vries"),
        runner("Dina", "Delgado"),
        runner("zed", "Oakley"),
        runner("Ava", "OAKLEY"),
    ])
    assert [(row["bib"], row["first_name"], row["last_name"]) for row in rows] == [
        (110, "Ana", "Abbott"),
        (111, "bram", "abbott"),
        (112, "Cole", "de Vries"),  # plain comparison: the space sorts before "l"
        (113, "Dina", "Delgado"),
        (114, "Ava", "OAKLEY"),
        (115, "Wren", "Oakley"),
        (116, "zed", "Oakley"),
    ]


def test_more_runners_than_bibs_is_refused():
    with pytest.raises(AssignError):
        assign([runner(f"R{i}", f"Test{i:03d}") for i in range(4)], first_bib=110, last_bib=112)


def test_an_export_with_spaced_headers_is_read_and_nameless_rows_counted(tmp_path):
    export = tmp_path / "export.csv"
    export.write_text(
        "﻿" + EXPORT_HEADER
        + "9001,Mira,J,Quill,,F,34,S,5K (Junior)\n"
        + "9002,,,,,M,40,L,5K (Adult)\n"
        + "\n",
        encoding="utf-8",
    )
    runners, nameless = read_export(export)
    assert runners == [{"first_name": "Mira", "last_name": "Quill", "age": "34", "gender": "F",
                        "event": "5K (Junior)", "tshirt": "S", "registration_id": "9001",
                        "registration_bib": ""}]
    assert nameless == 1


def test_main_writes_the_roster_and_sheet_and_leaves_the_export_alone(tmp_path, capsys):
    export = tmp_path / "export.csv"
    write_export(export, [("Tova", "Zeller"), ("Ike", "Amsel"), ("Juno", "Marsh")])
    before = export.read_bytes()
    roster, sheet = tmp_path / "Data" / "registration_bibs.csv", tmp_path / "Data" / "pickup_sheet.html"

    assert main([str(export), "--out", str(roster), "--sheet", str(sheet), "--first", "110", "--last", "120"]) == 0
    assert export.read_bytes() == before
    with roster.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == ["bib", "first_name", "last_name", "age", "gender", "event", "tshirt",
                             "registration_id"]
    assert [(r["bib"], r["last_name"]) for r in rows] == [("110", "Amsel"), ("111", "Marsh"), ("112", "Zeller")]

    printed = capsys.readouterr().out
    assert "Last bib used: 112. Spare for day-of signups: 113-120 (8 bibs)." in printed
    for name in ("Tova", "Zeller", "Amsel", "Marsh"):
        assert name not in printed  # counts only, never names
    assert sheet.read_text(encoding="utf-8").count('<tr class="blank">') == 8


def test_running_again_keeps_the_bibs_already_handed_out(tmp_path, capsys):
    export = tmp_path / "export.csv"
    roster, sheet = tmp_path / "registration_bibs.csv", tmp_path / "sheet.html"
    write_export(export, [("Tova", "Zeller"), ("Ike", "Amsel")])
    main([str(export), "--out", str(roster), "--sheet", str(sheet)])

    # A late registrant sorts first; reassigning would shift everyone's bib, so
    # they take the next free bib instead.
    write_export(export, [("Tova", "Zeller"), ("Ike", "Amsel"), ("Abe", "Aaron")])
    main([str(export), "--out", str(roster), "--sheet", str(sheet)])
    assert [(r["bib"], r["last_name"]) for r in read_roster(roster)] == [
        (110, "Amsel"), (111, "Zeller"), (112, "Aaron")]
    printed = capsys.readouterr().out
    assert "already assigned" in printed
    assert "Runners before: 2. Now: 3. New runners given bibs: 112." in printed
    assert "Aaron" not in printed

    main([str(export), "--out", str(roster), "--sheet", str(sheet), "--force"])
    assert [r["last_name"] for r in read_roster(roster)] == ["Aaron", "Amsel", "Zeller"]


def test_the_pickup_sheet_is_sorted_escaped_and_has_blank_rows():
    rows = [
        {"bib": 111, **runner("Lark", "Zane", tshirt="XL")},
        {"bib": 110, **runner("Bo", "<O'Hara & Co>", event="5K (Junior)")},
    ]
    page = pickup_sheet(rows, "Test 5K - Bib pickup", spare_bibs=[112, 113, 114], spare="112-140")
    assert page.index("&lt;O&#x27;Hara &amp; Co&gt;") < page.index("Zane")
    assert "<O'Hara" not in page
    assert page.count('<tr class="blank">') == 3
    for heading in ("Last", "First", "Bib", "Event", "T-Shirt"):
        assert f"<th>{heading}</th>" in page
    assert page.count('<span class="box"></span>') == 5
    assert "spare bibs 112-140" in page


def test_signup_rows_carry_the_spare_bibs_in_order_under_their_own_header():
    rows = [{"bib": 110, **runner("Lark", "Zane")}, {"bib": 112, **runner("Bo", "Abel")}]
    page = pickup_sheet(rows, "Test 5K - Bib pickup", spare_bibs=[111, 113, 114], spare="111, 113-114")
    header = "Race day signups: hand out the next bib in order and write the name."
    assert page.count(header) == 1
    assert page.index("Zane") < page.index(header)  # after every named runner
    signups = page[page.index(header):]
    assert [int(b) for b in re.findall(r'<tr class="blank">.*?<td class="bib">(\d+)</td>', signups)] == [111, 113, 114]
    blank = '<tr class="blank"><td></td><td></td><td class="bib">113</td><td></td><td></td>' \
            '<td><span class="box"></span></td></tr>'
    assert blank in page  # name, event and shirt empty, picked-up box kept
    # One table, so the column headings repeat on every printed page.
    assert page.count("<table>") == 1 and "thead { display: table-header-group; }" in page


def test_no_spare_bibs_means_no_signup_header():
    page = pickup_sheet([{"bib": 110, **runner("Lark", "Zane")}], "T", spare_bibs=[], spare="none")
    assert "Race day signups" not in page and '<tr class="blank">' not in page


# ------------------------------------------------------------------ roster on the pairing screen


def test_the_pairing_screen_shows_the_runner_or_spare(tmp_path):
    roster_file = tmp_path / "registration_bibs.csv"
    roster_file.write_text(
        "bib,first_name,last_name,age,gender,event,tshirt\n"
        "110,Jane,Doe,30,F,5K (Adult),M\n"
        "111,Sam,Rook,12,M,5K (Junior),\n",
        encoding="utf-8",
    )
    roster = load_roster(roster_file)
    assert bib_label(110, roster) == "Bib 110 - Jane Doe"
    assert bib_label(111, roster) == "Bib 111 - Sam Rook"
    assert bib_label(290, roster) == "Bib 290 - spare"
    assert bib_label(110, None) == "Bib 110"


def test_the_roster_defaults_to_data_registration_bibs_when_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_roster(None) is None
    (tmp_path / "Data").mkdir()
    (tmp_path / "Data" / "registration_bibs.csv").write_text(
        "bib,first_name,last_name\n150,Nell,Fable\n", encoding="utf-8"
    )
    assert resolve_roster(None) == {150: "Nell Fable"}
    other = tmp_path / "other.csv"
    other.write_text("bib,first_name,last_name\n151,Otto,Penn\n", encoding="utf-8")
    assert resolve_roster(other) == {151: "Otto Penn"}


def test_the_roster_merges_into_201_importable_participants(tmp_path):
    from db import parse_participant_csv
    from merge import load_registration, merge, write_participants
    from pair import load_pairs

    export = tmp_path / "export.csv"
    write_export(export, [(f"Runner{i}", f"Test{i:03d}") for i in range(173)])
    roster = tmp_path / "registration_bibs.csv"
    assert main([str(export), "--out", str(roster), "--sheet", str(tmp_path / "sheet.html")]) == 0

    pairs = tmp_path / "pairs.csv"
    pairs.write_text("bib,epc1\n" + "".join(f"{b},E28068940000{b:012X}\n" for b in range(110, 311)))
    people, no_bib = load_registration(roster)
    rows, unpaired = merge(people, load_pairs(pairs))
    assert (no_bib, unpaired) == (0, [])

    out = tmp_path / "participants.csv"
    write_participants(out, rows)
    imported = parse_participant_csv(out.read_text(encoding="utf-8"))
    assert len(imported) == 201
    assert sum(1 for row in imported if row["last_name"]) == 173
    assert imported[0]["bib"] == "110" and imported[-1]["bib"] == "310"
    assert imported[-1]["first_name"] == "" and imported[-1]["epcs"] == ["E28068940000000000000136"]


def test_registration_ids_are_kept_and_exported_for_the_bib_import(tmp_path, capsys):
    export = tmp_path / "export.csv"
    write_export(export, [("Tova", "Zeller"), ("Ike", "Amsel"), ("Juno", "Marsh")])
    assert main([str(export)]) == 0  # defaults, inside the temp folder
    rows = read_roster(tmp_path / "Data" / "registration_bibs.csv")
    assert [(r["bib"], r["last_name"], r["registration_id"]) for r in rows] == [
        (110, "Amsel", "9001"), (111, "Marsh", "9002"), (112, "Zeller", "9000")]
    lines = (tmp_path / "Data" / "runsignup_bib_import.csv").read_text(encoding="utf-8").splitlines()
    assert lines == ["Registration ID,Bib", "9001,110", "9002,111", "9000,112"]
    assert "Bib import: " in capsys.readouterr().out


def test_an_older_roster_gets_ids_filled_in_without_moving_a_bib(tmp_path, capsys):
    export = tmp_path / "export.csv"
    write_export(export, [("Tova", "Zeller"), ("Ike", "Amsel"), ("Abe", "Aaron")])
    roster = tmp_path / "Data" / "registration_bibs.csv"
    roster.parent.mkdir()
    # Written before IDs were recorded, and before Abe Aaron registered.
    roster.write_text(
        "bib,first_name,last_name,age,gender,event,tshirt\n"
        "110,Ike,Amsel,30,F,5K (Adult),M\n"
        "111,Tova,Zeller,30,F,5K (Adult),M\n",
        encoding="utf-8",
    )
    assert main([str(export)]) == 0
    rows = read_roster(roster)
    assert [(r["bib"], r["last_name"], r["registration_id"]) for r in rows] == [
        (110, "Amsel", "9001"), (111, "Zeller", "9000"), (112, "Aaron", "9002")]
    printed = capsys.readouterr().out
    assert "Filled in 2 registration IDs" in printed
    assert "New runners given bibs: 112." in printed


def test_a_roster_row_missing_from_the_export_fills_nothing(tmp_path):
    from assign_bibs import backfill_registration_ids

    rows = [{"bib": 110, "registration_id": "", **runner("Ike", "Amsel")},
            {"bib": 111, "registration_id": "", **runner("Gone", "Away")}]
    runners = [{"registration_id": "9001", **runner("Ike", "Amsel")}]
    with pytest.raises(AssignError, match="bib 111"):
        backfill_registration_ids(rows, runners)
    assert rows[0]["registration_id"] == ""


# ------------------------------------------------------------------ late registrants


def write_export_with_bibs(path, rows):
    """(registration ID, first, last, bib registration holds or "")"""
    path.write_text(
        EXPORT_HEADER
        + "".join(f"{rid},{first},,{last},{bib},F,30,M,5K (Adult)\n" for rid, first, last, bib in rows),
        encoding="utf-8",
    )


def roster_of(tmp_path, *rows):
    roster = tmp_path / "Data" / "registration_bibs.csv"
    roster.parent.mkdir(exist_ok=True)
    roster.write_text(
        "bib,first_name,last_name,age,gender,event,tshirt,registration_id\n"
        + "".join(f"{bib},{first},{last},30,F,5K (Adult),M,{rid}\n" for bib, first, last, rid in rows),
        encoding="utf-8",
    )
    return roster


def test_a_new_runner_keeps_the_bib_registration_holds_and_the_rest_take_the_next_free(tmp_path, capsys):
    roster = roster_of(tmp_path, (110, "Ike", "Amsel", "9001"), (111, "Tova", "Zeller", "9000"))
    export = tmp_path / "export.csv"
    write_export_with_bibs(export, [
        ("9000", "Tova", "Zeller", "111"),
        ("9001", "Ike", "Amsel", "110"),
        ("9002", "Wren", "Yates", ""),
        ("9003", "Hand", "Picked", "113"),  # given out by hand in registration
        ("9004", "Abe", "Aaron", ""),
    ])
    assert main([str(export), "--first", "110", "--last", "120"]) == 0
    assert [(r["bib"], r["registration_id"]) for r in read_roster(roster)] == [
        (110, "9001"), (111, "9000"), (112, "9004"), (113, "9003"), (114, "9002")]
    printed = capsys.readouterr().out
    assert "Runners before: 2. Now: 5. New runners given bibs: 112-114." in printed
    assert "Spare for day-of signups: 115-120 (6 bibs)." in printed
    lines = (tmp_path / "Data" / "runsignup_bib_import.csv").read_text(encoding="utf-8").splitlines()
    assert lines[1:] == ["9001,110", "9000,111", "9004,112", "9003,113", "9002,114"]


def test_a_registration_bib_that_differs_from_the_roster_writes_nothing(tmp_path, capsys):
    roster = roster_of(tmp_path, (110, "Ike", "Amsel", "9001"), (111, "Tova", "Zeller", "9000"))
    before = roster.read_bytes()
    export = tmp_path / "export.csv"
    write_export_with_bibs(export, [("9000", "Tova", "Zeller", "115"), ("9001", "Ike", "Amsel", "110"),
                                    ("9002", "Abe", "Aaron", "")])
    assert main([str(export)]) == 1
    assert roster.read_bytes() == before
    assert not (tmp_path / "Data" / "pickup_sheet.html").exists()
    printed = capsys.readouterr().out
    assert "roster 111 vs registration 115" in printed and "Zeller" not in printed


def test_a_new_runner_holding_a_bib_already_in_the_roster_is_refused(tmp_path):
    from assign_bibs import add_new_runners

    rows = [{"bib": 110, "registration_id": "9001", **runner("Ike", "Amsel")}]
    runners = [{"registration_id": "9001", "registration_bib": "110", **runner("Ike", "Amsel")},
               {"registration_id": "9002", "registration_bib": "110", **runner("Abe", "Aaron")}]
    with pytest.raises(AssignError, match="registration bib 110"):
        add_new_runners(rows, runners, 110, 120)
    assert len(rows) == 1
