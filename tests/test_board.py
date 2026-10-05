"""The tent display, operator corrections, and the RunSignup uploader.

The field below is small enough to reason about by hand:

    101  M 40  chip timed, 1499.0
    102  F 35  no start read, so gun timed, 1600.0
    103  M 10  junior, chip timed, 1698.0
    104  F 30  chip timed, 1399.0
    105  M 50  no tags at all: the reader never saw them
"""

from __future__ import annotations

import io

import pytest

import runsignup
from app import Console, build_board, correct, create_app
from conftest import (
    FINISH_PORT,
    GUN,
    MICROS,
    START_PORT,
    TAG_A,
    TAG_B,
    at,
    burst,
    csv_for,
)
from db import RaceDB
from simulator import make_participants
from timing import STATUS_FINISHED, STATUS_REMOVED, parse_elapsed

TAG_C = "E28011700000020000000003"
TAG_D = "E28011700000020000000004"

FIELD = [
    {"bib": "101", "first_name": "Al", "last_name": "One", "age": 40, "gender": "M", "epcs": [TAG_A]},
    {"bib": "102", "first_name": "Bea", "last_name": "Two", "age": 35, "gender": "F", "epcs": [TAG_B]},
    {"bib": "103", "first_name": "Cy", "last_name": "Three", "age": 10, "gender": "M", "epcs": [TAG_C]},
    {"bib": "104", "first_name": "Di", "last_name": "Four", "age": 30, "gender": "F", "epcs": [TAG_D]},
    {"bib": "105", "first_name": "Ed", "last_name": "Five", "age": 50, "gender": "M", "epcs": []},
]

READS = (
    burst(TAG_A, START_PORT, 1.0) + burst(TAG_A, FINISH_PORT, 1500.0)
    + burst(TAG_B, FINISH_PORT, 1600.0)
    + burst(TAG_C, START_PORT, 2.0) + burst(TAG_C, FINISH_PORT, 1700.0)
    + burst(TAG_D, START_PORT, 1.0) + burst(TAG_D, FINISH_PORT, 1400.0)
)


def timed_race(root) -> RaceDB:
    race = RaceDB.create("Panther Test", "2026-10-03", "5K", 720, root=root)
    race.add_participants(FIELD)
    race.set_gun_time(GUN, 0)
    race.append_reads(READS)
    race.recompute()
    return race


@pytest.fixture
def race(tmp_path):
    db = timed_race(tmp_path / "races")
    yield db
    db.close()


def by_bib(results):
    return {r.bib: r for r in results}


# ------------------------------------------------------------------ board


def test_the_board_lists_finishers_in_crossing_order_with_gun_and_chip_times(race):
    board = build_board(race, race.results())
    rows = [(row["place"], row["bib"], row["time"], row["chip"]) for row in board["finishers"]]
    assert rows == [
        (1, "104", "23:20.00", "23:19.00"),
        (2, "101", "25:00.00", "24:59.00"),
        (3, "102", "26:40.00", "26:40.00"),  # no start read: gun time both ways
        (4, "103", "28:20.00", "28:18.00"),
    ]


def test_the_list_and_all_three_award_boxes_rank_by_gun_time(tmp_path):
    # 301 starts at the back and crosses after 300, but ran faster on chip
    # time. Awards go by gun time, so 300 is first, in the list and in the
    # box. The juniors are the same way round and ranked the same way.
    db = RaceDB.create("Packed", "2026-10-03", "5K", 720, root=tmp_path / "races")
    db.add_participants([
        {"bib": "300", "age": 30, "gender": "M", "epcs": [TAG_A]},
        {"bib": "301", "age": 30, "gender": "M", "epcs": [TAG_B]},
        {"bib": "400", "age": 10, "gender": "F", "epcs": [TAG_C]},
        {"bib": "401", "age": 11, "gender": "M", "epcs": [TAG_D]},
    ])
    db.set_gun_time(GUN, 0)
    db.append_reads(burst(TAG_A, START_PORT, 1.0) + burst(TAG_A, FINISH_PORT, 1500.0)
                    + burst(TAG_B, START_PORT, 30.0) + burst(TAG_B, FINISH_PORT, 1510.0)
                    + burst(TAG_C, START_PORT, 1.0) + burst(TAG_C, FINISH_PORT, 1600.0)
                    + burst(TAG_D, START_PORT, 40.0) + burst(TAG_D, FINISH_PORT, 1610.0))
    board = build_board(db, db.recompute())
    assert [(row["place"], row["bib"], row["time"], row["chip"]) for row in board["finishers"]] == [
        (1, "300", "25:00.00", "24:59.00"),
        (2, "301", "25:10.00", "24:40.00"),
        (3, "400", "26:40.00", "26:39.00"),
        (4, "401", "26:50.00", "26:10.00"),
    ]
    assert [(row["bib"], row["time"]) for row in board["leaders"]["adult_male"]] == [
        ("300", "25:00.00"), ("301", "25:10.00")]
    assert [(row["bib"], row["time"]) for row in board["leaders"]["junior"]] == [
        ("400", "26:40.00"), ("401", "26:50.00")]
    db.close()


