"""Bib pairing: tie each bib's tag EPC to its bib number using the reader.

Pair mode walks through the bibs in order. Hold one bib over the antenna, the
tool reads it, and when exactly one tag was seen it writes bib,epc1 to
pairs.csv and moves on. Verify mode reads pairs.csv back and shows the bib for
every tag that passes the antenna.

    python pair.py                       pair bibs 110 to 310 into pairs.csv
    python pair.py --start 250           pair from bib 250
    python pair.py --verify              check tags against pairs.csv
    python pair.py --simulate            no reader, generated tags

This only ever inventories tags. It never writes or locks an EPC, and the only
RF setting it chooses is transmit power, for its own session.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import queue
import random
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from reader import LLRP_DEFAULT_PORT, LLRPReader, Reader, TagRead, normalize_epc

DEFAULT_HOST = "192.168.10.20"
# The R420's lowest setting. At 12 dBm a bib held over the antenna read near
# -30 dBm and one a few feet away near -55 dBm, both continuously, so power
# alone cannot keep the neighbours out. MIN_RSSI does that.
DEFAULT_POWER_DBM = 10.0

# Pair mode ignores reads weaker than this, so a bib lying a few feet away is
# neither paired nor counted as a second tag in the field. Verify mode sees
# every read.
DEFAULT_MIN_RSSI = -45.0

# The bibs printed for this event.
FIRST_BIB = 110
LAST_BIB = 310

# How long to keep collecting after the first tag shows up. Long enough for a
# second tag in the field to be read too, short enough not to slow anyone down.
DEFAULT_WINDOW_SECONDS = 0.75

# How long the field has to be quiet before the next bib can be captured. This
# is what stops one bib counting twice while it is still over the antenna.
DEFAULT_CLEAR_SECONDS = 1.5

PAIRS_HEADER = ["bib", "epc1"]


# ----------------------------------------------------------------------
# pairs.csv
# ----------------------------------------------------------------------


class PairsFileError(ValueError):
    """pairs.csv is unreadable or contradicts itself."""


def load_pairs(path: Path) -> dict[int, str]:
    """Read pairs.csv into {bib: epc}. A missing file is an empty one."""
    path = Path(path)
    if not path.exists():
        return {}
    pairs: dict[int, str] = {}
    owners: dict[str, int] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for line_number, row in enumerate(csv.reader(handle), start=1):
            if not row or not row[0].strip():
                continue
            if line_number == 1 and row[0].strip().lower() == "bib":
                continue
            try:
                bib = int(row[0].strip())
            except ValueError:
                raise PairsFileError(f"{path} line {line_number}: bib {row[0]!r} is not a number")
            epc = normalize_epc(row[1] if len(row) > 1 else "")
            if not epc:
                raise PairsFileError(f"{path} line {line_number}: bib {bib} has no EPC")
            if bib in pairs:
                raise PairsFileError(f"{path} line {line_number}: bib {bib} appears twice")
            if epc in owners:
                raise PairsFileError(
                    f"{path} line {line_number}: EPC {epc} is on bib {owners[epc]} and bib {bib}"
                )
            pairs[bib] = epc
            owners[epc] = bib
    return pairs


class PairStore:
    """pairs.csv, kept in step with an in-memory copy.

    Every accepted pair is appended and synced before the screen moves on, so
    pulling the power mid session loses nothing that was shown as accepted.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.pairs = load_pairs(self.path)

    def bib_for(self, epc: str) -> int | None:
        for bib, paired in self.pairs.items():
            if paired == epc:
                return bib
        return None

    def add(self, bib: int, epc: str) -> None:
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            if new_file:
                writer.writerow(PAIRS_HEADER)
            writer.writerow([bib, epc])
            handle.flush()
            os.fsync(handle.fileno())
        self.pairs[bib] = epc

    def remove(self, bib: int) -> None:
        if bib not in self.pairs:
            return
        del self.pairs[bib]
        temp = self.path.with_name(self.path.name + ".tmp")
        with temp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(PAIRS_HEADER)
            for kept_bib, epc in self.pairs.items():
                writer.writerow([kept_bib, epc])
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)


# ----------------------------------------------------------------------
# the pairing and verify rules, driven by explicit times so tests can run them
# ----------------------------------------------------------------------


