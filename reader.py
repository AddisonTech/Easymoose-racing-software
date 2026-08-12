"""Tag reader interface and the live LLRP implementation.

Everything downstream of this module consumes TagRead events and does not care
whether they came off real hardware or the simulator.

Timestamps are integer microseconds since the Unix epoch, which is exactly what
LLRP FirstSeenTimestampUTC carries. Keeping them as integers means no float
rounding creeps into a race result.
"""

from __future__ import annotations

import logging
import queue
import threading
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

    Accepts raw bytes from the reader or text typed into a participant CSV.
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex().upper()
    return str(value).strip().replace(" ", "").replace("-", "").upper()


class Reader:
    """A source of tag reads.

    reads() yields TagRead events until stop() is called. Implementations are
    expected to be usable from a single consumer thread.
    """

    def reads(self) -> Iterator[TagRead]:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


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
    tx_power_dbm, session and search mode against real mats and real tags.
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

    def _on_tag_report(self, _client, tags) -> None:
        for tag in tags:
            read = tag_report_to_read(tag)
            if read is None:
                self._dropped += 1
                continue
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
        self._client.connect()
        logger.info("reader connected")

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