def test_the_board_shows_the_top_three_in_each_category(race):
    leaders = build_board(race, race.results())["leaders"]
    assert [row["bib"] for row in leaders["adult_male"]] == ["101"]
    assert [row["bib"] for row in leaders["adult_female"]] == ["104", "102"]
    assert [row["bib"] for row in leaders["junior"]] == ["103"]


def test_the_board_keeps_only_three_per_category(tmp_path):
    db = RaceDB.create("Big", "2026-10-03", "5K", 720, root=tmp_path / "races")
    tags = [f"E2801170000002000000{n:04d}" for n in range(5)]
    db.add_participants([
        {"bib": str(200 + n), "age": 30, "gender": "F", "epcs": [tags[n]]} for n in range(5)
    ])
    db.set_gun_time(GUN, 0)
    reads = []
    for n, tag in enumerate(tags):
        reads += burst(tag, START_PORT, 1.0) + burst(tag, FINISH_PORT, 1500.0 + n * 10)
    db.append_reads(reads)
    leaders = build_board(db, db.recompute())["leaders"]
    assert [row["bib"] for row in leaders["adult_female"]] == ["200", "201", "202"]
    db.close()


# ------------------------------------------------------------ corrections


def test_parse_elapsed_reads_a_three_group_5k_time_as_minutes():
    assert parse_elapsed("46:31:19") == 2791.19
    assert parse_elapsed("46:31.19") == 2791.19
    assert parse_elapsed("1:05:31.54") == 3931.54
    for bad in ("46", "46:61", "a:b", ""):
        with pytest.raises(ValueError):
            parse_elapsed(bad)


def test_a_runner_the_reader_missed_can_be_given_a_time(race):
    message = correct(race, race.results(), "set_time", "105", time_text="46:31:19")
    assert "46:31.19" in message
    result = by_bib(race.recompute())["105"]
    assert result.status == STATUS_FINISHED
    assert result.source == "manual"
    assert result.elapsed_seconds == 2791.19
    assert result.finish_utc == at(2791.19)
    assert build_board(race, race.results())["finishers"][-1]["bib"] == "105"


def test_swapped_bibs_swap_times_and_survive_a_recompute(race):
    correct(race, race.results(), "swap", "101", other_bib="104")
    results = by_bib(race.recompute())
    assert results["101"].elapsed_seconds == 1399.0
    assert results["104"].elapsed_seconds == 1499.0
    # The reads are untouched: clearing one side gives the computed time back.
    correct(race, race.results(), "clear", "101")
    assert by_bib(race.recompute())["101"].elapsed_seconds == 1499.0


def test_a_removed_finish_leaves_the_board_until_cleared(race):
    correct(race, race.results(), "remove", "102")
    results = by_bib(race.recompute())
    assert results["102"].status == STATUS_REMOVED
    assert "102" not in [row["bib"] for row in build_board(race, race.results())["finishers"]]
    correct(race, race.results(), "clear", "102")
    assert by_bib(race.recompute())["102"].source == "gun"