@dataclass
class Event:
    kind: str  # accepted, multiple, duplicate, clear
    bib: int | None = None
    epc: str = ""
    epcs: list[str] = field(default_factory=list)
    other_bib: int | None = None


class Pairer:
    """Decides what each burst of reads means for the bib on screen.

    Armed means the field has been quiet long enough to capture. The first read
    while armed opens a window; when the window closes the set of EPCs seen in
    it is judged. Whatever the verdict, the field then has to go quiet again
    before anything else is captured.
    """

    def __init__(self, store: PairStore, start: int, end: int,
                 window: float = DEFAULT_WINDOW_SECONDS,
                 clear_gap: float = DEFAULT_CLEAR_SECONDS,
                 min_rssi: float | None = DEFAULT_MIN_RSSI):
        self.store = store
        self.start = start
        self.end = end
        self.window = window
        self.clear_gap = clear_gap
        self.min_rssi = min_rssi
        self.current = self._next_missing(start)
        self.armed = True
        self._window_start: float | None = None
        self._window_epcs: list[str] = []
        self._last_read: float | None = None

    def _next_missing(self, bib: int) -> int | None:
        while bib <= self.end:
            if bib not in self.store.pairs:
                return bib
            bib += 1
        return None

    @property
    def done(self) -> bool:
        return self.current is None

    @property
    def reading(self) -> bool:
        return self._window_start is not None

    @property
    def paired_in_range(self) -> int:
        return sum(1 for bib in self.store.pairs if self.start <= bib <= self.end)

    def on_read(self, epc: str, now: float, rssi: float = 0.0) -> None:
        # Too weak to be the bib over the antenna. Ignored completely, so a
        # neighbour lying nearby cannot hold the field open either.
        if self.min_rssi is not None and rssi < self.min_rssi:
            return
        self._last_read = now
        if not self.armed or self.done:
            return
        if self._window_start is None:
            self._window_start = now
        if epc not in self._window_epcs:
            self._window_epcs.append(epc)

    def poll(self, now: float) -> Event | None:
        if self._window_start is not None and now - self._window_start >= self.window:
            return self._judge()
        if not self.armed and (self._last_read is None or now - self._last_read >= self.clear_gap):
            self.armed = True
            return Event("clear", bib=self.current)
        return None

    def _judge(self) -> Event:
        epcs = self._window_epcs
        self._window_start = None
        self._window_epcs = []
        self.armed = False
        bib = self.current
        if len(epcs) > 1:
            return Event("multiple", bib=bib, epcs=epcs)
        epc = epcs[0]
        owner = self.store.bib_for(epc)
        if owner is not None:
            return Event("duplicate", bib=bib, epc=epc, other_bib=owner)
        self.store.add(bib, epc)
        self.current = self._next_missing(bib + 1)
        return Event("accepted", bib=bib, epc=epc)

    def _abandon_window(self) -> None:
        if self._window_start is not None:
            self._window_start = None
            self._window_epcs = []
            self.armed = False

    def skip(self) -> None:
        if self.done:
            return
        self._abandon_window()
        self.current = self._next_missing(self.current + 1)

    def back(self) -> int | None:
        """Go back one bib and clear its pair. Returns the bib now current."""
        target = (self.end + 1 if self.done else self.current) - 1
        if target < self.start:
            return self.current
        self._abandon_window()
        self.store.remove(target)
        self.current = target
        return target


class Verifier:
    """Names each tag that passes. A tag is announced again only after it has
    been out of the field for clear_gap seconds."""

    def __init__(self, pairs: dict[int, str], clear_gap: float = DEFAULT_CLEAR_SECONDS):
        self.pairs = dict(pairs)
        self.bibs_by_epc = {epc: bib for bib, epc in self.pairs.items()}
        self.clear_gap = clear_gap
        self.confirmed: set[int] = set()
        self.unknown: set[str] = set()
        self._last_seen: dict[str, float] = {}

    def on_read(self, epc: str, now: float) -> Event | None:
        previous = self._last_seen.get(epc)
        self._last_seen[epc] = now
        if previous is not None and now - previous < self.clear_gap:
            return None
        bib = self.bibs_by_epc.get(epc)
        if bib is None:
            self.unknown.add(epc)
            return Event("unknown", epc=epc)
        self.confirmed.add(bib)
        return Event("confirmed", bib=bib, epc=epc)

    @property
    def total(self) -> int:
        return len(self.pairs)

    def never_seen(self) -> list[int]:
        return sorted(bib for bib in self.pairs if bib not in self.confirmed)


