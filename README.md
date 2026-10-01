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

### Two clocks

Read timestamps come from the reader's own clock, via the LLRP
FirstSeenTimestampUTC field. The gun time comes from the Pi's clock, because
that is what the operator's button press lands on. Those are two independent
clocks and nothing keeps them in step, so comparing a read against the gun
time raw is comparing two unrelated numbers. A reader a minute out would shift
or destroy every start in the race, quietly.

The software handles this itself. On connect it measures the difference
between the reader's clock and the Pi's, preferring the UTCTimestamp the
reader sends in its connection event and falling back to the first tag report
if the reader does not send one. When you confirm START, the gun time is
recorded twice: once as the Pi saw it, and once converted into the reader's
clock domain. Only the converted one is compared against reads. Read
timestamps themselves are never adjusted; they stay exactly as the reader
reported them.

The measured offset appears on the reader status line as soon as the reader
attaches. Past two seconds it also raises a warning on screen. Times are still
corrected at any offset, but a gap that wide means something is wrong with the
setup and is worth fixing before the gun rather than trusting the arithmetic.

The console will refuse to record a gun time if no reader is attached, or if
the offset has not been measured yet, because there would be nothing to
convert it with. If it says the offset is not measured, present a tag to an
antenna and try again.

### Keeping the reader's clock close: chrony

Correcting for the offset is the first layer and it works on its own. Keeping
the clocks close anyway is the second, and it makes the numbers in the logs
and the exports easier to reason about when something needs investigating.

Run the Pi as the reader's time source:

    sudo apt install chrony

Then in `/etc/chrony/chrony.conf`, serve the local network and keep serving
time even when the Pi itself has no upstream, which is the normal case at a
race in a field:

    allow 192.168.1.0/24
    local stratum 10

Restart it and check it is listening:

    sudo systemctl restart chrony
    chronyc clients

Then point the reader at it: in the R420's web interface, under the network or
time settings, set the NTP server to the Pi's IP address. Give it a few
minutes and the offset on the console should settle near zero.

### The Pi has no real time clock

A Raspberry Pi has no battery backed clock. With no network at boot it comes
up believing whatever `fake-hwclock` wrote down when it was last shut down,
which can be days out.

Elapsed times are unaffected, because they are differences between two reads
and a wrong clock shifts both ends equally. What does go wrong is everything
absolute: the race date, the timestamps in export filenames, and the times of
day shown in the results and the exports.

If the Pi will be used offline, a DS3231 module on the I2C header fixes it for
a couple of pounds. Optional, not required, and nothing in the software
depends on it.

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
    --sim-clock-skew 0     seconds the simulated reader's clock runs ahead of
                           the Pi, for exercising the offset correction

Import a participant CSV before starting simulate mode: the simulator needs to
know who is running.

## Pairing bibs

Each bib has a tag stuck to it, and the participant CSV needs to know which
EPC is on which bib. Everything in this section that has runner names or tag
pairs in it lives in `Data/`, which git ignores. The repo is public, so keep
registration exports, rosters and pair files in there.

### Assign bibs

Put the registration export in `Data/`, then:

    .venv/bin/python assign_bibs.py Data/<registration export>.csv --title "Race name - Bib pickup"

Runners are sorted by last name, then first name, ignoring case, and numbered
from 110. It writes two files and prints counts only, never names:

    Data/registration_bibs.csv   bib,first_name,last_name,age,gender,event,tshirt
    Data/pickup_sheet.html       printable pickup list, one blank row per spare bib

The export itself is left untouched. Running it again keeps the bibs already
assigned, because by then they may be printed and handed out, and only
rebuilds the sheet. `--force` reassigns everyone from scratch.

### Pair tags to bibs

Run this with the race console stopped, because the reader only takes one
LLRP client at a time:

    .venv/bin/python pair.py

It shows the bib number in large type, with the runner's name from
`Data/registration_bibs.csv` (or `spare` for an unassigned bib):

    Bib 110 - Jane Doe - waiting