def test_corrections_refuse_what_cannot_be_done(race):
    from app import CorrectionError
    with pytest.raises(CorrectionError, match="no runner"):
        correct(race, race.results(), "remove", "999")
    with pytest.raises(CorrectionError, match="no finish time"):
        correct(race, race.results(), "swap", "101", other_bib="105")
    with pytest.raises(CorrectionError, match="not a time"):
        correct(race, race.results(), "set_time", "101", time_text="soon")
    with pytest.raises(CorrectionError, match="no correction"):
        correct(race, race.results(), "clear", "101")


# -------------------------------------------------------------- endpoints


@pytest.fixture
def client(tmp_path):
    console = Console(root=tmp_path / "races", simulate=True, reader_host="", reader_port=0,
                      tx_power_dbm=30.0, sim_speed=400.0, sim_seed=3)
    console.root.mkdir(parents=True, exist_ok=True)
    app = create_app(console)
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client, console
    console.stop_live()


def test_board_page_and_feed_load_and_have_no_controls(client, tmp_path):
    test_client, console = client
    timed_race(console.root).close()
    slug = "2026-10-03_panther-test"
    page = test_client.get(f"/races/{slug}/board")
    assert page.status_code == 200
    assert b"<button" not in page.data and b"<form" not in page.data
    feed = test_client.get(f"/api/races/{slug}/board").get_json()
    assert len(feed["finishers"]) == 4


def test_editing_a_runners_age_moves_their_category(client):
    test_client, console = client
    timed_race(console.root).close()
    slug = "2026-10-03_panther-test"
    response = test_client.post(f"/api/races/{slug}/runners/103",
                                json={"first_name": "Cy", "last_name": "Three", "age": "13", "gender": "M"})
    assert response.get_json()["ok"]
    leaders = test_client.get(f"/api/races/{slug}/board").get_json()["leaders"]
    assert [row["bib"] for row in leaders["adult_male"]] == ["101", "103"]
    assert leaders["junior"] == []


def test_a_correction_posted_from_the_console_reaches_the_board(client):
    test_client, console = client
    timed_race(console.root).close()
    slug = "2026-10-03_panther-test"
    response = test_client.post(f"/api/races/{slug}/corrections",
                                json={"action": "set_time", "bib": "105", "time": "30:00"})
    assert response.get_json()["ok"]
    feed = test_client.get(f"/api/races/{slug}/board").get_json()
    assert "105" in [row["bib"] for row in feed["finishers"]]
    bad = test_client.post(f"/api/races/{slug}/corrections", json={"action": "set_time", "bib": "105"})
    assert bad.status_code == 400


def test_stopping_the_reader_stops_the_race_clock(client):
    test_client, console = client
    test_client.post("/races", data={"name": "Clock", "date": "2026-11-26", "min_elapsed_seconds": "720"})
    slug = "2026-11-26_clock"
    test_client.post(f"/races/{slug}/participants",
                     data={"file": (io.BytesIO(csv_for(make_participants(4, seed=2)).encode()), "f.csv")},
                     content_type="multipart/form-data")
    test_client.post(f"/races/{slug}/live")
    assert test_client.get(f"/api/races/{slug}/state").get_json()["race"]["stopped_utc"] is None
    test_client.post(f"/races/{slug}/stop")
    stopped = test_client.get(f"/api/races/{slug}/state").get_json()["race"]["stopped_utc"]
    assert stopped is not None
    # Attaching again, say after a laptop restart mid race, starts it again.
    test_client.post(f"/races/{slug}/live")
    assert test_client.get(f"/api/races/{slug}/state").get_json()["race"]["stopped_utc"] is None


def test_runsignup_sending_cannot_be_turned_on_in_simulate_mode(client):
    test_client, console = client
    timed_race(console.root).close()
    slug = "2026-10-03_panther-test"
    response = test_client.post(f"/races/{slug}/runsignup", data={
        "enabled": "1", "race_id": "1", "adult_event_id": "2", "adult_result_set_id": "3"})
    assert "simulate" in response.headers["Location"]
    race = RaceDB.open(console.root / slug)
    assert race.runsignup_settings()["enabled"] == 0
    race.close()


# -------------------------------------------------------------- RunSignup


SETTINGS = {"enabled": 1, "race_id": 205792, "adult_event_id": 11, "adult_result_set_id": 21,
            "junior_event_id": 12, "junior_result_set_id": 22}