# ----------------------------------------------------------------------
# a simulated bench for running with no reader
# ----------------------------------------------------------------------


def simulated_epc(number: int) -> str:
    return f"E28068940000{number:012X}"


class BenchSimulator(Reader):
    """Someone at a table presenting tags to one antenna, one at a time.

    Mostly a single tag, but now and then two held together, or the last tag
    held up a second time, so the rejections can be seen working.
    """

    def __init__(self, epcs: list[str], antenna: int = 1, seed: int = 1,
                 mistakes: bool = True):
        self.epcs = list(epcs)
        self.antenna = antenna
        self.rng = random.Random(seed)
        self.mistakes = mistakes
        self._stopped = threading.Event()

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stopped.is_set() and time.monotonic() < deadline:
            time.sleep(min(0.05, deadline - time.monotonic()))

    def reads(self) -> Iterator[TagRead]:
        index = 0
        previous = None
        while not self._stopped.is_set() and self.epcs:
            self._sleep(self.rng.uniform(1.8, 3.0))
            epc = self.epcs[index % len(self.epcs)]
            present = [epc]
            roll = self.rng.random()
            if self.mistakes and roll < 0.08:
                present.append(self.epcs[(index + 1) % len(self.epcs)])
            elif self.mistakes and roll < 0.12 and previous:
                present = [previous]
            else:
                previous = epc
                index += 1
            hold = time.monotonic() + self.rng.uniform(0.8, 1.4)
            while not self._stopped.is_set() and time.monotonic() < hold:
                for tag in present:
                    yield TagRead(
                        epc=tag,
                        antenna_port=self.antenna,
                        first_seen_utc=int(time.time() * 1_000_000),
                        rssi=round(self.rng.uniform(-36.0, -28.0), 1),
                    )
                self._sleep(0.05)

    def stop(self) -> None:
        self._stopped.set()


# ----------------------------------------------------------------------
# terminal
# ----------------------------------------------------------------------

FONT = {
    "0": ("###", "# #", "# #", "# #", "###"),
    "1": (" # ", "## ", " # ", " # ", "###"),
    "2": ("###", "  #", "###", "#  ", "###"),
    "3": ("###", "  #", "###", "  #", "###"),
    "4": ("# #", "# #", "###", "  #", "  #"),
    "5": ("###", "#  ", "###", "  #", "###"),
    "6": ("###", "#  ", "###", "# #", "###"),
    "7": ("###", "  #", "  #", "  #", "  #"),
    "8": ("###", "# #", "###", "# #", "###"),
    "9": ("###", "# #", "###", "  #", "###"),
    "B": ("## ", "# #", "## ", "# #", "## "),
    "I": ("###", " # ", " # ", " # ", "###"),
    " ": ("   ", "   ", "   ", "   ", "   "),
}


