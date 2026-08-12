"""A simulated R420 that produces a plausible 5K worth of tag reads.

This is the test harness for the whole system. Every timing rule in timing.py
is exercised by something this module deliberately generates:

  - runners standing in the start line read zone for minutes before the gun
  - a mass start where the back of the pack crosses the line well after the gun
  - dozens of reads per crossing rather than one clean read
  - individual reads dropped, and occasionally a whole tag failing to read
  - stray reads from people loitering near the finish arch early in the race
  - a couple of starters who never finish, and someone whose start was missed

The plan is built once, at construction, from a single seeded Random. Read
times are stored as offsets in seconds relative to the gun, so the same plan can
be rebased onto any gun time and still be bit for bit repeatable.
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, Sequence

from reader import (
    FINISH_ANTENNAS,
    START_ANTENNAS,
    Reader,
    TagRead,
    normalize_epc,
)

START_PORTS = sorted(START_ANTENNAS)
FINISH_PORTS = sorted(FINISH_ANTENNAS)

MICROS = 1_000_000


@dataclass
class SimParticipant:
    """The only thing the simulator needs to know about a runner."""

    bib: str
    epcs: list[str] = field(default_factory=list)


def participants_from_rows(rows: Sequence[dict]) -> list[SimParticipant]:
    """Build simulator participants from db rows or plain dicts.

    Accepts either {"bib": ..., "epcs": [...]} or {"bib": ..., "epc1": ..., "epc2": ...}.
    """
    people: list[SimParticipant] = []
    for row in rows:
        epcs = row.get("epcs")
        if epcs is None:
            epcs = [row.get("epc1"), row.get("epc2")]
        clean = [normalize_epc(e) for e in epcs if normalize_epc(e)]
        if not clean:
            continue
        people.append(SimParticipant(bib=str(row["bib"]), epcs=clean))
    return people


def make_participants(count: int, start_bib: int = 100, dual_tag_ratio: float = 0.5,
                      seed: int = 1) -> list[SimParticipant]:
    """Generate a synthetic field. Handy for tests and for --simulate demos."""
    rng = random.Random(seed)
    people = []
    for index in range(count):
        bib = str(start_bib + index)
        epcs = [f"E280{int(bib):08X}0001"]
        if rng.random() < dual_tag_ratio:
            epcs.append(f"E280{int(bib):08X}0002")
        people.append(SimParticipant(bib=bib, epcs=epcs))
    return people


@dataclass
class _RunnerPlan:
    person: SimParticipant
    start_offset: float          # seconds after the gun that they cross the start
    net_seconds: float           # their own start line to finish line time
    dnf: bool
    missed_start: bool


class SimulatedReader(Reader):
    """Reader implementation that plays back a generated race.

    Two ways to consume it:

      generate(gun_utc)  -> the entire read log at once, for tests
      reads()            -> paced playback for demos, driven by trigger_start()

    speed compresses playback only. The timestamps carried on the reads are
    real race times, so a demo that finishes in half a minute still produces
    28 minute 5K results.

    clock_skew_seconds models the thing a real reader does and a naive
    simulator does not: the reader stamps reads from its own clock, which is
    not the Pi's clock. A positive skew puts the reader ahead of the Pi. It is
    applied to every timestamp this reader emits, exactly as a real reader
    with a wrong clock would, and is reported through clock_offset_micros the
    same way LLRPReader reports its measured offset.
    """

    def __init__(
        self,
        participants: Sequence[SimParticipant],
        seed: int = 1,
        speed: float = 60.0,
        clock_skew_seconds: float = 0.0,
        pre_gun_seconds: float = 300.0,
        drop_probability: float = 0.15,
        tag_failure_probability: float = 0.04,
        dnf_count: int = 2,
        missed_start_count: int = 1,
        stray_finish_runners: int = 8,
        median_net_seconds: float = 1680.0,
        fastest_net_seconds: float = 960.0,
        slowest_net_seconds: float = 4200.0,
    ):
        if speed <= 0:
            raise ValueError("speed must be positive")
        self.participants = list(participants)
        self.seed = seed
        self.speed = speed
        self.clock_skew_seconds = clock_skew_seconds
        self._skew_micros = int(round(clock_skew_seconds * MICROS))
        self.pre_gun_seconds = pre_gun_seconds
        self.drop_probability = drop_probability
        self.tag_failure_probability = tag_failure_probability
        self.dnf_count = dnf_count
        self.missed_start_count = missed_start_count
        self.stray_finish_runners = stray_finish_runners
        self.median_net_seconds = median_net_seconds
        self.fastest_net_seconds = fastest_net_seconds
        self.slowest_net_seconds = slowest_net_seconds

        self._stopped = threading.Event()
        self._started = threading.Event()
        self._gun_utc: int | None = None

        rng = random.Random(seed)
        self._plans = self._plan_runners(rng)
        # (epc, antenna_port, offset_seconds_from_gun, rssi)
        self._pre_gun: list[tuple[str, int, float, float]] = []
        self._race: list[tuple[str, int, float, float]] = []
        self._build(rng)

    # ------------------------------------------------------------------
    # planning
    # ------------------------------------------------------------------

    def _plan_runners(self, rng: random.Random) -> list[_RunnerPlan]:
        order = list(self.participants)
        rng.shuffle(order)

        # Whoever lines up at the back takes longer to reach the line. This is
        # the whole reason net time exists, so it needs to be visible in the
        # generated data.
        per_person_delay = 0.18

        dnf_picks = set(rng.sample(range(len(order)), min(self.dnf_count, len(order))))
        remaining = [i for i in range(len(order)) if i not in dnf_picks]
        missed_picks = set(
            rng.sample(remaining, min(self.missed_start_count, len(remaining)))
        )

        plans = []
        for position, person in enumerate(order):
            start_offset = 1.5 + position * per_person_delay + rng.uniform(0.0, 2.5)
            net = self.median_net_seconds * math.exp(rng.gauss(0.0, 0.28))
            net = max(self.fastest_net_seconds, min(self.slowest_net_seconds, net))
            plans.append(
                _RunnerPlan(
                    person=person,
                    start_offset=start_offset,
                    net_seconds=net,
                    dnf=position in dnf_picks,
                    missed_start=position in missed_picks,
                )
            )
        return plans

    def _build(self, rng: random.Random) -> None:
        for plan in self._plans:
            self._build_milling(plan, rng)

        for plan in self._plans:
            if not plan.missed_start:
                self._burst(plan.person, START_PORTS, plan.start_offset, self._race, rng)
            if not plan.dnf:
                finish_offset = plan.start_offset + plan.net_seconds
                self._burst(plan.person, FINISH_PORTS, finish_offset, self._race, rng)

        self._build_strays(rng)

        self._pre_gun.sort(key=lambda item: item[2])
        self._race.sort(key=lambda item: item[2])

    def _build_milling(self, plan: _RunnerPlan, rng: random.Random) -> None:
        """Runners drift in and out of the start line read zone before the gun.

        These reads are real and get logged, but timing must ignore them.
        """
        for _ in range(rng.randint(0, 4)):
            offset = -rng.uniform(8.0, self.pre_gun_seconds)
            self._burst(
                plan.person,
                START_PORTS,
                offset,
                self._pre_gun,
                rng,
                min_reads=3,
                max_reads=14,
                max_dwell=4.0,
            )

    def _build_strays(self, rng: random.Random) -> None:
        """Isolated reads from people standing near the finish arch.

        Placed inside the first ten minutes, which is well inside the default
        720 second minimum elapsed, so a correct implementation throws them out.
        """
        if not self._plans:
            return
        picks = rng.sample(
            self._plans, min(self.stray_finish_runners, len(self._plans))
        )
        for plan in picks:
            for _ in range(rng.randint(1, 3)):
                offset = rng.uniform(30.0, 600.0)
                epc = rng.choice(plan.person.epcs)
                self._race.append(
                    (epc, rng.choice(FINISH_PORTS), offset, round(rng.uniform(-78, -62), 1))
                )
            # A few of them wander past the arch before the race even starts.
            if rng.random() < 0.4:
                offset = -rng.uniform(20.0, self.pre_gun_seconds)
                epc = rng.choice(plan.person.epcs)
                self._pre_gun.append(
                    (epc, rng.choice(FINISH_PORTS), offset, round(rng.uniform(-78, -62), 1))
                )

    def _burst(
        self,
        person: SimParticipant,
        ports: Sequence[int],
        crossing_offset: float,
        sink: list,
        rng: random.Random,
        min_reads: int = 12,
        max_reads: int = 45,
        max_dwell: float = 2.6,
    ) -> None:
        """One tag passing an antenna pair, as a scatter of reads over the dwell."""
        for epc in person.epcs:
            if rng.random() < self.tag_failure_probability:
                # Tag never woke up on this pass. If the runner has a second
                # tag they are still timed, which is the point of dual tags.
                continue
            dwell = rng.uniform(1.2, max_dwell)
            count = rng.randint(min_reads, max_reads)
            for _ in range(count):
                if rng.random() < self.drop_probability:
                    continue
                position = rng.random()
                offset = crossing_offset + position * dwell
                # Signal is strongest as the runner passes the antenna.
                rssi = -72.0 + 22.0 * math.sin(math.pi * position) + rng.uniform(-3.0, 3.0)
                sink.append((epc, rng.choice(list(ports)), offset, round(rssi, 1)))

    # ------------------------------------------------------------------
    # consumption
    # ------------------------------------------------------------------

    @property
    def clock_offset_micros(self) -> int:
        """Reader clock minus Pi clock, in microseconds.

        A real reader has to measure this. A simulated one already knows it,
        because it is the skew it was told to apply.
        """
        return self._skew_micros

    @property
    def clock_offset_source(self) -> str:
        return "simulated"

    def _rebase(self, entries, gun_utc: int) -> list[TagRead]:
        """Offsets to absolute times, in the reader's clock domain.

        gun_utc is a Pi clock instant. The reader stamps its reads from its own
        clock, so the skew lands on every timestamp that leaves here.
        """
        return [
            TagRead(
                epc=epc,
                antenna_port=port,
                first_seen_utc=gun_utc + int(round(offset * MICROS)) + self._skew_micros,
                rssi=rssi,
            )
            for epc, port, offset, rssi in entries
        ]

    def generate(self, gun_utc: int) -> list[TagRead]:
        """The complete read log for the race, pre gun reads included."""
        reads = self._rebase(self._pre_gun, gun_utc) + self._rebase(self._race, gun_utc)
        reads.sort(key=lambda r: r.first_seen_utc)
        return reads

    def pre_gun_reads(self, gun_utc: int) -> list[TagRead]:
        return self._rebase(self._pre_gun, gun_utc)

    def race_reads(self, gun_utc: int) -> list[TagRead]:
        return self._rebase(self._race, gun_utc)

    @property
    def expected_dnf_bibs(self) -> list[str]:
        return [p.person.bib for p in self._plans if p.dnf]

    @property
    def expected_review_bibs(self) -> list[str]:
        return [p.person.bib for p in self._plans if p.missed_start and not p.dnf]

    def trigger_start(self, gun_utc: int) -> None:
        """Fire the gun. Playback of the race itself begins from here."""
        self._gun_utc = gun_utc
        self._started.set()

    def _sleep(self, seconds: float) -> None:
        """Sleep in slices so stop() takes effect promptly."""
        deadline = time.monotonic() + seconds
        while not self._stopped.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    def reads(self) -> Iterator[TagRead]:
        rng = random.Random(self.seed ^ 0x5EED)

        # Before the gun, keep the start line busy so the operator sees the
        # reader is alive while the field assembles.
        while not self._stopped.is_set() and not self._started.is_set():
            if self.participants:
                plan = rng.choice(self._plans)
                now_utc = int(time.time() * MICROS) + self._skew_micros
                for epc in plan.person.epcs:
                    for index in range(rng.randint(2, 8)):
                        yield TagRead(
                            epc=epc,
                            antenna_port=rng.choice(START_PORTS),
                            first_seen_utc=now_utc + index * rng.randint(30_000, 90_000),
                            rssi=round(rng.uniform(-78.0, -55.0), 1),
                        )
            self._sleep(rng.uniform(0.2, 0.8))

        if self._stopped.is_set() or self._gun_utc is None:
            return

        started_wall = time.monotonic()
        for read in self.race_reads(self._gun_utc):
            if self._stopped.is_set():
                return
            # Pacing is wall clock work, so the skew comes back off here. It
            # belongs on the timestamp, not on when the read is handed over.
            offset_seconds = (
                read.first_seen_utc - self._gun_utc - self._skew_micros
            ) / MICROS
            due = started_wall + offset_seconds / self.speed
            wait = due - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            if self._stopped.is_set():
                return
            yield read

    def stop(self) -> None:
        self._stopped.set()
