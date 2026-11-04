from datetime import datetime, timedelta, timezone

from app.aclean import TZ, clean_info, daily_hours, daily_kwh, parse_run, weighted_hours

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=TZ)


def test_progress_follows_weighted_running_hours_or_calendar_whichever_is_further():
    r = clean_info({"baseline": (NOW - timedelta(days=10)).isoformat()}, NOW, weighted=100)
    assert r["progress"] == 0.5 and r["driver"] == "hours" and r["text"] == "100/200 小时" and not r["due"]
    r = clean_info({"baseline": (NOW - timedelta(days=135)).isoformat()}, NOW, weighted=20)
    assert r["driver"] == "days" and r["text"] == "135/180 天" and r["progress"] == 0.75
    assert clean_info({"last": (NOW - timedelta(days=3)).isoformat()}, NOW, weighted=250)["due"]


def test_weighted_hours_counts_cooling_more_and_only_after_the_last_clean():
    daily = {"2026-10-30": {"modes": {"cool": 10}}, "2026-11-01": {"modes": {"cool": 2, "heat": 4, "fan_only": 2}}}
    assert weighted_hours(daily, "2026-11-01") == 2 * 1.5 + 4 + 1
    assert weighted_hours(daily, "2026-10-01") == 10 * 1.5 + 8


def test_run_report_parsing():
    assert parse_run("1,35,2,1") == {"running": True, "progress": 35, "stage": 2, "winding_down": False}
    assert parse_run("1,90,5,0")["winding_down"] and not parse_run("0,100,5,1")["running"]
    assert parse_run("1") is None and parse_run("") is None


def test_daily_kwh_from_a_cumulative_meter():
    t = lambda d, h: datetime(2026, 11, d, h, tzinfo=TZ)
    assert daily_kwh([(t(1, 8), 10.0), (t(1, 20), 10.5), (t(2, 9), 11.0)]) == {"2026-11-01": 0.5, "2026-11-02": 0.5}


def test_daily_hours_cut_at_midnight_split_by_mode_and_idle_skipped():
    t = lambda d, h: datetime(2026, 11, d, h, tzinfo=TZ).astimezone(timezone.utc)
    rows = [(t(1, 22), "heat"), (t(2, 2), "off"), (t(2, 20), "cool")]
    assert daily_hours(rows, t(2, 23)) == {"2026-11-01": {"heat": 2.0}, "2026-11-02": {"heat": 2.0, "cool": 3.0}}
