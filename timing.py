"""Turning a log of tag reads into race results.

Everything here is a pure function over a list of TagRead. Nothing touches the
database, the clock or the network, so the whole ruleset can be tested against
a generated read log without any hardware.

The rules, in the order they are applied:

  gun time     Recorded by the operator when the race starts, and converted
               into the reader's clock domain before it gets here. Reads are
               stamped by the reader and the gun is taken from the Pi, so
               comparing the two raw would be comparing two unrelated clocks.
               Everything below expects gun_time_reader_utc.

  start        The first read of any of the participant's EPCs on a start
               antenna at or after the gun. Reads before the gun are logged but
               ignored, because the field stands in the start read zone for
               several minutes beforehand. Someone already in the zone when the
               gun fires gets a
               start of essentially the gun time, which is what we want.

  finish       The first crossing of the finish line at least
               min_elapsed_seconds after that participant's own start. The
               default of 720 seconds throws out reads picked up from people
               milling near the finish arch early in the race.

  two times    Every finisher has a chip time (elapsed_seconds: their own
               start to their finish) and a gun time (gun_seconds: the gun to
               their finish). Overall places and overall awards go by gun
               time; age group results go by chip time.

  gun fallback A runner with no start read but a finish crossing at least
               min_elapsed_seconds after the gun is timed from the gun. A
               tightly packed start can hide tags from the start antennas, and
               those runners still ran the race. Their result is marked
               source "gun" so the console can show which times are not chip
               times.

  bursts       One tag passing an antenna generates dozens of reads. Reads less
               than 2 seconds apart are one burst, and the earliest read in the
               burst is the crossing.

  dual tags    A participant may carry two EPCs. Whichever gives the earlier
               valid crossing wins.

Note the deliberate asymmetry between the two lines. The gun cutoff is applied
to raw reads before bursts are formed, because a runner standing in the zone is
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
STATUS_REMOVED = "removed"

# Where a finisher's elapsed time came from.
SOURCE_CHIP = "chip"      # their own start read to their finish read
SOURCE_GUN = "gun"        # no start read, so the gun to their finish read
SOURCE_MANUAL = "manual"  # set by the operator

# Display and upload categories. Juniors are by age alone, both genders.
JUNIOR_MAX_AGE = 12
CATEGORY_ADULT_MALE = "adult_male"
CATEGORY_ADULT_FEMALE = "adult_female"
CATEGORY_JUNIOR = "junior"
CATEGORIES = (CATEGORY_ADULT_MALE, CATEGORY_ADULT_FEMALE, CATEGORY_JUNIOR)


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
    source: str = SOURCE_CHIP
    gun_seconds: float | None = None


# How finishers are ranked. Overall places and awards go by gun time, age
# group results by chip time.
BY_GUN = "gun"
BY_CHIP = "chip"


@dataclass(frozen=True)
class Adjustment:
    """An operator correction, laid over the results computed from reads.

    kind "time" makes the participant a finisher with this elapsed time;
    finish_utc is when they crossed, in the reader's clock, and orders them in
    the finish list. kind "removed" takes them out of the results. Reads are
    never changed, so clearing the adjustment gives back the computed result.
    """

    participant_id: int
    kind: str
    elapsed_seconds: float | None = None
    finish_utc: int | None = None

    TIME = "time"
    REMOVED = "removed"


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
    gun_time_reader_utc: int,
    gap_seconds: float = BURST_GAP_SECONDS,
) -> int | None:
    """First start line crossing at or after the gun, for a single tag."""
    crossings = crossings_at_line(reads, START_ANTENNAS, gun_time_reader_utc, gap_seconds)
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
    gun_time_reader_utc: int,
    min_elapsed_seconds: float = DEFAULT_MIN_ELAPSED_SECONDS,
    gap_seconds: float = BURST_GAP_SECONDS,
) -> ParticipantResult:
    """Apply the full ruleset to one participant."""
    tag_reads = [reads_by_epc.get(epc, []) for epc in participant.epcs]

    start_utc = _earliest(
        start_crossing(reads, gun_time_reader_utc, gap_seconds) for reads in tag_reads
    )

    finish_utc = None
    if start_utc is not None:
        finish_utc = _earliest(
            finish_crossing(reads, start_utc, min_elapsed_seconds, gap_seconds)
            for reads in tag_reads
        )

    source = SOURCE_CHIP
    if start_utc is not None and finish_utc is not None:
        elapsed = round((finish_utc - start_utc) / MICROS, 2)
        status = STATUS_FINISHED
    elif start_utc is not None:
        elapsed = None
        status = STATUS_DNF
    else:
        # No start read. A real finish, judged from the gun, is timed from the
        # gun. Finish reads too soon after the gun are someone near the arch,
        # and go to a human.
        finish_utc = _earliest(
            finish_crossing(reads, gun_time_reader_utc, min_elapsed_seconds, gap_seconds)
            for reads in tag_reads
        )
        if finish_utc is not None:
            elapsed = round((finish_utc - gun_time_reader_utc) / MICROS, 2)
            status = STATUS_FINISHED
            source = SOURCE_GUN
        else:
            elapsed = None
            finish_utc = _earliest(
                (crossings_at_line(reads, FINISH_ANTENNAS, gun_time_reader_utc, gap_seconds) or [None])[0]
                for reads in tag_reads
            )
            status = STATUS_REVIEW if finish_utc is not None else STATUS_NOT_STARTED

    return ParticipantResult(
        participant_id=participant.participant_id,
        bib=participant.bib,
        start_utc=start_utc,
        finish_utc=finish_utc,
        elapsed_seconds=elapsed,
        status=status,
        source=source,
        gun_seconds=(
            round((finish_utc - gun_time_reader_utc) / MICROS, 2)
            if status == STATUS_FINISHED else None
        ),
    )


def compute_results(
    reads: Iterable[TagRead],
    participants: Sequence[Participant],
    gun_time_reader_utc: int | None,
    min_elapsed_seconds: float = DEFAULT_MIN_ELAPSED_SECONDS,
    gap_seconds: float = BURST_GAP_SECONDS,
) -> list[ParticipantResult]:
    """Results for the whole field, in bib order.

    A race with no gun time has not started, so nobody has a result yet.
    """
    if gun_time_reader_utc is None:
        return [
            ParticipantResult(p.participant_id, p.bib, None, None, None, STATUS_NOT_STARTED)
            for p in participants
        ]

    reads_by_epc = index_reads_by_epc(reads)
    return [
        compute_result(p, reads_by_epc, gun_time_reader_utc, min_elapsed_seconds, gap_seconds)
        for p in participants
    ]


def apply_adjustments(
    results: Iterable[ParticipantResult], adjustments: dict[int, Adjustment],
    gun_time_reader_utc: int | None = None,
) -> list[ParticipantResult]:
    """The computed results with the operator's corrections laid over them.

    A corrected finish carries its crossing time, so its gun time is that
    crossing minus the gun.
    """
    adjusted = []
    for result in results:
        adjustment = adjustments.get(result.participant_id)
        if adjustment is None:
            adjusted.append(result)
        elif adjustment.kind == Adjustment.REMOVED:
            adjusted.append(ParticipantResult(
                result.participant_id, result.bib, result.start_utc, None, None,
                STATUS_REMOVED, SOURCE_MANUAL,
            ))
        else:
            gun_seconds = adjustment.elapsed_seconds
            if gun_time_reader_utc is not None and adjustment.finish_utc is not None:
                gun_seconds = round((adjustment.finish_utc - gun_time_reader_utc) / MICROS, 2)
            adjusted.append(ParticipantResult(
                result.participant_id, result.bib, result.start_utc,
                adjustment.finish_utc, adjustment.elapsed_seconds,
                STATUS_FINISHED, SOURCE_MANUAL, gun_seconds,
            ))
    return adjusted


def category(age: int | str | None, gender: str | None) -> str | None:
    """Adult male, adult female or junior. None if the gender is unknown."""
    try:
        if age not in (None, "") and int(age) <= JUNIOR_MAX_AGE:
            return CATEGORY_JUNIOR
    except (TypeError, ValueError):
        pass
    first = (gender or "").strip()[:1].upper()
    if first == "M":
        return CATEGORY_ADULT_MALE
    if first == "F":
        return CATEGORY_ADULT_FEMALE
    return None


def order_finishers(results: Iterable[ParticipantResult], by: str = BY_GUN) -> list[ParticipantResult]:
    """Finishers fastest first, by gun time (overall) or chip time (age
    group). Ties broken by who crossed the line first."""
    finishers = [r for r in results if r.status == STATUS_FINISHED]
    if by == BY_CHIP:
        finishers.sort(key=lambda r: (r.elapsed_seconds, r.finish_utc or 0))
    else:
        finishers.sort(key=lambda r: (
            r.gun_seconds if r.gun_seconds is not None else r.elapsed_seconds,
            r.finish_utc or 0,
        ))
    return finishers


def places(results: Iterable[ParticipantResult], by: str = BY_GUN) -> dict[int, int]:
    """Map participant id to place. Only finishers get a place. Overall
    places are by gun time."""
    return {
        result.participant_id: index + 1
        for index, result in enumerate(order_finishers(results, by))
    }


def summarize(results: Iterable[ParticipantResult]) -> dict[str, int]:
    """Counts for the race console header."""
    results = list(results)
    return {
        "registered": len(results),
        "started": sum(1 for r in results if r.start_utc is not None),
        "finished": sum(1 for r in results if r.status == STATUS_FINISHED),
        # Started and not yet across the line. A gun time finisher has no
        # start read, so started minus finished would undercount.
        "on_course": sum(1 for r in results if r.status == STATUS_DNF),
        "gun_time": sum(1 for r in results
                        if r.status == STATUS_FINISHED and r.source == SOURCE_GUN),
        "review": sum(1 for r in results if r.status == STATUS_REVIEW),
        "not_started": sum(1 for r in results if r.status == STATUS_NOT_STARTED),
    }


def parse_elapsed(text: str) -> float:
    """An operator's typed time as seconds.

    Takes m:ss, m:ss.hh, h:mm:ss and h:mm:ss.hh. Three groups whose first is
    10 or more and whose last is two digits are read as m:ss:hh, because
    "46:31:19" at a 5K means 46 minutes 31.19 seconds, not 46 hours.
    """
    parts = [part.strip() for part in text.strip().split(":")]
    if not 2 <= len(parts) <= 3 or not all(parts):
        raise ValueError(f"not a time: {text!r}")
    try:
        if len(parts) == 3 and "." not in parts[2] and len(parts[2]) == 2 and int(parts[0]) >= 10:
            minutes, seconds, hundredths = (int(part) for part in parts)
            total = minutes * 60 + seconds + hundredths / 100
            fields = [seconds]
        else:
            numbers = [float(part) for part in parts]
            total = 0.0
            for number in numbers:
                total = total * 60 + number
            fields = numbers[1:]
    except ValueError:
        raise ValueError(f"not a time: {text!r}") from None
    if any(field < 0 or field >= 60 for field in fields) or total <= 0:
        raise ValueError(f"not a time: {text!r}")
    return round(total, 2)


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