Hold that bib over antenna 1. When exactly one tag is read, it rings the bell,
writes `bib,epc1` to `Data/pairs.csv` straight away, and moves to the next
bib. Take the bib away before presenting the next one, because nothing is
captured until the field has been quiet for a moment. Two tags in the field
are rejected with `MORE THAN ONE TAG, rescan`, and a tag that is already
paired shows `ALREADY BIB X`.

    Enter   skip this bib
    b       go back one bib and clear its pair
    q       quit

Quitting is safe at any point. Run it again and it carries on from the first
bib not yet in `Data/pairs.csv`.

    --start 110 --end 310            bib range, defaults are this event's bibs
    --power 10                       dBm, for this session only
    --min-rssi -45                   ignore weaker reads while pairing
    --host 192.168.10.20             reader IP
    --out Data/pairs.csv             pairs file
    --roster Data/registration_bibs.csv   names to show; used by default if present
    --simulate                       no reader, generated tags

Power and the RSSI floor were set against the R420 and DogBone tags. At 12
dBm, a bib held over the antenna read at about -30 dBm, while a bib lying a
few feet away read continuously at about -55. Turning the power down cannot
keep that one out, because 10 dBm is the reader's lowest setting. The RSSI
floor does. If bibs on the table still cause rejections, move them further
away before changing these numbers.

### Verify

Run verify mode and walk the bibs past the antenna:

    .venv/bin/python pair.py --verify

Every tag shows its bib and runner, or `UNKNOWN TAG` if it is not in
`Data/pairs.csv`, with a running count of bibs confirmed. When you quit, it
lists the paired bibs it never saw.

### Merge

    .venv/bin/python merge.py Data/registration_bibs.csv Data/pairs.csv --out Data/participants.csv

`Data/participants.csv` is the participant CSV the console imports. Every
paired bib goes in, named or not, so spare bibs and day-of signups still get
times. Registered bibs with no pair are left out and listed, because a bib
without a tag cannot be timed. A registration export with headers like
`First Name` also works, and any other columns are ignored. Registrations
with no bib number are counted and flagged, because they can't be matched to
anything.

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

107 tests, a few seconds. They cover the timing rules at their edges (pre-gun
reads ignored, burst collapsing, minimum elapsed rejection including a burst
that straddles the cutoff, dual tag selection, DNF and review), the storage
layer, the CSV import and export, the web endpoints, EPC decoding from live
reader reports, and bib assignment, pairing, verify and merge.

Two of them matter more than the rest. One checks that live processing and a
recompute from the read log produce identical results: what the operator reads
off the screen during the race has to be what gets published afterwards. The
other runs a whole race with the reader's clock 37 seconds out and demands the
results come out right anyway, because a simulator that stamps reads from the
same clock the gun comes from assumes away the worst bug this software can
have.

## Layout

    reader.py           TagRead, the Reader interface, and the live LLRP reader
    simulator.py        SimulatedReader, the generated race
    timing.py           the rules, pure functions over a read log
    db.py               per-race SQLite, CSV import and export, recompute
    app.py              Flask server, live session, the reader thread
    pair.py             bib to EPC pairing and verify, off the reader
    assign_bibs.py      bib numbers for a registration export, pickup sheet
    merge.py            registration plus pairs.csv into a participant CSV
    templates/          two pages: the archive and the race console
    static/             one stylesheet, one script, no CDN, no build step
    tests/              pytest
    run.sh              launcher
    easymoose.service   systemd unit for the Pi

## Licence

GPL-3.0-only. The full text is in `LICENSE`.

The choice is not really a choice: sllurp, the LLRP library this depends on to
talk to the reader at all, is GPL-3.0-only, and a combined work that links it
has to be distributed under the same terms. The other dependencies are more
permissive and impose nothing here: Flask is BSD-3-Clause, pytest is MIT, and
SQLite is public domain.
