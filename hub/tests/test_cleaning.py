from datetime import datetime, timedelta, timezone

from app import cleaning as c
from app.washer_logic import DONE, Event, derive_runs, drum_entities

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 10, 15, 0, tzinfo=TZ)
DAY = timedelta(days=1)
CFG = c.settings(None)


def info(cleaned=(), washes=(), baseline=NOW):
    return c.clean_info(CFG, "lower", NOW, list(cleaned), list(washes), baseline)


def test_progress_grows_by_day_and_only_a_completed_clean_resets_it():
    assert info(baseline=NOW)["progress"] == 0
    i = info(baseline=NOW - 15 * DAY)
    assert i["progress"] == 0.5 and not i["due"] and i["label"] == "还有 15 天"
    i = info(baseline=NOW - 40 * DAY)
    assert i["progress"] == 1 and i["due"] and i["label"] == "该清洁了"
    # a completed clean 2 days ago resets it, whatever the baseline was
    assert info(cleaned=[NOW - 2 * DAY], baseline=NOW - 40 * DAY)["days_since"] == 2


def test_thirty_washes_also_fill_it():
    i = info(washes=[NOW - DAY + timedelta(minutes=k) for k in range(30)], baseline=NOW - 2 * DAY)
    assert i["washes_since"] == 30 and i["due"]
    # washes before the last clean don't count
    assert info(cleaned=[NOW - DAY], washes=[NOW - 2 * DAY] * 30)["washes_since"] == 0


def test_doses_upper_drum_is_a_quarter():
    assert c.dose_text(c.tablets(CFG, "upper")) == "¼ 片" and c.dose_text(c.tablets(CFG, "lower")) == "1 片"
    assert c.dose_text(c.tablets(CFG, "upper", deep=True)) == "¾ 片" and c.dose_text(c.tablets(CFG, "lower", deep=True)) == "3 片"


def test_tonight():
    assert c.minutes_until_tonight(NOW) == 450 and c.minutes_until_tonight(NOW.replace(hour=23)) == 0


def test_a_clean_that_was_stopped_is_not_completed():
    e = drum_entities("lower")
    t = lambda m: NOW + timedelta(minutes=m)
    evs = [Event(e["mode"], "筒清洁", t(0)), Event(e["status"], "待机中", t(0)), Event(e["left_time"], "58", t(1)),
           Event(e["status"], "工作中", t(1)), Event(e["status"], "待机中", t(10)),          # stopped midway
           Event(e["status"], "工作中", t(20)), Event(e["status"], DONE, t(80))]            # ran to the end
    runs = derive_runs(evs, "lower")
    assert [r.completed for r in runs] == [False, True] and all(r.mode == "筒清洁" for r in runs)
