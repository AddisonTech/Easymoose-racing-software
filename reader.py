"""Tag reader interface and the live LLRP implementation.

Everything downstream of this module consumes TagRead events and does not care
whether they came off real hardware or the simulator.

Timestamps are integer microseconds since the Unix epoch, which is exactly what
LLRP FirstSeenTimestampUTC carries. Keeping them as integers means no float
rounding creeps into a race result.

Read times are reported in the reader's clock domain and are never adjusted.
The gun time is taken from the Pi's clock, and those two clocks have no
relationship at all, so a reader exposes clock_offset_micros: the difference
between them. The gun time is moved into the reader's domain once, when it is
recorded. Individual reads are left exactly as the reader stamped them.
"""

from __future__ import annotations

import logging
import queue
import string
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Iterator

logger = logging.getLogger(__name__)

# Physical wiring. Ports 1 and 2 are the start line, 3 and 4 are the finish.
# Two antennas cover one line, so a single crossing produces reads on both.
START_ANTENNAS = frozenset({1, 2})
FINISH_ANTENNAS = frozenset({3, 4})
ALL_ANTENNAS = (1, 2, 3, 4)

LLRP_DEFAULT_PORT = 5084


@dataclass(frozen=True)
class TagRead:
    """One tag observation reported by a reader."""

    epc: str
    antenna_port: int
    first_seen_utc: int  # microseconds since the Unix epoch
    rssi: float


def normalize_epc(value) -> str:
    """Put an EPC into the one form used everywhere: uppercase hex, no spaces.

    Accepts bytes from the reader or text typed into a participant CSV.

    sllurp hands the EPC over already hexlified, as ASCII bytes such as
    b"e28011b0a5050076e8fe9942". Those are decoded, not hex encoded a second
    time, or every live read would carry a 48 character EPC that matches
    nothing in the participant list. Bytes that are not ASCII hex are taken
    to be the raw EPC.
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            return raw.hex().upper()
        if text and all(ch in string.hexdigits for ch in text):
            return text.upper()
        return raw.hex().upper()
    return str(value).strip().replace(" ", "").replace("-", "").upper()


def pi_now_micros() -> int:
    """The Pi's own clock, in the same units the reader reports."""
    return int(time.time() * 1_000_000)


class Reader:
    """A source of tag reads.

    reads() yields TagRead events until stop() is called. Implementations are
    expected to be usable from a single consumer thread.
    """

    def reads(self) -> Iterator[TagRead]:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    @property
    def clock_offset_micros(self) -> int | None:
        """Reader clock minus Pi clock, in microseconds.

        Positive means the reader is ahead of the Pi. None means it has not
        been measured yet, and a gun time must not be recorded until it has:
        without it there is nothing to convert the gun time with.
        """
        return None


def _scalar(value):
    """sllurp hands back some report fields wrapped in lists. Unwrap them."""
    while isinstance(value, (list, tuple)):
        if not value:
            return None
        value = value[0]
    return value


def tag_report_to_read(tag: dict) -> TagRead | None:
    """Convert one sllurp tag report dict into a TagRead.

    Returns None when the report is missing anything we require. A report with
    no FirstSeenTimestampUTC is dropped rather than backfilled with the Pi's
    clock: an arrival timestamp has already been through the network stack and
    is not a measurement of when the runner crossed the line.
    """
    epc = _scalar(tag.get("EPC"))
    if epc is None:
        epc = _scalar(tag.get("EPC-96"))
    epc = normalize_epc(epc)
    if not epc:
        return None

    first_seen = _scalar(tag.get("FirstSeenTimestampUTC"))
    if first_seen is None:
        logger.warning("dropping read for %s: no FirstSeenTimestampUTC in report", epc)
        return None

    antenna = _scalar(tag.get("AntennaID"))
    if antenna is None:
        logger.warning("dropping read for %s: no AntennaID in report", epc)
        return None

    rssi = _scalar(tag.get("PeakRSSI"))

    return TagRead(
        epc=epc,
        antenna_port=int(antenna),
        first_seen_utc=int(first_seen),
        rssi=float(rssi) if rssi is not None else 0.0,
    )


