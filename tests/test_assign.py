"""Bib assignment, the pickup sheet, and runner names on the pairing screen.

Every name here is made up. Nothing reads the real registration export.
"""

from __future__ import annotations

import csv

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
                        "event": "5K (Junior)", "tshirt": "S", "registration_id": "9001"}]
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

    # A late registrant sorts first; reassigning would shift everyone's bib.
    write_export(export, [("Tova", "Zeller"), ("Ike", "Amsel"), ("Abe", "Aaron")])
    main([str(export), "--out", str(roster), "--sheet", str(sheet)])
    assert [(r["bib"], r["last_name"]) for r in read_roster(roster)] == [(110, "Amsel"), (111, "Zeller")]
    assert "already assigned" in capsys.readouterr().out

    main([str(export), "--out", str(roster), "--sheet", str(sheet), "--force"])
    assert [r["last_name"] for r in read_roster(roster)] == ["Aaron", "Amsel", "Zeller"]


def test_the_pickup_sheet_is_sorted_escaped_and_has_blank_rows():
    rows = [
        {"bib": 111, **runner("Lark", "Zane", tshirt="XL")},
        {"bib": 110, **runner("Bo", "<O'Hara & Co>", event="5K (Junior)")},
    ]
    page = pickup_sheet(rows, "Test 5K - Bib pickup", blank_rows=3, spare="112-140")
    assert page.index("&lt;O&#x27;Hara &amp; Co&gt;") < page.index("Zane")
    assert "<O'Hara" not in page
    assert page.count('<tr class="blank">') == 3
    for heading in ("Last", "First", "Bib", "Event", "T-Shirt"):
        assert f"<th>{heading}</th>" in page
    assert page.count('<span class="box"></span>') == 5
    assert "spare bibs 112-140" in page


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
        (110, "Amsel", "9001"), (111, "Zeller", "9000")]
    printed = capsys.readouterr().out
    assert "Filled in 2 registration IDs" in printed
    assert "1 runners in the export have no bib yet" in printed


def test_a_roster_row_missing_from_the_export_fills_nothing(tmp_path):
    from assign_bibs import backfill_registration_ids

    rows = [{"bib": 110, "registration_id": "", **runner("Ike", "Amsel")},
            {"bib": 111, "registration_id": "", **runner("Gone", "Away")}]
    runners = [{"registration_id": "9001", **runner("Ike", "Amsel")}]
    with pytest.raises(AssignError, match="bib 111"):
        backfill_registration_ids(rows, runners)
    assert rows[0]["registration_id"] == ""
