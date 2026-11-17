"""Washer logic with no I/O: which HA entities belong to which drum, how to cut the
recorded state history into wash runs, and how to plan a booking that finishes in time.

The washer (米家双区洗衣机 Pro, mibx5.x13) reserves by "finish in N minutes" and starts at
finish - (its own estimate). Its estimate is often off, so we measure every real run and
pull the finish target earlier by the worst recent overrun.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

PREFIX = "mibx5_cn_860976874_x13"

# MIoT service 12 = 上桶, 13 = 下桶. Property/action ids differ slightly between drums.
DRUMS = {
    "upper": {"name": "上桶", "siid": 12},
    "lower": {"name": "下桶", "siid": 13},
}


DRY_TIMES = (30, 60, 90, 120, 180)      # minutes of timed drying the washer accepts (150 and everything else is refused)
DRY_ONLY_TIMES = tuple(range(20, 190, 10))   # with 单烘干 on it takes 20-180 in steps of 10 (tested on the upper drum 2026-11-12, as Mijia offers)


def entity(kind: str, key: str, drum: str) -> str:
    return f"{kind}.{PREFIX}_{key}_p_{DRUMS[drum]['siid']}_{_PIID[key]}"


_PIID = {
    "status": 28,               # 工作状态: 待机中/暂停中/工作中/预约中/洗涤完成/已关机
    "run_status": 11,           # 运行阶段: 称重中/主洗中/...
    "left_time": 10,            # 剩余时间 (min)
    "mode": 3,                  # 模式
    "door_state": 9,            # 门状态
    "reservation_wash_status": 16,  # None / Start Wash / Finish Wash
    "reservation_left_time": 17,    # 预约剩余时间 (min, 0-1440)
    "fault": 4,
}


def drum_entities(drum: str) -> dict[str, str]:
    return {
        "status": entity("sensor", "status", drum),
        "run_status": entity("sensor", "run_status", drum),
        "left_time": entity("sensor", "left_time", drum),
        "mode": entity("select", "mode", drum),
        "door_state": entity("sensor", "door_state", drum),
        "reservation_wash_status": entity("select", "reservation_wash_status", drum),
        "reservation_left_time": entity("number", "reservation_left_time", drum),
        "fault": entity("sensor", "fault", drum),
        "start_wash": f"button.{PREFIX}_start_wash_a_{DRUMS[drum]['siid']}_2",
        "stop_washing": f"button.{PREFIX}_stop_washing_a_{DRUMS[drum]['siid']}_1",
        "set_custom": f"button.{PREFIX}_set_customize_params_a_{DRUMS[drum]['siid']}_4",
    }


# ---- 自定义 program ---------------------------------------------------------------------------
# Selecting 自定义 alone makes the washer run whatever custom program it has stored itself, which
# is not the one set up in the Mijia app (2026-10-24: lower drum ran 800 rpm, no steam, no
# drying). Mijia writes the whole program before every start, so we do the same.
# key: (label, kind, entity per drum — absent if the drum lacks it, choices for select / range for number)
PROGRAM_FIELDS = {
    "dry_only": ("单烘干", "switch", {"upper": "dry_only_p_12_34", "lower": "dry_only_p_13_32"}, None),
    "blue_oxygen": ("蓝氧除菌", "switch", {"lower": "blue_oxygen_p_13_33"}, None),
    "temp": ("水温", "select", {"upper": "target_temperature_p_12_5", "lower": "target_temperature_p_13_5"},
             ["常温", "30", "40", "60", "95"]),
    "spin": ("转速", "select", {"upper": "spin_speed_p_12_7", "lower": "spin_speed_p_13_7"},
             ["无", "400", "600", "800", "1000", "1200"]),
    "rinse": ("漂洗次数", "number", {"upper": "rinsh_times_p_12_6", "lower": "rinsh_times_p_13_6"}, [0, 5]),
    "soak": ("浸泡(分钟)", "number", {"upper": "soak_time_p_12_12", "lower": "soak_time_p_13_12"}, [0, 120]),
    "steam_time": ("蒸汽(分钟)", "select", {"upper": "steam_time_p_12_31"}, ["0", "12", "18"]),
    "steam": ("蒸汽除菌", "switch", {"lower": "steam_sterilization_p_13_18"}, None),
    "high_water": ("高水位", "switch", {"upper": "high_water_switch_p_12_19", "lower": "high_water_switch_p_13_19"}, None),
    "drying_level": ("烘干", "select", {"upper": "drying_level_p_12_14", "lower": "drying_level_p_13_14"},
                     ["智能烘干", "定时烘干", "不烘干"]),
    "drying_degree": ("烘干程度", "select", {"upper": "drying_degree_p_12_29", "lower": "drying_degree_p_13_29"},
                      ["Store", "Wear Immediately", "Ironing"]),
    "drying_time": ("定时烘干(分钟)", "number", {"upper": "drying_time_p_12_8", "lower": "drying_time_p_13_8"}, [0, 180]),   # the machine takes only DRY_TIMES (tested on the real washer; 20, 40, 50 ... are refused with -4404)
    "sleep_mode": ("夜间模式", "switch", {"upper": "sleep_mode_p_12_23", "lower": "sleep_mode_p_13_23"}, None),
}
DRYING_DEGREE_LABELS = {"Store": "收纳干", "Wear Immediately": "穿着即可", "Ironing": "熨烫干", "Close": "关"}

# What the user runs as 自定义 in Mijia: upper confirmed by the user, lower taken from the run on
# 2026-10-23 that dried (plan 称重/蒸汽/浸泡/主洗/漂洗/脱水/烘干, washer estimate 254 min).
# estimate_min is the program length the washer reports once booked; re-learned on every booking.
DEFAULT_PROGRAMS = {
    "upper": {"dry_only": False, "temp": "60", "spin": "1000", "rinse": 2, "soak": 10, "steam_time": "18",
              "high_water": False, "drying_level": "智能烘干", "drying_degree": "Wear Immediately", "drying_time": 0,
              "sleep_mode": False, "estimate_min": 235},
    "lower": {"dry_only": False, "blue_oxygen": True, "temp": "40", "spin": "1200", "rinse": 2, "soak": 30,
              "steam": True, "high_water": True, "drying_level": "智能烘干", "drying_degree": "Wear Immediately",
              "drying_time": 0, "sleep_mode": False, "estimate_min": 254},
}

# A washed-only lower run is ~46 min against 254 with drying, so a booked program reporting much
# less than its usual length has lost its drying step.
SUSPICIOUS_SHORT_RATIO = 0.6
DRY_ONLY_SLACK_MIN = 15   # weighing etc. on top of the drying time


# 快速 has three sub-modes in Mijia (极快 12 / 超快 20 / 快速 30 min), kept in the washer's "快速洗档位" select.
QUICK_MODE = "快速"
QUICK_LEVELS = {"Level1": ("极快", 12), "Level2": ("超快", 20), "Level3": ("快速", 30)}
QUICK_DEFAULT = {"quick_level": "Level2", "temp": "常温", "rinse": 1, "spin": "800", "drying_level": "不烘干", "shake": 0}
_QUICK_PIID = {"upper": (33, 24), "lower": (31, 24)}      # (quick_wash_level, shake_time) property ids


def quick_entities(drum: str) -> dict[str, str]:
    lvl, shake = _QUICK_PIID[drum]
    siid = DRUMS[drum]["siid"]
    return {"level": f"select.{PREFIX}_quick_wash_level_p_{siid}_{lvl}", "shake": f"number.{PREFIX}_shake_time_p_{siid}_{shake}"}


def program_entities(drum: str) -> dict[str, tuple[str, str]]:
    """key -> (kind, entity id) for the fields this drum has."""
    return {k: (kind, f"{kind}.{PREFIX}_{ents[drum]}")
            for k, (_label, kind, ents, _choices) in PROGRAM_FIELDS.items() if drum in ents}


def normalize_program(drum: str, prog: dict) -> dict:
    """Fill gaps from the default and check every value; raises ValueError on a bad one."""
    out = dict(DEFAULT_PROGRAMS[drum])
    for k, (kind, _eid) in program_entities(drum).items():
        if k not in prog:
            continue
        v, choices = prog[k], PROGRAM_FIELDS[k][3]
        if kind == "select":
            v = str(v)
            if v not in choices:
                raise ValueError(f"{PROGRAM_FIELDS[k][0]} 只能是 {choices}")
        elif kind == "number":
            v = int(v)
            if not choices[0] <= v <= choices[1]:
                raise ValueError(f"{PROGRAM_FIELDS[k][0]} 要在 {choices[0]}–{choices[1]} 之间")
            if k == "drying_time" and v:
                v = min(DRY_ONLY_TIMES if prog.get("dry_only") else DRY_TIMES, key=lambda t: abs(t - v))   # the nearest the machine accepts
        else:
            v = bool(v)
        out[k] = v
    if prog.get("quick_level") in QUICK_LEVELS:
        out["quick_level"] = prog["quick_level"]
    if "shake" in prog:
        out["shake"] = max(0, min(1440, int(prog["shake"])))
    if prog.get("estimate_min"):
        out["estimate_min"] = int(prog["estimate_min"])
    return out


def program_dries(prog: dict) -> bool:
    return prog.get("drying_level", "不烘干") != "不烘干"


def program_summary(drum: str, prog: dict) -> str:
    if prog.get("dry_only"):
        return "单烘干"
    parts = ["常温" if prog["temp"] == "常温" else f"{prog['temp']}℃", f"{prog['spin']}转", f"漂洗{prog['rinse']}次"]
    if prog.get("soak"):
        parts.append(f"浸泡{prog['soak']}分钟")
    if prog.get("steam") or ("steam_time" in program_entities(drum) and prog.get("steam_time", "0") != "0"):
        parts.append("蒸汽除菌")
    if prog.get("blue_oxygen"):
        parts.append("蓝氧除菌")
    if prog.get("high_water"):
        parts.append("高水位")
    if prog["drying_level"] == "不烘干":
        parts.append("不烘干")
    elif prog["drying_level"] == "定时烘干":
        parts.append(f"定时烘干{prog['drying_time']}分钟")
    else:
        parts.append(f"智能烘干·{DRYING_DEGREE_LABELS.get(prog['drying_degree'], prog['drying_degree'])}")
    return "、".join(parts)


def check_booked_length(prog: dict, reported_min: int | None) -> str | None:
    """After booking, the washer reports the length of what it will actually run. Returns a
    problem description, or None when it looks like the program we wrote."""
    if reported_min is None or reported_min <= 0:
        return None
    if prog.get("dry_only") and prog.get("drying_level") == "定时烘干":
        # 2026-10-25: a dry-only start the washer ignored planned the whole wash too (69 min for 30 of drying)
        limit = prog["drying_time"] + DRY_ONLY_SLACK_MIN
        if reported_min > limit:
            return f"只烘干 {prog['drying_time']} 分钟，洗衣机却排了 {reported_min} 分钟，它把洗涤也排进去了"
        return None
    expected = prog.get("estimate_min")
    if program_dries(prog) and expected and reported_min < expected * SUSPICIOUS_SHORT_RATIO:
        return f"洗衣机说只要 {reported_min} 分钟，这个程序平时要 {expected} 分钟左右，看起来烘干没有生效"
    return None


POWER = f"switch.{PREFIX}_on_p_2_1"
REMOTE_CONTROL = f"binary_sensor.{PREFIX}_child_protected_status_p_2_4"

BUTTONS = ("start_wash", "stop_washing", "set_custom")
WATCHED = sorted(
    {POWER, REMOTE_CONTROL}
    | {e for d in DRUMS for k, e in drum_entities(d).items() if k not in BUTTONS + ("reservation_wash_status",)}
)

IDLE = "待机中"
WORKING = "工作中"
PAUSED = "暂停中"
RESERVED = "预约中"
DONE = "洗涤完成"
OFF = "已关机"

CUSTOM_MODE = "自定义"
DEFAULT_FINISH_BY = time(6, 0)
VALLEY_START = time(22, 0)

DEFAULT_BUFFER_MIN = 20     # until we have enough measured runs
MIN_BUFFER_MIN = 5
MAX_BUFFER_MIN = 60
RUNS_FOR_BUFFER = 10
MIN_RUNS_FOR_BUFFER = 3


@dataclass
class Event:
    entity_id: str
    state: str
    ts: datetime


@dataclass
class Run:
    drum: str
    start: datetime
    end: datetime | None
    mode: str | None
    estimate_min: float | None   # washer's left_time when the run started
    reserved: bool               # started from 预约中
    completed: bool = False      # ended in 洗涤完成, not stopped or powered off midway

    @property
    def duration_min(self) -> float | None:
        return None if self.end is None else (self.end - self.start).total_seconds() / 60

    @property
    def overrun_min(self) -> float | None:
        if self.duration_min is None or self.estimate_min is None:
            return None
        return self.duration_min - self.estimate_min


def _num(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def derive_runs(events: list[Event], drum: str) -> list[Run]:
    """Cut one drum's history into runs. A run starts when status enters 工作中 from anything but
    暂停中, and ends at the first status that is not 工作中/暂停中. Runs whose start we never saw
    (history begins mid-run) are dropped; a run still going has end=None."""
    ents = drum_entities(drum)
    evs = sorted((e for e in events if e.entity_id in (ents["status"], ents["left_time"], ents["mode"])),
                 key=lambda e: e.ts)
    runs: list[Run] = []
    status = None           # None = not seen yet
    left_time = None
    mode = None
    current: Run | None = None
    awaiting_estimate = False
    for e in evs:
        if e.entity_id == ents["left_time"]:
            v = _num(e.state)
            if v is not None:
                left_time = v
                if awaiting_estimate and current is not None and v > 0:
                    current.estimate_min = v
                    awaiting_estimate = False
            continue
        if e.entity_id == ents["mode"]:
            if e.state not in ("unknown", "unavailable"):
                mode = e.state
            continue
        new = e.state
        if new in ("unknown", "unavailable") or new == status:
            continue
        if new == WORKING and status is not None and status != PAUSED and current is None:
            current = Run(drum=drum, start=e.ts, end=None, mode=mode,
                          estimate_min=left_time if left_time else None, reserved=(status == RESERVED))
            # left_time often updates a moment after the status flips; take that if we had none
            awaiting_estimate = current.estimate_min is None
        elif new not in (WORKING, PAUSED) and current is not None:
            current.end = e.ts
            current.completed = new == DONE
            runs.append(current)
            current = None
            awaiting_estimate = False
        status = new
    if current is not None:
        runs.append(current)
    return runs


def buffer_minutes(runs: list[Run], mode: str = CUSTOM_MODE) -> tuple[int, str]:
    """How much earlier than the target we tell the washer to finish, from the worst overrun of
    the last finished runs in this mode."""
    done = [r for r in runs if r.end is not None and r.mode == mode and r.overrun_min is not None]
    recent = sorted(done, key=lambda r: r.start)[-RUNS_FOR_BUFFER:]
    if len(recent) < MIN_RUNS_FOR_BUFFER:
        return DEFAULT_BUFFER_MIN, f"实测记录只有 {len(recent)} 次，先按默认余量 {DEFAULT_BUFFER_MIN} 分钟"
    worst = max(r.overrun_min for r in recent)
    b = int(min(MAX_BUFFER_MIN, max(MIN_BUFFER_MIN, math.ceil(worst) + 5)))
    return b, f"最近 {len(recent)} 次最多比预估多用 {worst:.0f} 分钟，留 {b} 分钟余量"


# ---- learning when a booking really ends --------------------------------------------------------
# The washer starts at (finish it was told) - (its own estimate) and then takes as long as it takes: the upper drum's
# smart drying runs ~90 min longer than the estimate, the lower one ~80 min shorter, and the drying alone varies by
# +-10 min from night to night. So every reservation we made is compared with when its run really ended; the typical
# difference ("bias") is what the next reservation is shifted by, and it moves with the last runs.
CALIB_KEEP = 7           # latest runs that count
CALIB_MIN = 3            # fewer than this: fall back to the worst-overrun buffer
CALIB_OUTLIER_MIN = 30   # a run this far from the typical one (door opened, power cut ...) is ignored
CALIB_ESTIMATE_TOL = 12  # same program = the washer's estimate for it within this many minutes
AIM_EARLY_MAX = 8        # aim a little before the target (late costs peak-rate electricity), at most this much


def _wf_at(detail: dict, ts: datetime) -> datetime | None:
    plan = detail.get("plan") or {}
    if plan.get("washer_finish_at"):
        return datetime.fromisoformat(plan["washer_finish_at"])
    hm = plan.get("washer_finish")
    if not hm or plan.get("reservation_min") is None:
        return None
    h, m = hm.split(":")
    t = ts.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    return t if t >= ts - timedelta(minutes=30) else t + timedelta(days=1)


def booking_biases(bookings: list[dict], runs: list[Run], drum: str, mode: str, estimate: float | None) -> list[tuple[datetime, float]]:
    """(when booked, minutes the run really ended after the finish time we told the washer) for every finished reservation
    of this drum, mode and program (told apart by the washer's estimate for it)."""
    out = []
    for b in bookings:
        d = b["detail"]
        if b["dry_run"] or not b["ok"] or b["drum"] != drum or d.get("mode", CUSTOM_MODE) != mode:
            continue
        plan = d.get("plan") or {}
        if estimate and abs((plan.get("estimate_min") or 0) - estimate) > CALIB_ESTIMATE_TOL:
            continue
        ts = datetime.fromisoformat(b["ts"])
        wf = _wf_at(d, ts)
        if wf is None:
            continue
        run = next((r for r in runs if r.start >= ts - timedelta(minutes=5)), None)
        if run is None or run.end is None or not run.completed or run.start > ts + timedelta(hours=26):
            continue
        out.append((ts, (run.end - wf).total_seconds() / 60))
    return sorted(out)


def calibrated_buffer(samples: list[tuple[datetime, float]], early: bool = True) -> tuple[int, str] | None:
    """Minutes to pull the finish time the washer is told by (negative: push it later), from the latest runs."""
    biases = [b for _, b in samples[-(CALIB_KEEP + 2):]]
    if len(biases) < CALIB_MIN:
        return None
    med = statistics.median(biases)
    kept = [b for b in biases if abs(b - med) <= CALIB_OUTLIER_MIN][-CALIB_KEEP:]
    if len(kept) < CALIB_MIN:
        return None
    bias = statistics.median(kept)
    spread = statistics.pstdev(kept)
    margin = min(AIM_EARLY_MAX, round(spread / 2)) if early else 0
    note = (f"最近 {len(kept)} 次实测：洗衣机实际比它自己报的{'晚' if bias >= 0 else '早'}结束 {abs(bias):.0f} 分钟（每晚波动约 ±{spread:.0f}），"
            f"按这个倒推{f'，再提前 {margin} 分钟' if margin else ''}")
    return round(bias + margin), note


def next_occurrence(now: datetime, t: time) -> datetime:
    target = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


@dataclass
class Plan:
    drum: str
    ok: bool
    finish_by: datetime
    estimate_min: float | None = None
    buffer_min: int | None = None
    reservation_min: int | None = None   # value written to 预约剩余时间 (Finish Wash)
    expected_start: datetime | None = None
    washer_finish: datetime | None = None
    notes: list[str] = field(default_factory=list)


def plan_booking(drum: str, now: datetime, finish_by: datetime, estimate_min: float | None,
                 buffer_min: int, buffer_note: str) -> Plan:
    p = Plan(drum=drum, ok=False, finish_by=finish_by, estimate_min=estimate_min, buffer_min=buffer_min)
    p.notes.append(buffer_note)
    if not estimate_min or estimate_min <= 0:
        p.notes.append("读不到洗衣机对这个程序的预估时长，无法计算")
        return p
    washer_finish = finish_by - timedelta(minutes=buffer_min)
    minutes = math.floor((washer_finish - now).total_seconds() / 60)
    if minutes < estimate_min:
        p.notes.append(f"离 {finish_by:%H:%M} 只剩 {(finish_by - now).total_seconds() / 60:.0f} 分钟，"
                       f"不够洗（预估 {estimate_min:.0f} + 余量 {buffer_min}），会立即开始")
        minutes = math.ceil(estimate_min)
    if minutes > 1440:
        p.notes.append("预约超过 24 小时，洗衣机不支持")
        return p
    p.reservation_min = minutes
    p.washer_finish = now + timedelta(minutes=minutes)
    p.expected_start = p.washer_finish - timedelta(minutes=estimate_min)
    valley_start = datetime.combine(finish_by.date() - timedelta(days=1), VALLEY_START, finish_by.tzinfo)
    if p.expected_start < valley_start:
        p.notes.append(f"预计 {p.expected_start:%H:%M} 开始，早于谷电 {VALLEY_START:%H:%M}")
    p.ok = True
    return p
