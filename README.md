# Easymoose race timing

Chip timing for road races, built to run on one Raspberry Pi with one RFID
reader and no internet connection. Every raw tag read is written to disk
before anything interprets it, so results can always be rebuilt from the read
log, and a whole event lives in one folder you can copy off the Pi.

There is a simulator, so the entire system can be developed, demonstrated and
tested with no hardware attached.

---

## Hardware

    Raspberry Pi 5 (4GB)                Impinj Speedway R420
    Raspberry Pi OS 64-bit              4-port fixed UHF reader, LLRP
              |                                    |
              +---------- PoE switch --------------+
                          (Ethernet)

    Reader port 1  ----  START line, antenna A   \  both cover the same line, so
    Reader port 2  ----  START line, antenna B   /  one crossing reads on both

    Reader port 3  ----  FINISH line, antenna A  \  same again at the finish
    Reader port 4  ----  FINISH line, antenna B  /

Antennas are circular polarised panels. Participants wear passive UHF bib tags
(Impinj M730/M830 foam dogbone). A participant may carry two tags with
different EPCs; both map to the same bib, and whichever tag gives the earlier
valid crossing is the one used.

The port-to-line mapping lives in `reader.py` as `START_ANTENNAS` and
`FINISH_ANTENNAS`. If the antennas get cabled differently on race morning,
change it there and nothing else needs to know.

## Install

    git clone https://github.com/AddisonTech/Easymoose-racing-software.git
    cd Easymoose-racing-software
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt

Python 3.11 or newer. Dependencies are Flask (web server), sllurp (LLRP), and
pytest (tests only). SQLite comes with Python.

## Reader configuration

The R420 needs a fixed IP on the same subnet as the Pi. Set it from the
reader's own web interface, or hand it a DHCP reservation on the switch.
Nothing in this software configures the reader's network.

Then point the software at it:

    ./run.sh --reader-host 192.168.1.50

Other reader options, all with sensible defaults:

    --reader-port 5084     LLRP port, rarely changes
    --tx-power 30          transmit power in dBm

Transmit power, session and search mode are starting points in `reader.py`,
not validated numbers. Tune them against the real antennas, real tags and a
real field before trusting them at an event.

## Simulate mode

No reader, no antennas, no tags:

    ./run.sh --simulate

The simulator generates a full 5K: runners milling in the start line read zone
before the gun, a mass start with the back of the pack crossing late, dozens
of reads per crossing, dropped reads, the occasional tag that never wakes up,
stray reads from people loitering by the finish arch, a couple of DNFs and
someone whose start is missed. Playback is compressed 60x by default, so a 28 minute
race takes about half a minute, but the timestamps on the reads are real race
times and the results come out as a real 5K.

    --sim-speed 60         playback compression
    --sim-seed 1           change it for a different race, keep it to repeat one

Import a participant CSV before starting simulate mode: the simulator needs to
know who is running.

## Running a real race

1. **Before race day.** Start the console (`./run.sh --reader-host <ip>`),
   create the race, and import the participant CSV. Check the participants tab
   shows the field you expect.
2. **On site.** Cable the antennas, confirm the mapping above, and open the
   race from the archive. Press **Attach reader**. The reader line under the
   clock should start counting reads as tags come into the read zones.
3. **Spot check.** Walk a tag through each read zone and watch the read count
   move. The participants tab, filtered to runners not yet read, is the
   fastest way to catch a bib that was never assigned a tag.
4. **Start.** Press **START RACE**, then **CONFIRM START** within five
   seconds. That records the gun time. Reads taken before the gun stay in the
   log but are ignored by the rules, so it does not matter that the field has
   been standing in the start read zone for ten minutes.
5. **During.** The live results view lists finishers newest first. Press
   **EXPORT CSV** whenever anyone wants standings; it writes a timestamped
   file and never interrupts the reads.
6. **After.** Press **Stop reader**. Export once more for the final results.
   If anything needs correcting afterwards, fix it and press **Recompute from
   reads**: results are rebuilt from the raw log, so nothing is lost.

The console binds to `0.0.0.0`, so a laptop or phone on the same network can
open it at the address `run.sh` prints. Useful for putting results on a screen
at the finish while the Pi stays in a case by the antennas.

## The timing rules

Implemented in `timing.py` as pure functions over the read log, so they can be
tested without hardware or a database.

| Rule | Behaviour |
| --- | --- |
| Gun time | Recorded when the operator confirms START. |
| Start | First read of any of the participant's EPCs on ports 1 or 2 at or after the gun. |
| Finish | First crossing on ports 3 or 4 at least `min_elapsed_seconds` after that runner's own start. Default 720. |
| Bursts | Reads less than 2 seconds apart are one crossing; the earliest read in the burst is the time. |
| Dual tags | Whichever tag gives the earlier valid crossing. |
| Elapsed | Finish minus start, to hundredths of a second. |
| Start, no finish | DNF. |
| Finish, no start | Flagged for review. Never guessed. |

`min_elapsed_seconds` is per race and is set when you create it. It exists to
throw out reads from people standing near the finish arch early on. 720
seconds suits a 5K; raise it for longer distances.

## Where the files land

    races/
      2026-11-26_turkey-trot/
        race.db                        every read, participant and result
        results_20261126-091500.csv    export taken mid race
        results_20261126-094212.csv    export taken at the end

One folder per race, fully self contained. Copy the folder and you have taken
the whole event with you. The archive on the launch screen lists every race
found under `races/` and any of them can be reopened to review or re-export.

### Participant CSV, in

    bib,first_name,last_name,age,gender,epc1,epc2
    101,Ada,Lovelace,36,F,E28011700000020000000001,E28011700000020000000002
    102,Alan,Turing,41,M,E2801170000002000000000A,

`bib` and at least one EPC are required; the rest may be blank. EPCs are
normalised to uppercase hex, so spaces and dashes in the file are fine. An
import is rejected whole if a bib or an EPC appears twice.

### Results CSV, out

    place,bib,first_name,last_name,age,gender,start_time,finish_time,elapsed,status

Finishers first in place order, then everyone else. Times are local ISO 8601
with the UTC offset attached. `status` is `finished`, `dnf`, `review` or
`not_started`.

## Running at boot

    sudo cp easymoose.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now easymoose
    journalctl -u easymoose -f

Edit `User`, `WorkingDirectory` and the `--reader-host` in `ExecStart` to match
the Pi.

## Tests

    .venv/bin/python -m pytest

61 tests, a couple of seconds. They cover the timing rules at their edges
(pre-gun reads ignored, burst collapsing, minimum elapsed rejection including a
burst that straddles the cutoff, dual tag selection, DNF and review), the
storage layer, the CSV import and export, and the web endpoints. The one that
matters most checks that live processing and a recompute from the read log
produce identical results: what the operator reads off the screen during the
race has to be what gets published afterwards.

## Layout

    reader.py           TagRead, the Reader interface, and the live LLRP reader
    simulator.py        SimulatedReader, the generated race
    timing.py           the rules, pure functions over a read log
    db.py               per-race SQLite, CSV import and export, recompute
    app.py              Flask server, live session, the reader thread
    templates/          two pages: the archive and the race console
    static/             one stylesheet, one script, no CDN, no build step
    tests/              pytest
    run.sh              launcher
    easymoose.service   systemd unit for the Pi

## Licence

This software is MIT, see `LICENSE`. Note that sllurp, the LLRP library, is
GPL-3.0-only, which has implications for how a combined work can be
distributed. Flask is BSD-3-Clause and pytest is MIT.
