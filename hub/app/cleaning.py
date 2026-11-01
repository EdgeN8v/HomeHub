"""Drum cleaning schedule: when each drum was last cleaned, how long until the next one, how much
cleaner goes in. No I/O - the washer service feeds it the run log and the saved settings.

"Cleaned" = a run in the washer's own 筒清洁 mode that ran to the end. Nothing else resets it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

CLEAN_MODE = "筒清洁"
TONIGHT_AT = (22, 30)      # cheap-power window starts at 22:00; 筒清洁 needs nobody there, only the tablet in the drum

DEFAULTS = {
    "interval_days": 30,
    "rainy_days": 0,               # 0 = no shorter interval in 梅雨 (6-7月)
    "rainy_months": [6, 7],
    "product": "绿伞泡腾片",
    # tablets per drum for a routine clean. Mijia lists the lower drum at 10 kg and the upper at 0.25 kg,
    # so the upper one gets a small piece (a quarter), not a tablet.
    "interval_washes": 30,         # or this many washes since the last clean, whichever comes first
    "doses": {"lower": 1.0, "upper": 0.25},
    "deep_mult": 3,                # first / heavy clean: this many times the routine dose
    "temp": "60",                  # 40 / 60 / 95; oxygen tablets want 40-60, and Mijia advises against cleaner above 60
}


def settings(saved: dict | None) -> dict:
    s = {**DEFAULTS, **(saved or {})}
    s["doses"] = {**DEFAULTS["doses"], **(s.get("doses") or {})}
    return s


def interval_for(cfg: dict, now: datetime) -> int:
    return int(cfg["rainy_days"] if now.month in cfg["rainy_months"] and cfg["rainy_days"] else cfg["interval_days"])


def dose_text(n: float) -> str:
    return {0.25: "¼ 片", 0.5: "半片", 0.75: "¾ 片"}.get(n) or (f"{int(n)} 片" if float(n).is_integer() else f"{n:g} 片")


def tablets(cfg: dict, drum: str, deep: bool = False) -> float:
    n = cfg["doses"][drum] * (cfg["deep_mult"] if deep else 1)
    return round(n * 4) / 4     # tablets are broken into halves and quarters, not finer


def minutes_until_tonight(now: datetime) -> int:
    """Minutes until 22:30 today; 0 when it is already past (then it simply starts)."""
    target = now.replace(hour=TONIGHT_AT[0], minute=TONIGHT_AT[1], second=0, microsecond=0)
    return max(0, int((target - now).total_seconds() // 60))


def clean_info(cfg: dict, drum: str, now: datetime, cleaned: list[datetime], washes: list[datetime],
               baseline: datetime) -> dict:
    """What the page shows for one drum.
    cleaned:  when this drum's 筒清洁 runs FINISHED (a stopped or interrupted one doesn't count - only a
              completed clean resets the countdown and the colour)
    washes:   starts of its other finished runs, to count "30 washes"
    baseline: where counting begins until the first completed clean is seen (set once, kept)
    """
    last = max(cleaned, default=baseline)
    interval = interval_for(cfg, now)
    days_since = (now - last).total_seconds() / 86400
    washes_since = sum(1 for t in washes if t > last)
    progress = min(1.0, max(days_since / interval, washes_since / cfg["interval_washes"]))
    due_in = interval - int(days_since)
    return {"interval_days": interval, "last": last.isoformat(), "from_baseline": not cleaned,
            "days_since": int(days_since), "washes_since": washes_since, "progress": round(progress, 3),
            "due": progress >= 1.0, "due_in_days": due_in,
            "label": "该清洁了" if progress >= 1.0 else f"还有 {max(due_in, 1)} 天",
            "dose": tablets(cfg, drum), "dose_text": dose_text(tablets(cfg, drum)),
            "deep_text": dose_text(tablets(cfg, drum, deep=True)), "product": cfg["product"], "temp": cfg["temp"]}
