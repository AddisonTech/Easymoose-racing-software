"""Turning sllurp tag reports into TagReads.

The EPC values here are what sllurp 3 delivered from the R420: hex text
carried as ASCII bytes.
"""

from __future__ import annotations

from db import parse_participant_csv
from reader import normalize_epc, tag_report_to_read

LIVE_EPC_BYTES = b"e28011b0a5050076e8fe9942"
LIVE_EPC = "E28011B0A5050076E8FE9942"


def live_report(epc=LIVE_EPC_BYTES):
    return {
        "EPC": epc,
        "AntennaID": 1,
        "PeakRSSI": -30,
        "FirstSeenTimestampUTC": 1_790_000_000_000_000,
        "TagSeenCount": 1,
    }


def test_hexlified_bytes_from_sllurp_are_decoded_not_encoded_again():
    assert normalize_epc(LIVE_EPC_BYTES) == LIVE_EPC
    assert normalize_epc(bytearray(LIVE_EPC_BYTES)) == LIVE_EPC


def test_raw_epc_bytes_are_still_hex_encoded():
    assert normalize_epc(bytes.fromhex("E28011B0A5050076E8FE9942")) == LIVE_EPC


def test_a_live_report_matches_the_epc_in_a_participant_csv():
    read = tag_report_to_read(live_report())
    assert read.epc == LIVE_EPC
    rows = parse_participant_csv(
        "bib,first_name,last_name,age,gender,epc1,epc2\n"
        "110,Ada,Lovelace,36,F,e280 11b0 a505 0076 e8fe 9942,\n"
    )
    assert read.epc in rows[0]["epcs"]


def test_an_epc_96_report_is_decoded_the_same_way():
    report = live_report()
    report["EPC-96"] = report.pop("EPC")
    assert tag_report_to_read(report).epc == LIVE_EPC
