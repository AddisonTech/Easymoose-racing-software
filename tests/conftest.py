"""Shared fixtures.

GUN is a fixed instant rather than time.time() so a failure reproduces
exactly, and every read in a test is written as an offset from it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import RaceDB  # noqa: E402
from reader import TagRead  # noqa: E402
from simulator import SimulatedReader, make_participants  # noqa: E402

GUN = 1_700_000_000_000_000  # microseconds since the epoch
MICROS = 1_000_000

START_PORT = 1
OTHER_START_PORT = 2
FINISH_PORT = 3
OTHER_FINISH_PORT = 4

TAG_A = "E28011700000020000000001"
TAG_B = "E28011700000020000000002"


def at(seconds: float) -> int:
    """A timestamp this many seconds after the gun. Negative is before it."""
    return GUN + int(round(seconds * MICROS))


def read(epc: str, port: int, seconds: float, rssi: float = -60.0) -> TagRead:
    return TagRead(epc=epc, antenna_port=port, first_seen_utc=at(seconds), rssi=rssi)


def burst(epc: str, port: int, start_seconds: float, count: int = 20,
          spacing: float = 0.05) -> list[TagRead]:
    """A crossing as the reader really sees it: a scatter of reads."""
    return [read(epc, port, start_seconds + index * spacing) for index in range(count)]


def csv_for(people) -> str:
    """A participant CSV covering a simulator field."""
    lines = ["bib,first_name,last_name,age,gender,epc1,epc2"]
    for index, person in enumerate(people):
        epcs = list(person.epcs) + [""]
        lines.append(
            f"{person.bib},Runner,Number{index},{30 + index % 40},"
            f"{'F' if index % 2 else 'M'},{epcs[0]},{epcs[1]}"
        )
    return "\n".join(lines) + "\n"


@pytest.fixture
def sim_race():
    """A generated 40 runner race: the participants and the whole read log."""
    people = make_participants(40, seed=7)
    sim = SimulatedReader(people, seed=7)
    return sim, people, sim.generate(GUN)


@pytest.fixture
def race_db(tmp_path):
    """An empty race in a temporary races root."""
    db = RaceDB.create("Test Race", "2026-08-12", "5K", 720, root=tmp_path / "races")
    yield db
    db.close()