class LLRPReader(Reader):
    """Live reader for an Impinj Speedway R420 over LLRP.

    The sllurp client is callback driven and runs its own socket thread, so
    reads land in a queue that reads() drains. That keeps the network thread
    free to keep pulling reports while the consumer writes to SQLite.

    The RF settings below are starting points. They have not been validated
    against hardware, because the hardware does not exist yet; expect to tune
    tx_power_dbm, session and search mode against the real antennas and tags.
    """

    def __init__(
        self,
        host: str,
        port: int = LLRP_DEFAULT_PORT,
        antennas: Iterable[int] = ALL_ANTENNAS,
        tx_power_dbm: float = 30.0,
        session: int = 1,
        impinj_search_mode: int = 2,
        connect_timeout: float = 5.0,
    ):
        self.host = host
        self.port = port
        self.antennas = list(antennas)
        self.tx_power_dbm = tx_power_dbm
        self.session = session
        self.impinj_search_mode = impinj_search_mode
        self.connect_timeout = connect_timeout

        self._queue: queue.Queue[TagRead] = queue.Queue()
        self._stopped = threading.Event()
        self._client = None
        self._dropped = 0
        self._clock_offset_micros: int | None = None
        self._clock_offset_source: str | None = None

    def _build_config(self):
        from sllurp.llrp import LLRPReaderConfig

        return LLRPReaderConfig(
            {
                "antennas": self.antennas,
                "tx_power_dbm": self.tx_power_dbm,
                "session": self.session,
                "start_inventory": True,
                "reset_on_connect": True,
                # Report every tag sighting as it happens. Buffering reports on
                # the reader would be cheaper on the network and useless here:
                # we want each crossing on screen while the runner is still in
                # the chute.
                "report_every_n_tags": 1,
                "report_timeout_ms": 0,
                "tag_content_selector": {
                    "EnableROSpecID": False,
                    "EnableSpecIndex": False,
                    "EnableInventoryParameterSpecID": False,
                    "EnableAntennaID": True,
                    "EnableChannelIndex": False,
                    "EnablePeakRSSI": True,
                    "EnableFirstSeenTimestamp": True,
                    "EnableLastSeenTimestamp": False,
                    "EnableTagSeenCount": True,
                    "EnableAccessSpecID": False,
                },
                # Dual Target keeps tags reporting repeatedly as they pass
                # through the field instead of going quiet after one read.
                "impinj_search_mode": self.impinj_search_mode,
            }
        )

    # ------------------------------------------------------------------
    # clock offset
    # ------------------------------------------------------------------

    @property
    def clock_offset_micros(self) -> int | None:
        """Reader clock minus Pi clock. None until it has been measured."""
        return self._clock_offset_micros

    @property
    def clock_offset_source(self) -> str | None:
        """Which measurement produced the offset: 'event' or 'first_report'."""
        return self._clock_offset_source

    def _on_event_notification(self, _client, event_data) -> None:
        """Preferred measurement: the UTCTimestamp on a reader event.

        The reader sends a ReaderEventNotification when the connection is
        established, and sllurp hands the decoded ReaderEventNotificationData
        straight over. UTCTimestamp is optional in LLRP, and a reader may send
        Uptime instead, so this is allowed to come up empty.

        The first measurement is kept rather than being replaced by later
        events. The gun time is converted with this number and a value that
        drifts under it would be worse than one that is merely slightly stale.
        """
        if self._clock_offset_micros is not None and self._clock_offset_source == "event":
            return
        stamp = (event_data or {}).get("UTCTimestamp")
        micros = stamp.get("Microseconds") if isinstance(stamp, dict) else None
        if micros is None:
            return
        self._clock_offset_micros = int(micros) - pi_now_micros()
        self._clock_offset_source = "event"
        logger.info(
            "reader clock offset %+.3f s, measured from the connection event",
            self._clock_offset_micros / 1_000_000,
        )

    def _measure_offset_from_report(self, read: TagRead) -> None:
        """Fallback: the first read's own timestamp against the Pi's clock.

        This number carries the transport and processing latency between the
        reader stamping the read and this process handling it, on the order of
        tens of milliseconds. That is fine for what it is used for, which is
        shifting the gun time and comparing it against a threshold measured in
        seconds. It is emphatically not fine for adjusting individual read
        timestamps, and nothing does that: read times stay exactly as the
        reader reported them.
        """
        self._clock_offset_micros = read.first_seen_utc - pi_now_micros()
        self._clock_offset_source = "first_report"
        logger.info(
            "reader clock offset %+.3f s, measured from the first tag report; "
            "this one carries transport latency",
            self._clock_offset_micros / 1_000_000,
        )

    def _on_tag_report(self, _client, tags) -> None:
        for tag in tags:
            read = tag_report_to_read(tag)
            if read is None:
                self._dropped += 1
                continue
            if self._clock_offset_micros is None:
                self._measure_offset_from_report(read)
            self._queue.put(read)

    def connect(self) -> None:
        from sllurp.llrp import LLRPReaderClient

        logger.info(
            "connecting to R420 at %s:%s, antennas %s", self.host, self.port, self.antennas
        )
        self._client = LLRPReaderClient(
            self.host, self.port, self._build_config(), timeout=self.connect_timeout
        )
        self._client.add_tag_report_callback(self._on_tag_report)
        # Registered before connect so the notification the reader sends on
        # connection is not missed.
        self._client.add_event_callback(self._on_event_notification)
        self._client.connect()
        logger.info("reader connected")
        if self._clock_offset_micros is None:
            logger.warning(
                "no UTCTimestamp in the connection event; the clock offset will "
                "be measured from the first tag report instead"
            )

    def reads(self) -> Iterator[TagRead]:
        if self._client is None:
            self.connect()
        while not self._stopped.is_set():
            try:
                # The timeout is what lets stop() actually end this loop.
                yield self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

    @property
    def dropped_reports(self) -> int:
        """Reports discarded for missing EPC, antenna or timestamp."""
        return self._dropped

    def stop(self) -> None:
        self._stopped.set()
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                logger.exception("error while disconnecting reader")
            self._client = None