class FakeClient:
    def __init__(self):
        self.posts = []
        self.next_id = 1000

    def post_results(self, race_id, event_id, result_set_id, results):
        self.posts.append((race_id, event_id, result_set_id, [dict(r) for r in results]))
        ids = []
        for result in results:
            if "result_id" in result:
                ids.append(result["result_id"])
            else:
                self.next_id += 1
                ids.append(self.next_id)
        return ids


class FakeConsole:
    def __init__(self, root):
        self.root = root

    def current_results(self, race):
        return race.results()


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def test_results_are_routed_by_category_and_placed_within_their_set(race):
    race.save_runsignup_settings(SETTINGS)
    want, problems = runsignup.wanted(race, race.results())
    ids = {p.bib: p.participant_id for p in race.participants()}
    assert problems == []
    assert want[ids["104"]] == (21, 11, {"bib_num": 104, "place": 1, "clock_time": "0:23:20.00",
                                         "chip_time": "0:23:19.00"})
    # No start read: timed from the gun, so both times are the same.
    assert want[ids["102"]] == (21, 11, {"bib_num": 102, "place": 3, "clock_time": "0:26:40.00",
                                         "chip_time": "0:26:40.00"})
    assert want[ids["103"]] == (22, 12, {"bib_num": 103, "place": 1, "clock_time": "0:28:20.00",
                                         "chip_time": "0:28:18.00"})


def test_nothing_is_sent_until_a_result_has_been_still_for_five_minutes(race):
    race.save_runsignup_settings(SETTINGS)
    fake, clock = FakeClient(), Clock()
    uploader = runsignup.Uploader(FakeConsole(race.directory.parent), client=fake, clock=clock)

    status = uploader.cycle(race)
    assert fake.posts == [] and status["waiting"] == 4 and status["next_send_seconds"] == 300

    clock.now += 299
    uploader.cycle(race)
    assert fake.posts == []

    clock.now += 1
    status = uploader.cycle(race)
    assert status["sent"] == 4
    assert sorted((p[2], len(p[3])) for p in fake.posts) == [(21, 3), (22, 1)]

    clock.now += 600
    uploader.cycle(race)
    assert len(fake.posts) == 2, "an unchanged result is not sent again"


def test_a_correction_inside_the_window_is_the_only_version_sent(race):
    race.save_runsignup_settings(SETTINGS)
    fake, clock = FakeClient(), Clock()
    uploader = runsignup.Uploader(FakeConsole(race.directory.parent), client=fake, clock=clock)
    uploader.cycle(race)
    clock.now += 200
    correct(race, race.results(), "swap", "101", other_bib="104")
    race.recompute()
    uploader.cycle(race)
    clock.now += 150
    uploader.cycle(race)
    sent = {r["bib_num"] for _, _, _, batch in fake.posts for r in batch}
    assert 101 not in sent and 104 not in sent, "the swap restarted their five minutes"
    clock.now += 200
    uploader.cycle(race)
    adult = {r["bib_num"]: r for _, _, set_id, batch in fake.posts if set_id == 21 for r in batch}
    # The swap moves the crossing too, so the gun time follows the chip time.
    assert (adult[101]["place"], adult[101]["chip_time"], adult[101]["clock_time"]) == (
        1, "0:23:19.00", "0:23:20.00")
    assert (adult[104]["place"], adult[104]["chip_time"], adult[104]["clock_time"]) == (
        2, "0:24:59.00", "0:25:00.00")


def test_a_later_change_updates_the_same_runsignup_result(race):
    race.save_runsignup_settings(SETTINGS)
    fake, clock = FakeClient(), Clock()
    uploader = runsignup.Uploader(FakeConsole(race.directory.parent), client=fake, clock=clock)
    uploader.cycle(race)
    clock.now += 300
    uploader.cycle(race)
    first_id = race.runsignup_sent()[race.participant_id_for_bib("102")]["result_id"]

    correct(race, race.results(), "set_time", "102", time_text="26:00")
    race.recompute()
    uploader.cycle(race)
    clock.now += 300
    uploader.cycle(race)
    last = [r for r in fake.posts[-1][3] if r["bib_num"] == 102][0]
    assert last["result_id"] == first_id
    assert (last["clock_time"], last["chip_time"]) == ("0:26:00.00", "0:26:00.00")


