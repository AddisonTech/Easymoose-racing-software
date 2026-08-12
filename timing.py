"""Turning a log of tag reads into race results.

Everything here is a pure function over a list of TagRead. Nothing touches the
database, the clock or the network, so the whole ruleset can be tested against
a generated read log without any hardware.

The rules, in the order they are applied:

  gun time     Recorded by the operator when the race starts.

  start        The first read of any of the participant's EPCs on a start
               antenna at or after the gun. Reads before the gun are logged but
               ignored, because the field stands on the mat for several minutes
               beforehand. Someone already on the mat when the gun fires gets a
               start of essentially the gun time, which is what we want.

  finish       The first crossing of the finish line at least
               min_elapsed_seconds after that participant's own start. The
               default of 720 seconds throws out reads picked up from people
               milling near the finish arch early in the race.

  bursts       One tag passing an antenna generates dozens of reads. Reads less
               than 2 seconds apart are one burst, and the earliest read in the
               burst is the crossing.

  dual tags    A participant may carry two EPCs. Whichever gives the earlier
               valid crossing wins.

Note the deliberate asymmetry between the two lines. The gun cutoff is applied
to raw reads before bursts are formed, because a runner standing on the mat is
mid burst when the gun fires and we still want to time them. The minimum
elapsed cutoff is applied to formed crossings, because there the whole point is
to reject a person loitering by the arch; if their burst straddled the cutoff,
filtering raw reads first would hand back a bogus finish at exactly the cutoff
instant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from reader import FINISH_ANTENNAS, START_ANTENNAS, TagRead

MICROS = 1_000_000

BURST_GAP_SECONDS = 2.0
DEFAULT_MIN_ELAPSED_SECONDS = 720

STATUS_FINISHED = "finished"
STATUS_DNF = "dnf"
STATUS_REVIEW = "review"
STATUS_NOT_STARTED = "not_started"


@dataclass(frozen=True)
class Participant:
    """Just enough of a participant for the timing rules to run."""

    participant_id: int
    bib: str
    epcs: tuple[str, ...]


@dataclass(frozen=True)
class ParticipantResult:
    participant_id: int
    bib: str
    start_utc: int | None
    finish_utc: int | None
    elapsed_seconds: float | None
    status: str


def collapse_bursts(
    timestamps: Iterable[int], gap_seconds: float = BURST_GAP_SECONDS
) -> list[int]:
    """Collapse a stream of read times into crossings.

    Consecutive reads less than gap_seconds apart belong to the same burst and
    the earliest read in that burst is returned as the crossing. The gap is
    measured from the previous read rather than from the start of the burst, so
    a runner who lingers in the field extends one burst instead of generating a
    string of phantom crossings.
    """
    ordered = sorted(timestamps)
    if not ordered:
        return []

    gap_us = int(round(gap_seconds * MICROS))
    crossings = [ordered[0]]
    previous = ordered[0]
    for stamp in ordered[1:]:
        if stamp - previous >= gap_us:
            crossings.append(stamp)
        previous = stamp
    return crossings


def index_reads_by_epc(reads: Iterable[TagRead]) -> dict[str, list[TagRead]]:
    """Group reads by EPC, each list sorted by time."""
    index: dict[str, list[TagRead]] = {}
    for read in reads:
        index.setdefault(read.epc, []).append(read)
    for entries in index.values():
        entries.sort(key=lambda read: read.first_seen_utc)
    return index


def crossings_at_line(
    reads: Sequence[TagRead],
    antennas: frozenset[int],
    not_before: int | None = None,
    gap_seconds: float = BURST_GAP_SECONDS,
) -> list[int]:
    """Crossing times for one tag at one line.

    not_before drops raw reads before that timestamp prior to forming bursts.
    """
    stamps = [
        read.first_seen_utc
        for read in reads
        if read.antenna_port in antennas
        and (not_before is None or read.first_seen_utc >= not_before)
    ]
    return collapse_bursts(stamps, gap_seconds)


def start_crossing(
    reads: Sequence[TagRead],
    gun_time_utc: int,
    gap_seconds: float = BURST_GAP_SECONDS,
) -> int | None:
    """First start line crossing at or after the gun, for a single tag."""
    crossings = crossings_at_line(reads, START_ANTENNAS, gun_time_utc, gap_seconds)
    return crossings[0] if crossings else None


def finish_crossing(
    reads: Sequence[TagRead],
    start_utc: int,
    min_elapsed_seconds: float = DEFAULT_MIN_ELAPSED_SECONDS,
    gap_seconds: float = BURST_GAP_SECONDS,
) -> int | None:
    """First finish line crossing far enough after this tag's start.

    Bursts are formed over every finish read, then the cutoff is applied to the
    crossings, so a burst that begins before the cutoff is rejected whole.
    """
    cutoff = start_utc + int(round(min_elapsed_seconds * MICROS))
    for crossing in crossings_at_line(reads, FINISH_ANTENNAS, None, gap_seconds):
        if crossing >= cutoff:
            return crossing
    return None


def _earliest(values: Iterable[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def compute_result(
    participant: Participant,
    reads_by_epc: dict[str, list[TagRead]],
    gun_time_utc: int,
    min_elapsed_seconds: float = DEFAULT_MIN_ELAPSED_SECONDS,
    gap_seconds: float = BURST_GAP_SECONDS,
) -> ParticipantResult:
    """Apply the full ruleset to one participant."""
    tag_reads = [reads_by_epc.get(epc, []) for epc in participant.epcs]

    start_utc = _earliest(
        start_crossing(reads, gun_time_utc, gap_seconds) for reads in tag_reads
    )

    finish_utc = None
    if start_utc is not None:
        finish_utc = _earliest(
            finish_crossing(reads, start_utc, min_elapsed_seconds, gap_seconds)
            for reads in tag_reads
        )

    if start_utc is not None and finish_utc is not None:
        elapsed = round((finish_utc - start_utc) / MICROS, 2)
        status = STATUS_FINISHED
    elif start_utc is not None:
        elapsed = None
        status = STATUS_DNF
    else:
        # No start. If the tag was seen at the finish line at all we cannot
        # guess what their start was, so it goes to a human.
        elapsed = None
        seen_at_finish = _earliest(
            (crossings_at_line(reads, FINISH_ANTENNAS, gun_time_utc, gap_seconds) or [None])[0]
            for reads in tag_reads
        )
        finish_utc = seen_at_finish
        status = STATUS_REVIEW if seen_at_finish is not None else STATUS_NOT_STARTED

    return ParticipantResult(
        participant_id=participant.participant_id,
        bib=participant.bib,
        start_utc=start_utc,
        finish_utc=finish_utc,
        elapsed_seconds=elapsed,
        status=status,
    )


def compute_results(
    reads: Iterable[TagRead],
    participants: Sequence[Participant],
    gun_time_utc: int | None,
    min_elapsed_seconds: float = DEFAULT_MIN_ELAPSED_SECONDS,
    gap_seconds: float = BURST_GAP_SECONDS,
) -> list[ParticipantResult]:
    """Results for the whole field, in bib order.

    A race with no gun time has not started, so nobody has a result yet.
    """
    if gun_time_utc is None:
        return [
            ParticipantResult(p.participant_id, p.bib, None, None, None, STATUS_NOT_STARTED)
            for p in participants
        ]

    reads_by_epc = index_reads_by_epc(reads)
    return [
        compute_result(p, reads_by_epc, gun_time_utc, min_elapsed_seconds, gap_seconds)
        for p in participants
    ]


def order_finishers(results: Iterable[ParticipantResult]) -> list[ParticipantResult]:
    """Finishers fastest first. Ties broken by who crossed the line first."""
    finishers = [r for r in results if r.status == STATUS_FINISHED]
    finishers.sort(key=lambda r: (r.elapsed_seconds, r.finish_utc))
    return finishers


def places(results: Iterable[ParticipantResult]) -> dict[int, int]:
    """Map participant id to overall place. Only finishers get a place."""
    return {
        result.participant_id: index + 1
        for index, result in enumerate(order_finishers(results))
    }


def summarize(results: Iterable[ParticipantResult]) -> dict[str, int]:
    """Counts for the race console header."""
    results = list(results)
    started = sum(1 for r in results if r.start_utc is not None)
    finished = sum(1 for r in results if r.status == STATUS_FINISHED)
    return {
        "registered": len(results),
        "started": started,
        "finished": finished,
        "on_course": started - finished,
        "review": sum(1 for r in results if r.status == STATUS_REVIEW),
        "not_started": sum(1 for r in results if r.status == STATUS_NOT_STARTED),
    }


def format_elapsed(seconds: float | None) -> str:
    """Elapsed time as h:mm:ss.hh, or m:ss.hh under an hour."""
    if seconds is None:
        return ""
    hundredths = int(round(seconds * 100))
    whole, hundredths = divmod(hundredths, 100)
    minutes, secs = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}.{hundredths:02d}"
    return f"{minutes}:{secs:02d}.{hundredths:02d}"
