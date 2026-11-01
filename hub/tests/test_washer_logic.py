from datetime import datetime, timedelta, timezone

from app.washer_logic import (Event, WATCHED, buffer_minutes, derive_runs, drum_entities,
                              next_occurrence, plan_booking, DEFAULT_FINISH_BY)

TZ = timezone(timedelta(hours=8))
U = drum_entities("upper")


def t(h, m, day=21):
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


def ev(key, state, ts):
    return Event(U[key], state, ts)


def test_entities_are_real_ha_ids():
    assert U["status"] == "sensor.mibx5_cn_860976874_x13_status_p_12_28"
    assert U["reservation_left_time"] == "number.mibx5_cn_860976874_x13_reservation_left_time_p_12_17"
    assert drum_entities("lower")["start_wash"] == "button.mibx5_cn_860976874_x13_start_wash_a_13_2"
    assert U["status"] in WATCHED and U["start_wash"] not in WATCHED


def test_reserved_run_with_pause():
    evs = [
        ev("mode", "自定义", t(22, 0)),
        ev("status", "待机中", t(22, 0)),
        ev("left_time", "152", t(22, 1)),
        ev("status", "预约中", t(22, 2)),
        ev("status", "工作中", t(2, 58, 22)),
        ev("status", "暂停中", t(3, 30, 22)),
        ev("status", "工作中", t(3, 35, 22)),
        ev("left_time", "120", t(3, 0, 22)),
        ev("status", "洗涤完成", t(5, 40, 22)),
    ]
    [r] = derive_runs(evs, "upper")
    assert r.reserved and r.mode == "自定义" and r.estimate_min == 152
    assert r.duration_min == 162 and r.overrun_min == 10


def test_history_starting_mid_run_is_dropped_and_open_run_kept():
    evs = [
        ev("status", "工作中", t(0, 30, 22)),       # first sight is mid-run: no start
        ev("status", "洗涤完成", t(1, 0, 22)),
        ev("status", "待机中", t(1, 5, 22)),
        ev("status", "工作中", t(1, 10, 22)),
        ev("left_time", "90", t(1, 11, 22)),        # estimate arrives just after start
    ]
    [r] = derive_runs(evs, "upper")
    assert r.end is None and r.estimate_min == 90 and not r.reserved


def _runs(overruns):
    out = []
    for i, o in enumerate(overruns):
        s = t(1, 0, 1 + i)
        evs = [ev("mode", "自定义", s - timedelta(minutes=5)), ev("status", "待机中", s - timedelta(minutes=5)),
               ev("left_time", "100", s - timedelta(minutes=4)), ev("status", "工作中", s),
               ev("status", "洗涤完成", s + timedelta(minutes=100 + o))]
        out += derive_runs(evs, "upper")
    return out


def test_buffer_default_then_learned():
    assert buffer_minutes(_runs([3, 4]))[0] == 20
    assert buffer_minutes(_runs([3, -10, 12, 4]))[0] == 17          # worst 12 + 5
    assert buffer_minutes(_runs([-20, -20, -20]))[0] == 5           # floor
    assert buffer_minutes(_runs([200, 0, 0]))[0] == 60              # cap


def test_plan_normal_night():
    now = t(23, 0)
    finish_by = next_occurrence(now, DEFAULT_FINISH_BY)
    assert finish_by == t(5, 50, 22)
    p = plan_booking("upper", now, finish_by, 152, 20, "")
    assert p.ok and p.reservation_min == 390                         # 23:00 -> 05:30
    assert p.washer_finish == t(5, 30, 22) and p.expected_start == t(2, 58, 22)
    assert not any("谷电" in n for n in p.notes)


def test_plan_too_late_starts_now():
    now = t(4, 0, 22)
    p = plan_booking("upper", now, next_occurrence(now, DEFAULT_FINISH_BY), 152, 20, "")
    assert p.ok and p.reservation_min == 152 and any("立即开始" in n for n in p.notes)


def test_plan_before_valley_warns():
    now = t(18, 0)
    p = plan_booking("upper", now, next_occurrence(now, DEFAULT_FINISH_BY), 600, 20, "")
    assert p.ok and p.expected_start == t(19, 30) and any("谷电" in n for n in p.notes)


def test_plan_without_estimate_fails():
    assert not plan_booking("upper", t(23, 0), t(5, 50, 22), None, 20, "").ok


def test_program_entities_match_ha_ids():
    from app.washer_logic import program_entities
    up, low = program_entities("upper"), program_entities("lower")
    assert up["steam_time"] == ("select", "select.mibx5_cn_860976874_x13_steam_time_p_12_31")
    assert low["steam"] == ("switch", "switch.mibx5_cn_860976874_x13_steam_sterilization_p_13_18")
    assert "steam" not in up and "steam_time" not in low
    assert drum_entities("lower")["set_custom"] == "button.mibx5_cn_860976874_x13_set_customize_params_a_13_4"


def test_normalize_program_fills_and_validates():
    import pytest
    from app.washer_logic import normalize_program
    p = normalize_program("lower", {"spin": 800, "steam": 0, "bogus": 1})
    assert p["spin"] == "800" and p["steam"] is False and p["temp"] == "40" and "bogus" not in p
    with pytest.raises(ValueError):
        normalize_program("upper", {"temp": "70"})
    with pytest.raises(ValueError):
        normalize_program("upper", {"rinse": 9})


def test_summary_and_drying_check():
    from app.washer_logic import DEFAULT_PROGRAMS, check_booked_length, program_summary
    low = DEFAULT_PROGRAMS["lower"]
    assert program_summary("lower", low) == "40℃、1200转、漂洗2次、浸泡30分钟、蒸汽除菌、蓝氧除菌、高水位、智能烘干·穿着即可"
    assert program_summary("upper", DEFAULT_PROGRAMS["upper"]).startswith("60℃、1000转、漂洗2次、浸泡10分钟、蒸汽除菌")
    # 2026-10-24: booked lower reported 46 min (washed, never dried); the good booking reported 254
    assert check_booked_length(low, 46)
    assert check_booked_length(low, 254) is None
    assert check_booked_length(low, None) is None
    assert check_booked_length({**low, "drying_level": "不烘干"}, 46) is None


def test_dry_only_length_check():
    from app.washer_logic import check_booked_length, normalize_program
    p = normalize_program("lower", {"dry_only": True, "drying_level": "定时烘干", "drying_time": 30})
    assert check_booked_length(p, 30) is None      # lab 2026-10-25: 自定义 + 单烘干 plans exactly the drying time
    assert check_booked_length(p, 44) is None
    assert "洗涤" in check_booked_length(p, 69)     # 随心洗烘 + 单烘干 planned 69 min: washing as well