def big_text(text: str, width: int) -> list[str]:
    """Render text in the block font, as large as fits in width columns."""
    glyphs = [FONT.get(ch, FONT[" "]) for ch in text.upper()]
    base_width = len(glyphs) * 4  # three columns per glyph plus a gap
    scale = max(1, min(3, width // (base_width * 2)))
    lines = []
    for row in range(5):
        line = "  ".join(
            "".join(("#" if cell == "#" else " ") * 2 * scale for cell in glyph[row])
            for glyph in glyphs
        )
        lines.extend([line.rstrip()] * scale)
    return lines


RED = "\033[1;37;41m"
GREEN = "\033[1;30;42m"
YELLOW = "\033[1;30;43m"
RESET = "\033[0m"


def enable_ansi() -> None:
    """Windows consoles need VT processing switched on for colours and clear."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def draw(lines: list[str]) -> None:
    sys.stdout.write("\033[2J\033[H" + "\n".join(lines) + "\n")
    sys.stdout.flush()


def bell() -> None:
    sys.stdout.write("\a")
    sys.stdout.flush()


class Keys:
    """Single keypresses without waiting for Enter, on Windows and on the Pi."""

    def __enter__(self):
        self._saved = None
        if os.name != "nt" and sys.stdin.isatty():
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    def get(self) -> str | None:
        if os.name == "nt":
            import msvcrt

            if not msvcrt.kbhit():
                return None
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):  # arrow and function keys come in pairs
                msvcrt.getwch()
                return None
        else:
            import select

            if not sys.stdin.isatty() or not select.select([sys.stdin], [], [], 0)[0]:
                return None
            ch = os.read(sys.stdin.fileno(), 1).decode(errors="ignore")
        if ch in ("\r", "\n"):
            return "enter"
        return ch.lower()


def start_pump(reader: Reader, antenna: int) -> "queue.Queue[TagRead | BaseException]":
    """Move reads off the reader onto a queue the screen loop can poll."""
    reads: queue.Queue = queue.Queue()

    def pump():
        try:
            for read in reader.reads():
                if read.antenna_port == antenna:
                    reads.put(read)
        except BaseException as exc:  # surface reader failures on screen
            reads.put(exc)

    threading.Thread(target=pump, name="pair-reader", daemon=True).start()
    return reads


def drain(reads: queue.Queue, timeout: float) -> list[TagRead]:
    got = []
    try:
        item = reads.get(timeout=timeout)
        while True:
            if isinstance(item, BaseException):
                raise item
            got.append(item)
            item = reads.get_nowait()
    except queue.Empty:
        pass
    return got


# ----------------------------------------------------------------------
# the two modes
# ----------------------------------------------------------------------


def run_pair(args, reader: Reader) -> int:
    store = PairStore(args.out)
    pairer = Pairer(store, args.start, args.end, args.window, args.clear_gap, args.min_rssi)
    total = args.end - args.start + 1
    if pairer.done:
        print(f"Every bib from {args.start} to {args.end} is already in {args.out}.")
        return 0
    resumed = pairer.paired_in_range
    message, colour = "", ""
    if resumed:
        message = f"Resumed: {resumed} already paired in {args.out}, starting at bib {pairer.current}"

    reads = start_pump(reader, args.antenna)
    redraw = True
    with Keys() as keys:
        while not pairer.done:
            key = keys.get()
            if key == "q":
                break
            if key == "enter":
                skipped = pairer.current
                pairer.skip()
                message, colour = f"Skipped bib {skipped}", YELLOW
                redraw = True
            elif key == "b":
                bib = pairer.back()
                message, colour = f"Back to bib {bib}, cleared", YELLOW
                redraw = True

            for read in drain(reads, 0.05):
                was_reading = pairer.reading
                pairer.on_read(read.epc, time.monotonic(), read.rssi)
                redraw = redraw or pairer.reading != was_reading

            event = pairer.poll(time.monotonic())
            if event is not None:
                redraw = True
                if event.kind == "accepted":
                    bell()
                    message, colour = f"BIB {event.bib} OK   {event.epc}", GREEN
                elif event.kind == "multiple":
                    message, colour = "MORE THAN ONE TAG, rescan", RED
                elif event.kind == "duplicate":
                    message, colour = f"ALREADY BIB {event.other_bib}", RED

            if redraw and not pairer.done:
                redraw = False
                width = shutil.get_terminal_size((80, 24)).columns
                if pairer.reading:
                    state = "reading..."
                elif pairer.armed:
                    state = "waiting"
                else:
                    state = "take the tag away"
                lines = [""]
                lines += big_text(f"BIB {pairer.current}", width)
                lines += ["", f"  Bib {pairer.current} - {state}", ""]
                if message:
                    lines.append(f"  {colour} {message} {RESET}" if colour else f"  {message}")
                lines += [
                    "",
                    f"  Paired {pairer.paired_in_range} of {total}   ->  {args.out}",
                    "  Enter skip   b back one and clear   q quit",
                ]
                draw(lines)

    reader.stop()
    missing = [bib for bib in range(args.start, args.end + 1) if bib not in store.pairs]
    print()
    print(f"Paired {total - len(missing)} of {total} bibs in {args.out}.")
    if missing:
        print(f"Not paired yet: {format_bibs(missing)}")
    return 0


def run_verify(args, reader: Reader) -> int:
    pairs = load_pairs(args.out)
    if not pairs:
        print(f"No pairs in {args.out}; nothing to verify against.")
        return 1
    verifier = Verifier(pairs, args.clear_gap)
    reads = start_pump(reader, args.antenna)
    last: Event | None = None
    redraw = True
    with Keys() as keys:
        while True:
            if keys.get() == "q":
                break
            for read in drain(reads, 0.05):
                event = verifier.on_read(read.epc, time.monotonic())
                if event is not None:
                    last = event
                    redraw = True
                    if event.kind == "confirmed":
                        bell()
            if redraw:
                redraw = False
                width = shutil.get_terminal_size((80, 24)).columns
                lines = [""]
                if last is None:
                    lines += ["  Pass a tag over the antenna.", ""]
                elif last.kind == "confirmed":
                    lines += big_text(f"BIB {last.bib}", width)
                    lines += ["", f"  {GREEN} BIB {last.bib} {RESET}  {last.epc}", ""]
                else:
                    lines += ["", f"  {RED} UNKNOWN TAG {RESET}  {last.epc}", ""]
                lines += [
                    f"  Confirmed {len(verifier.confirmed)} of {verifier.total}"
                    + (f"   unknown tags seen: {len(verifier.unknown)}" if verifier.unknown else ""),
                    "  q quit",
                ]
                draw(lines)

    reader.stop()
    print()
    print(f"Confirmed {len(verifier.confirmed)} of {verifier.total} bibs in {args.out}.")
    never = verifier.never_seen()
    print(f"Never seen: {format_bibs(never)}" if never else "Every paired bib was seen.")
    if verifier.unknown:
        print("Unknown tags:")
        for epc in sorted(verifier.unknown):
            print(f"  {epc}")
    return 0


def format_bibs(bibs: list[int]) -> str:
    """1,2,3,7,9,10 -> 1-3, 7, 9-10"""
    runs = []
    for bib in sorted(bibs):
        if runs and bib == runs[-1][1] + 1:
            runs[-1][1] = bib
        else:
            runs.append([bib, bib])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def build_reader(args) -> Reader:
    if args.simulate:
        if args.verify:
            epcs = list(load_pairs(args.out).values())
            rng = random.Random(7)
            rng.shuffle(epcs)
            epcs.insert(min(3, len(epcs)), simulated_epc(0xFFFF))  # one stranger
            return BenchSimulator(epcs, antenna=args.antenna, mistakes=False)
        epcs = [simulated_epc(bib) for bib in range(args.start, args.end + 1)]
        return BenchSimulator(epcs, antenna=args.antenna)
    reader = LLRPReader(
        host=args.host,
        port=args.port,
        antennas=[args.antenna],
        tx_power_dbm=float(args.power),  # sllurp rejects an int here
    )
    reader.connect()
    return reader


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Pair bib numbers to tag EPCs, or verify the pairs.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="reader IP")
    parser.add_argument("--port", type=int, default=LLRP_DEFAULT_PORT, help="LLRP port")
    parser.add_argument("--antenna", type=int, default=1, help="reader antenna port")
    parser.add_argument("--power", type=float, default=DEFAULT_POWER_DBM,
                        help="transmit power in dBm for this session")
    parser.add_argument("--start", type=int, default=FIRST_BIB, help="first bib")
    parser.add_argument("--end", type=int, default=LAST_BIB, help="last bib")
    parser.add_argument("--out", type=Path, default=Path("pairs.csv"), help="pairs file")
    parser.add_argument("--verify", action="store_true", help="check tags against the pairs file")
    parser.add_argument("--simulate", action="store_true", help="no reader, generated tags")
    parser.add_argument("--window", type=float, default=DEFAULT_WINDOW_SECONDS,
                        help="seconds to collect reads after the first tag appears")
    parser.add_argument("--clear-gap", type=float, default=DEFAULT_CLEAR_SECONDS,
                        help="seconds of quiet before the next tag counts")
    parser.add_argument("--min-rssi", type=float, default=DEFAULT_MIN_RSSI,
                        help="pair mode ignores reads weaker than this, in dBm")
    args = parser.parse_args(argv)
    if args.start > args.end:
        parser.error("--start is after --end")

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    enable_ansi()
    try:
        load_pairs(args.out)
    except PairsFileError as exc:
        print(f"Can't use {args.out}: {exc}")
        return 1
    try:
        reader = build_reader(args)
    except Exception as exc:
        print(f"Could not connect to the reader at {args.host}:{args.port}: {exc}")
        return 1
    try:
        return run_verify(args, reader) if args.verify else run_pair(args, reader)
    except KeyboardInterrupt:
        reader.stop()
        print("\nStopped. Everything accepted so far is in", args.out)
        return 0
    except Exception as exc:
        reader.stop()
        print(f"\nReader error: {exc}. Everything accepted so far is in {args.out}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