def test_a_corrected_chip_time_moves_the_gun_time_with_it(race):
    # 101 started 1 s after the gun. Correcting their chip time to 25:30
    # puts the crossing at 25:31 from the gun.
    race.save_runsignup_settings(SETTINGS)
    correct(race, race.results(), "set_time", "101", time_text="25:30")
    want, _ = runsignup.wanted(race, race.recompute())
    payload = want[race.participant_id_for_bib("101")][2]
    assert (payload["chip_time"], payload["clock_time"]) == ("0:25:30.00", "0:25:31.00")


def test_a_sent_result_that_is_removed_is_flagged_for_a_human(race):
    race.save_runsignup_settings(SETTINGS)
    fake, clock = FakeClient(), Clock()
    uploader = runsignup.Uploader(FakeConsole(race.directory.parent), client=fake, clock=clock)
    uploader.cycle(race)
    clock.now += 300
    uploader.cycle(race)
    correct(race, race.results(), "remove", "102")
    race.recompute()
    status = uploader.cycle(race)
    assert any("102" in problem and "remove it on RunSignup" in problem for problem in status["problems"])


def test_a_failed_send_is_retried_on_the_next_pass(race):
    race.save_runsignup_settings(SETTINGS)

    class Flaky(FakeClient):
        fail = True

        def post_results(self, *args):
            if self.fail:
                raise OSError("no internet at the finish line")
            return super().post_results(*args)

    fake, clock = Flaky(), Clock()
    uploader = runsignup.Uploader(FakeConsole(race.directory.parent), client=fake, clock=clock)
    uploader.cycle(race)
    clock.now += 300
    status = uploader.cycle(race)
    assert status["error"] and status["sent"] == 0
    fake.fail = False
    clock.now += 15
    assert uploader.cycle(race)["sent"] == 4


# --------------------------------------------------------------- sponsors

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # the signature is all the upload checks


def upload_logo(test_client, slug, filename, data, name="Acme Feed & Seed"):
    return test_client.post(
        f"/races/{slug}/sponsors",
        data={"name": name, "logo": (io.BytesIO(data), filename)},
        content_type="multipart/form-data",
    )


def test_a_sponsor_logo_is_stored_with_the_race_and_shown_on_the_board(client):
    test_client, console = client
    timed_race(console.root).close()
    slug = "2026-10-03_panther-test"
    response = upload_logo(test_client, slug, "acme.png", PNG)
    assert "saved=sponsor" in response.headers["Location"]
    sponsors = test_client.get(f"/api/races/{slug}/board").get_json()["sponsors"]
    assert [s["name"] for s in sponsors] == ["Acme Feed & Seed"]
    assert (console.root / slug / "sponsors" / "1.png").read_bytes() == PNG
    served = test_client.get(sponsors[0]["url"])
    assert served.status_code == 200 and served.data == PNG
    assert b"Acme Feed &amp; Seed" in test_client.get(f"/races/{slug}").data


def test_removing_a_sponsor_deletes_the_logo(client):
    test_client, console = client
    timed_race(console.root).close()
    slug = "2026-10-03_panther-test"
    upload_logo(test_client, slug, "acme.png", PNG)
    test_client.post(f"/races/{slug}/sponsors/1/delete")
    assert test_client.get(f"/api/races/{slug}/board").get_json()["sponsors"] == []
    assert not (console.root / slug / "sponsors" / "1.png").exists()


def test_sponsor_uploads_must_really_be_raster_images(client):
    test_client, console = client
    timed_race(console.root).close()
    slug = "2026-10-03_panther-test"
    svg = upload_logo(test_client, slug, "logo.svg", b"<svg onload='alert(1)'/>")
    assert "error=" in svg.headers["Location"]
    fake = upload_logo(test_client, slug, "logo.png", b"<html>not a picture</html>")
    assert "error=" in fake.headers["Location"]
    assert test_client.get(f"/api/races/{slug}/board").get_json()["sponsors"] == []
    assert test_client.get(f"/races/{slug}/sponsors/..%2Frace.db").status_code == 404
