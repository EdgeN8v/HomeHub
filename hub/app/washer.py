"""Washer service: keeps the run log fed from HA and books both drums in one go."""
from __future__ import annotations

import asyncio
import contextvars
import time as _time
import logging
import uuid

import aiohttp
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import cleaning
from .ha import HA
from .local_bridge import DirectFailed
from .store import Store
from .washer_logic import (DRY_TIMES, CUSTOM_MODE, QUICK_LEVELS, QUICK_MODE, quick_entities, DEFAULT_FINISH_BY, DONE, DRUMS, IDLE, PAUSED, POWER, PROGRAM_FIELDS,
                           REMOTE_CONTROL, RESERVED, WATCHED, WORKING, booking_biases, buffer_minutes, calibrated_buffer, check_booked_length,
                           derive_runs, drum_entities, next_occurrence, normalize_program, plan_booking,
                           program_entities, program_summary)

log = logging.getLogger("washer")
TZ = ZoneInfo("Asia/Shanghai")
BACKFILL_DAYS = 10   # HA keeps 10 days of history by default


def _why(e) -> str:
    """What to tell the phone about a failed write: the device's own refusal when it was sent directly, else Home Assistant's status."""
    return e.message if isinstance(e, DirectFailed) else f"HA {e.status}"


class ProgramError(Exception):
    pass


_job_ctx: contextvars.ContextVar = contextvars.ContextVar("washer_job", default=None)


class Washer:
    def __init__(self, ha: HA, store: Store):
        self.ha = ha
        self.store = store
        self.lock = asyncio.Lock()
        self.jobs: dict[str, dict] = {}   # background bookings from the home-page card buttons
        self.stopped_at: dict[str, float] = {}      # drum -> when somebody last pressed 停止洗涤 on it
        self.refused: set[tuple[str, str, str]] = set()   # (drum, mode, setting) the washer locks in that mode: not tried again

    def in_background(self, drum: str, make) -> str:
        """Bookings run here and the phone polls job(): the washer says 预约中 within seconds
        (job["booked"]), but checking it took the right program takes ~10 s more (job["result"]).
        make(on_booked) returns the coroutine to run."""
        job_id = uuid.uuid4().hex[:12]
        self.jobs[job_id] = {"drum": drum, "done": False, "booked": None, "result": None, "stage": "排队中…", "stages": [], "t0": _time.time()}

        def on_booked(out: dict):
            self.jobs[job_id]["booked"] = dict(out)

        async def run():
            _job_ctx.set(job_id)
            try:
                self.jobs[job_id]["result"] = await make(on_booked)
            except Exception as e:   # noqa: BLE001 - reported to the phone instead of lost
                log.exception("background booking failed")
                self.jobs[job_id]["result"] = {"drum": drum, "name": DRUMS[drum]["name"], "ok": False, "notes": [f"出错了：{e}"]}
            self.jobs[job_id]["done"] = True
            self.jobs[job_id]["stage"] = "完成"
            self._stage_log(job_id, "完成")
        asyncio.get_running_loop().create_task(run())
        if len(self.jobs) > 50:
            for k in list(self.jobs)[:-50]:
                self.jobs.pop(k, None)
        return job_id

    def job(self, job_id: str) -> dict | None:
        return self.jobs.get(job_id)

    def note_stop(self, entity_id: str):
        """The phone pressed 停止洗涤 (home_call): remembered, so a booking that is being checked can tell 'cancelled by you' from 'failed'."""
        for d in DRUMS:
            if entity_id == drum_entities(d)["stop_washing"]:
                self.stopped_at[d] = _time.time()

    def stage(self, text: str):
        """What the running background job is doing right now - the phone shows it as small text under the button."""
        jid = _job_ctx.get()
        j = self.jobs.get(jid) if jid else None
        if j is None:
            return
        j["stage"] = text
        self._stage_log(jid, text)

    def _stage_log(self, jid: str, text: str):
        j = self.jobs.get(jid)
        if j is None:
            return
        el = round(_time.time() - j["t0"], 1)
        j["stages"].append([el, text])
        log.info("washer job %s +%.1fs %s", jid, el, text)

    # ---- recording -------------------------------------------------------------------------
    async def backfill(self):
        start = datetime.now(TZ) - timedelta(days=BACKFILL_DAYS)
        rows = await self.ha.history(start, WATCHED)
        n = self.store.add_events(rows)
        log.info("backfill: %d history rows, %d new", len(rows), n)

    async def on_change(self, entity_id: str, new: dict):
        if entity_id in WATCHED:
            self.store.add_events([(entity_id, new["state"], new["last_changed"])])

    def runs(self, drum: str):
        return derive_runs(self.store.events(), drum)

    # ---- status ----------------------------------------------------------------------------
    async def status(self) -> dict:
        ids = [POWER, REMOTE_CONTROL] + [e for d in DRUMS for e in drum_entities(d).values()]
        st = await self.ha.states(ids)
        val = lambda e: st.get(e, {}).get("state")  # noqa: E731
        now = datetime.now(TZ)
        drums = {}
        for d, meta in DRUMS.items():
            ents = drum_entities(d)
            runs = self.runs(d)
            cal = calibrated_buffer(booking_biases(self.store.bookings(400), runs, d, CUSTOM_MODE, self.program(d).get("estimate_min")))
            buf, buf_note = cal or buffer_minutes(runs)
            status = val(ents["status"])
            info = {
                "name": meta["name"], "status": status, "run_status": val(ents["run_status"]),
                "left_time": _int(val(ents["left_time"])), "mode": val(ents["mode"]),
                "door": val(ents["door_state"]), "fault": val(ents["fault"]),
                "reservation_left_time": _int(val(ents["reservation_left_time"])),
                "buffer_min": buf, "buffer_note": buf_note,
                "runs": [_run_json(r) for r in reversed(runs[-15:])],
                "program": self.program(d), "program_summary": program_summary(d, self.program(d)),
                "program_fields": {k: {"label": PROGRAM_FIELDS[k][0], "kind": kind, "choices": PROGRAM_FIELDS[k][3]}
                                   for k, (kind, _e) in program_entities(d).items()},
            }
            info["clean"] = self.clean_info(d, now)
            exp = self.store.get_setting("washer_expect", {}).get(d)
            if exp and status not in ("预约中", "工作中"):
                # run finished/stopped: drop the stale expected-finish so it can't mislead a later cycle on this drum
                m = self.store.get_setting("washer_expect", {}); m.pop(d, None); self.store.set_setting("washer_expect", m); exp = None
            fin = None
            if exp and status in ("预约中", "工作中") and datetime.fromisoformat(exp) > now:
                fin = datetime.fromisoformat(exp)
            elif status == "预约中" and info["reservation_left_time"] is not None:
                fin = now + timedelta(minutes=info["reservation_left_time"])
            elif status == "工作中" and info["left_time"] is not None:
                fin = now + timedelta(minutes=info["left_time"])
            if fin is not None:
                # expose the finish instant so the phone derives 还剩 from the SAME source as 约X洗完 —
                # otherwise 还剩 (live entity) and 约 (this poll) refresh on different clocks and visibly disagree
                info["finish_eta"] = _hm(fin)
                info["finish_at"] = fin.isoformat()
            drums[d] = info
        return {"now": now.isoformat(), "power": val(POWER), "remote_control": val(REMOTE_CONTROL),
                "drums": drums, "modes": self.mode_params(), "default_finish_by": DEFAULT_FINISH_BY.strftime("%H:%M"), "clean_cfg": self.clean_cfg(),
                "bookings": self.store.bookings(10)}

    # ---- what each mode was last set to (所有手机共用，下次打开就是上次改完的样子) --------------------
    MODE_KEYS = {"dry_only", "blue_oxygen", "steam", "soak", "temp", "rinse", "spin", "drying_level", "drying_degree", "drying_time",
                 "high_water", "sleep_mode", "steam_time", "shake", "quick_level"}

    def mode_params(self) -> dict:
        return self.store.get_setting("washer_modes", {}) or {}

    def save_mode_params(self, drum: str, mode: str, params: dict) -> dict:
        if drum not in DRUMS or not mode or mode == CUSTOM_MODE:
            raise ValueError("mode")
        clean = {k: v for k, v in params.items() if k in self.MODE_KEYS and isinstance(v, (str, int, float, bool))}
        clean["dry_only"] = False          # 单烘干 is a one-off, never what the mode opens with next time
        if clean.get("drying_level") == "定时烘干" and isinstance(clean.get("drying_time"), (int, float)):
            clean["drying_time"] = min(DRY_TIMES, key=lambda t: abs(t - clean["drying_time"]))
        allm = self.mode_params()
        allm[f"{drum}/{mode}"] = clean
        self.store.set_setting("washer_modes", allm)
        return clean

    # ---- drum cleaning ---------------------------------------------------------------------
    def clean_cfg(self) -> dict:
        return cleaning.settings(self.store.get_setting("washer_clean"))

    def save_clean_settings(self, patch: dict) -> dict:
        cfg = self.clean_cfg()
        if "interval_washes" in patch:
            cfg["interval_washes"] = min(300, max(5, int(patch["interval_washes"])))
        if "interval_days" in patch:
            cfg["interval_days"] = min(180, max(7, int(patch["interval_days"])))
        if "rainy_days" in patch:
            cfg["rainy_days"] = min(180, max(0, int(patch["rainy_days"])))
        if "temp" in patch:
            if str(patch["temp"]) not in ("40", "60", "95"):
                raise ValueError("温度只能是 40 / 60 / 95")
            cfg["temp"] = str(patch["temp"])
        if "product" in patch:
            cfg["product"] = str(patch["product"])[:20] or cfg["product"]
        for d, v in (patch.get("doses") or {}).items():
            if d in DRUMS:
                cfg["doses"][d] = min(10.0, max(0.0, round(float(v) * 4) / 4))
        self.store.set_setting("washer_clean", cfg)
        return cfg

    def clean_info(self, drum: str, now: datetime) -> dict:
        runs = [r for r in self.runs(drum) if r.end is not None]
        cleaned = [r.end for r in runs if r.mode == cleaning.CLEAN_MODE and r.completed]
        washes = [r.start for r in runs if r.mode != cleaning.CLEAN_MODE]
        base = self.store.get_setting("washer_clean_baseline", {}) or {}
        if drum not in base:      # first time seen: start counting today (never from a guess about the past)
            base[drum] = now.isoformat()
            self.store.set_setting("washer_clean_baseline", base)
        return cleaning.clean_info(self.clean_cfg(), drum, now, cleaned, washes, datetime.fromisoformat(base[drum]))

    async def clean(self, drum: str, tonight: bool = False, on_booked=None) -> dict:
        """Start the washer's own 筒清洁 on one drum (tablet already in the empty drum), now or tonight at
        22:30. The temperature is set from the settings; if the washer drops it, its default runs."""
        cfg = self.clean_cfg()
        ents, name = drum_entities(drum), DRUMS[drum]["name"]
        out = {"drum": drum, "name": name, "mode": cleaning.CLEAN_MODE, "ok": False, "steps": [], "notes": [], "tonight": tonight}
        power = await self._power_on(False)
        st = await self.ha.states(list(ents.values()))
        status, fault = st[ents["status"]]["state"], st[ents["fault"]]["state"]
        if power != "on":
            out["notes"].append("洗衣机没开机，远程开机也没成功")
        if status not in (IDLE, DONE):
            out["notes"].append(f"{name}现在是「{status}」，要「{IDLE}」才能开始")
        if fault not in ("No Faults", None):
            out["notes"].append(f"{name}有故障：{fault}")
        if out["notes"]:
            return out
        async with self.lock:
            await self.ha.call("select", "select_option", {"entity_id": ents["mode"], "option": cleaning.CLEAN_MODE})
            await asyncio.sleep(0.5)
            _, temp_entity = program_entities(drum)["temp"]
            try:
                await self.ha.call("select", "select_option", {"entity_id": temp_entity, "option": cfg["temp"]})
                out["steps"].append(f"水温 {cfg['temp']}℃")
            except aiohttp.ClientResponseError:
                out["notes"].append(f"水温没设上，按洗衣机默认的来")
            await asyncio.sleep(0.4)
            minutes = cleaning.minutes_until_tonight(datetime.now(TZ)) if tonight else 0
            if minutes > 0:
                await self.ha.call("select", "select_option", {"entity_id": ents["reservation_wash_status"], "option": "Start Wash"})
                await self.ha.call("number", "set_value", {"entity_id": ents["reservation_left_time"], "value": minutes})
                out["steps"].append(f"{minutes} 分钟后（22:30）开始")
            else:
                await self.ha.call("select", "select_option", {"entity_id": ents["reservation_wash_status"], "option": "None"})
            await self.ha.call("button", "press", {"entity_id": ents["start_wash"]})
            out["steps"].append("按下开始")
            flipped = await self._wait_flip(ents)
        out["result_status"] = flipped
        out["ok"] = bool(flipped)
        if flipped:
            if on_booked:
                on_booked({**out})
        else:
            out["notes"].append(f"按了开始，但{name}没有开始，可能门没关好或者要先开远程控制")
        self.store.add_booking(datetime.now(TZ), drum, False, out["ok"], out)
        return out

    # ---- booking ---------------------------------------------------------------------------
    async def _power_on(self, dry_run: bool) -> str | None:
        self.stage("正在查洗衣机是否开机…")
        async with self.lock:
            power = (await self.ha.state(POWER))["state"]
            if power != "on" and not dry_run:
                self.stage("洗衣机关着，正在远程开机…")
                await self.ha.call("switch", "turn_on", {"entity_id": POWER})
                for _ in range(12):
                    await asyncio.sleep(0.5)
                    power = (await self.ha.state(POWER))["state"]
                    if power == "on":
                        await asyncio.sleep(1)   # let the drums report 待机中
                        break
            return power

    async def book(self, drums: list[str], finish_by: time, dry_run: bool, on_booked=None) -> list[dict]:
        """一键预约: every drum on its saved 自定义 program, done before finish_by (with the overrun buffer)."""
        power = await self._power_on(dry_run)
        results = []
        for d in drums:
            r = await self._book_one(d, next_occurrence(datetime.now(TZ), finish_by), dry_run, power, on_booked=on_booked)
            self.store.add_booking(datetime.now(TZ), d, dry_run, r["ok"], r)
            results.append(r)
        return results

    async def check_mode(self, drum: str, mode: str):
        modes = (await self.ha.state(drum_entities(drum)["mode"])).get("attributes", {}).get("options") or []
        if mode not in modes:
            raise ValueError(f"{DRUMS[drum]['name']}没有「{mode}」这个模式")

    async def start(self, drum: str, mode: str, prog: dict | None, finish_at: datetime | None, on_booked=None) -> dict:
        """The 开始 / 预约 buttons of one mode page: start now, or finish at exactly finish_at.
        For 自定义 the given program is saved first, so it is what the next 一键预约 uses too."""
        persist = True
        if (prog or {}).get("dry_only"):
            # 单烘干 is a one-off in whatever mode the page is on; it must not become the saved 自定义
            # program 一键预约 uses, so nothing about it is stored.
            prog = normalize_program(drum, prog)
            if prog["drying_level"] == "不烘干":
                raise ValueError("单烘干要先选一种烘干方式")
            prog["estimate_min"] = prog["drying_time"] if prog["drying_level"] == "定时烘干" else 90
            persist = False
        elif mode == CUSTOM_MODE:
            prog = self.save_program(drum, {**self.program(drum), **(prog or {})})
        else:
            prog = normalize_program(drum, prog or {})
            prog["estimate_min"] = (QUICK_LEVELS[prog["quick_level"]][1] if mode == QUICK_MODE and prog.get("quick_level") in QUICK_LEVELS
                                    else self.store.get_setting("washer_estimates", {}).get(f"{drum}/{mode}") or 60)
        power = await self._power_on(False)
        r = await self._book_one(drum, finish_at, False, power, mode=mode, prog=prog, use_buffer=False,
                                 on_booked=on_booked, persist=persist)
        self.store.add_booking(datetime.now(TZ), drum, False, r["ok"], r)
        return r

    async def _book_one(self, drum: str, finish_by: datetime | None, dry_run: bool, power: str | None,
                        mode: str = CUSTOM_MODE, prog: dict | None = None, use_buffer: bool = True,
                        on_booked=None, persist: bool = True) -> dict:
        ents = drum_entities(drum)
        name = DRUMS[drum]["name"]
        prog = prog or self.program(drum)
        out = {"drum": drum, "name": name, "mode": mode, "dry_run": dry_run, "ok": False, "steps": [], "notes": [],
               "program": program_summary(drum, prog)}
        self.stage(f"正在读取{name}的状态…")
        st = await self.ha.states(list(ents.values()))
        status = st[ents["status"]]["state"]
        # Just switched on from off: the drum takes a few seconds (up to ~20) to report 待机中. Wait for it instead of
        # failing with 「已关机」, so one tap is enough, as in Mijia.
        if power == "on" and not dry_run and status in ("已关机", "unavailable", "unknown"):
            for i in range(40):
                self.stage(f"等{name}开机就绪（{i // 2} 秒）…")
                await asyncio.sleep(0.5)
                st = await self.ha.states(list(ents.values()))
                status = st[ents["status"]]["state"]
                if status not in ("已关机", "unavailable", "unknown"):
                    await asyncio.sleep(1)
                    st = await self.ha.states(list(ents.values()))
                    status = st[ents["status"]]["state"]
                    break
        fault = st[ents["fault"]]["state"]
        blockers = []
        if power != "on":
            blockers.append("洗衣机没开机" + ("" if dry_run else "，远程开机也没成功"))
        if status not in (IDLE, DONE):
            blockers.append(f"{name}现在是「{status}」，要「{IDLE}」才能{'预约' if finish_by else '开始'}")
        # The door state isn't checked up front: for ~15 s after power-on it still says 舱门已开
        # (2026-10-24/25), and waiting that out made the second drum slow. A door that really is
        # open makes the washer report 门未关好 E2 right after the start, which _settle() catches.
        if dry_run and st[ents["door_state"]]["state"] == "舱门已开":
            out["notes"].append(f"{name}门好像开着（刚开机时这个状态不准）")
        if fault not in ("No Faults", None):
            blockers.append(f"{name}有故障：{fault}")
        out["notes"] += blockers
        if blockers and not dry_run:
            return out

        # The washer's left_time isn't refreshed in standby, so it can't tell how long the program
        # we are about to write takes; use what the washer reported when this program was last booked.
        estimate = prog["estimate_min"]
        now = datetime.now(TZ)
        cal = None
        if finish_by is None:
            reservation = None
        else:
            runs = self.runs(drum)
            cal = calibrated_buffer(booking_biases(self.store.bookings(400), runs, drum, mode, estimate), early=use_buffer)
            if cal:
                buf, buf_note = cal
            else:
                buf, buf_note = buffer_minutes(runs) if use_buffer else (0, "按你选的时间洗完，不另留余量")
            plan = plan_booking(drum, now, finish_by, estimate, buf, buf_note)
            out["notes"] += plan.notes if use_buffer else plan.notes[1:]
            out["plan"] = {
                "finish_by": _hm(plan.finish_by), "estimate_min": plan.estimate_min, "buffer_min": plan.buffer_min,
                "reservation_min": plan.reservation_min, "expected_start": _hm(plan.expected_start),
                "washer_finish": _hm(plan.washer_finish),
                "washer_finish_at": plan.washer_finish.isoformat() if plan.washer_finish else None,
            }
            if dry_run:
                out["notes"].append(f"会先写入自定义程序：{out['program']}")
            if not plan.ok or dry_run:
                out["ok"] = plan.ok
                return out
            reservation = plan.reservation_min

        for attempt in (1, 2):
            # Only the writes need the washer to ourselves; the other drum can be written while this
            # one's result is being checked.
            t_attempt = _time.time()
            self.stage(f"等{name}空出来…" if self.lock.locked() else f"正在给{name}写入程序…")
            async with self.lock:
                self.stage(f"正在给{name}写入程序…")
                try:
                    await self._write_program(drum, prog, out, mode)
                except ProgramError as e:
                    out["notes"].append(str(e))
                    return out
                before = _int((await self.ha.state(ents["left_time"]))["state"])
                self.stage("程序写好了，正在按「开始」…")
                try:
                    await self._press_start(ents, reservation, out)
                except ProgramError as e:
                    out["notes"].append(str(e))
                    return out
                self.stage(f"等{name}回应（开始洗/预约中）…")
                flipped = await self._wait_flip(ents)
            if flipped and on_booked:
                on_booked({**out, "ok": True, "result_status": flipped, "result_reservation_min": reservation})
            self.stage(f"{name}已接受，正在核对程序有没有写对…")
            result_status, left, fault = await self._settle(ents)
            out["result_status"] = result_status
            faulted = fault not in ("No Faults", None)
            if flipped and result_status not in (RESERVED, WORKING) and self.stopped_at.get(drum, 0) > t_attempt:
                # the washer took it (预约中 / 工作中), then somebody pressed 停止洗涤 while it was being checked: not a failure
                out["cancelled"] = True
                out["notes"].append(f"{name}约上后被停止了（你点了停止），已取消")
                return out
            if result_status not in (RESERVED, WORKING) or faulted:
                if fault == "门未关好 E2":
                    note = f"{name}门没关好，没约上。关好门再点一次"
                else:
                    note = f"按了开始，但{name}状态是「{result_status}」，没有{'进入预约' if reservation else '开始洗'}"
                    note += f"（{fault}）" if faulted else "。可能需要先在洗衣机上打开远程控制"
                out["notes"].append(note)
                if result_status in (PAUSED, RESERVED, WORKING):
                    await self.ha.call("button", "press", {"entity_id": ents["stop_washing"]})
                    out["steps"].append("取消（停止洗涤）")
                return out
            out["result_reservation_min"] = reservation
            out["booked_length_min"] = left
            problem = check_booked_length(prog, left)
            if problem and left == before:
                # No new length reported; the old value says nothing about this booking.
                problem = None
                out["notes"].append("洗衣机没有报新的程序时长，没法确认烘干是否生效")
            if not problem:
                break
            out["notes"].append(problem)
            await self.ha.call("button", "press", {"entity_id": ents["stop_washing"]})
            out["steps"].append("取消这次预约（停止洗涤）")
            if on_booked:
                on_booked({**out, "ok": False, "retrying": attempt == 1})
            await asyncio.sleep(5)
            if attempt == 2:
                out["notes"].append("重写程序后还是一样，已取消，请在米家里约这一桶")
                return out
            out["notes"].append("重新写入程序再约一次")
        out["ok"] = True
        # The washer's own live countdown is unreliable (see calibrated_buffer); when WE know the real finish, store it so
        # finish_eta stays stable instead of following the washer's jumpy self-report. Fixed-length runs (定时单烘干 / 快速各档不烘干)
        # pin to 开始+时长; a calibrated 预约 pins to the target finish; anything dynamic (智能洗/烘) is left to the washer.
        fixed = bool((prog.get("dry_only") and prog.get("drying_level") == "定时烘干")
                     or (mode == QUICK_MODE and not prog.get("dry_only") and prog.get("drying_level") == "不烘干"))
        expect = self.store.get_setting("washer_expect", {})
        if finish_by is not None and ((cal and persist) or fixed):
            expect[drum] = finish_by.isoformat()
        elif finish_by is None and fixed and estimate:
            expect[drum] = (datetime.now(TZ) + timedelta(minutes=estimate)).isoformat()
        else:
            expect.pop(drum, None)
        self.store.set_setting("washer_expect", expect)
        if left and left != before and persist:
            if mode == CUSTOM_MODE:
                self.save_program(drum, {**prog, "estimate_min": left})
            elif mode != QUICK_MODE:
                est = self.store.get_setting("washer_estimates", {})
                self.store.set_setting("washer_estimates", {**est, f"{drum}/{mode}": left})
        return out

    async def _set(self, kind: str, eid: str, v):
        if kind == "select":
            await self.ha.call("select", "select_option", {"entity_id": eid, "option": str(v)})
        elif kind == "number":
            await self.ha.call("number", "set_value", {"entity_id": eid, "value": v})
        else:
            await self.ha.call("switch", "turn_on" if v else "turn_off", {"entity_id": eid})

    async def _write_program(self, drum: str, prog: dict, out: dict, mode: str = CUSTOM_MODE):
        """Mode first, then the settings one after another as fast as the washer answers (~0.3 s
        each). Sending them all at once makes it refuse some as busy (-30012, 2026-10-24)."""
        ents = drum_entities(drum)
        await self.ha.call("select", "select_option", {"entity_id": ents["mode"], "option": mode})
        await asyncio.sleep(0.5)
        if mode == QUICK_MODE and not prog.get("dry_only"):
            qe = quick_entities(drum)
            try:
                if prog.get("quick_level"):
                    await self.ha.call("select", "select_option", {"entity_id": qe["level"], "option": prog["quick_level"]})
                    await asyncio.sleep(0.4)
                if "shake" in prog:
                    await self.ha.call("number", "set_value", {"entity_id": qe["shake"], "value": prog["shake"]})
                    await asyncio.sleep(0.3)
            except aiohttp.ClientResponseError as e:
                out["notes"].append(f"快速的档位没写进去（{_why(e)}）")
        if prog.get("dry_only"):
            await self._write_dry_only(drum, prog, out, mode)
            return
        todo = [k for k in program_entities(drum) if not (k == "drying_time" and prog["drying_level"] != "定时烘干")]
        for k, (kind, eid) in program_entities(drum).items():
            if k == "drying_time" and prog["drying_level"] != "定时烘干":
                continue    # the washer rejects it (-4404) unless 定时烘干 is selected
            if mode != CUSTOM_MODE and (drum, mode, k) in self.refused:
                continue    # known: this mode locks it (Mijia greys it out), so no 1.5 s of retries each time
            self.stage(f"写入设置 {todo.index(k) + 1}/{len(todo)}：{PROGRAM_FIELDS[k][0]}")
            for attempt in (1, 2, 3):
                try:
                    await self._set(kind, eid, prog[k])
                    break
                except aiohttp.ClientResponseError as e:
                    if attempt < 3:   # busy: give it a moment
                        await asyncio.sleep(0.4)
                        continue
                    if mode != CUSTOM_MODE:   # built-in modes lock some settings; Mijia greys those out
                        out["notes"].append(f"「{mode}」不能改{PROGRAM_FIELDS[k][0]}，按洗衣机默认")
                        self.refused.add((drum, mode, k))
                        break
                    raise ProgramError(f"洗衣机不接受「{PROGRAM_FIELDS[k][0]} = {prog[k]}」（{_why(e)}），没有预约") from e
        if mode == CUSTOM_MODE:
            await self.ha.call("button", "press", {"entity_id": ents["set_custom"]})
        out["steps"].append(f"写入{'自定义程序' if mode == CUSTOM_MODE else '「' + mode + '」'}：{program_summary(drum, prog)}")
        await asyncio.sleep(0.5)

    async def _write_dry_only(self, drum: str, prog: dict, out: dict, mode: str):
        """Lab 2026-10-25 (lower drum, 随心洗烘, six trials): the washer plans only the drying (30 of 30
        min, stage 烘干中) when it is given exactly 单烘干, then the drying time, then the drying level
        - in that order, as separate writes, nothing else. Writing the level before the time, or any
        washing setting (spin, soak, steam, high water ...), made it plan the whole wash again
        (59-69 min, 称重中 -> 主洗中): that is how HomeHub wet dry clothes. Setting the washer's own
        displayed values doesn't show in HA, so the planned length is what gets checked afterwards."""
        ents = drum_entities(drum)
        ids = {k: eid for k, (_kind, eid) in program_entities(drum).items()}
        try:
            await self.ha.call("switch", "turn_on", {"entity_id": ids["dry_only"]})
            await asyncio.sleep(0.4)
            if prog["drying_level"] == "定时烘干":
                await self.ha.call("number", "set_value", {"entity_id": ids["drying_time"], "value": prog["drying_time"]})
                await asyncio.sleep(0.4)
            await self.ha.call("select", "select_option", {"entity_id": ids["drying_level"], "option": prog["drying_level"]})
            await asyncio.sleep(0.3)
        except aiohttp.ClientResponseError as e:
            raise ProgramError(f"洗衣机不接受单烘干的设置（{_why(e)}），没有开始") from e
        out["steps"].append(f"写入「{mode}」：单烘干 {prog['drying_time']} 分钟" if prog["drying_level"] == "定时烘干"
                            else f"写入「{mode}」：单烘干（智能烘干）")

    async def _press_start(self, ents: dict, minutes: int | None, out: dict):
        """minutes=None starts right away."""
        await self.ha.call("select", "select_option",
                           {"entity_id": ents["reservation_wash_status"], "option": "Finish Wash" if minutes else "None"})
        if minutes:
            await self.ha.call("number", "set_value", {"entity_id": ents["reservation_left_time"], "value": minutes})
        for attempt in (1, 2, 3, 4):      # the washer answers an error (HA 500) while it is still settling after power-on / the mode write
            try:
                await self.ha.call("button", "press", {"entity_id": ents["start_wash"]})
                break
            except aiohttp.ClientResponseError as e:
                if attempt == 4:
                    raise ProgramError(f"洗衣机没有接受「开始」（{_why(e)}），请再点一次") from e
                await asyncio.sleep(1.5)
        out["steps"].append(f"预约 {minutes} 分钟后洗完，按下开始" if minutes else "按下开始")

    async def _wait_flip(self, ents: dict) -> str | None:
        """The status the washer switched to (预约中 / 工作中) within ~10 s, or None."""
        for _ in range(20):
            await asyncio.sleep(0.5)
            s = (await self.ha.state(ents["status"]))["state"]
            if s in (RESERVED, WORKING):
                return s
            if s == PAUSED:
                return None
        return None

    async def _settle(self, ents: dict) -> tuple[str, int | None, str]:
        """门未关好 E2 only shows up ~8 s after 预约中 (2026-10-24 21:21), so watch a bit longer
        before calling the booking good."""
        ids = [ents["status"], ents["left_time"], ents["fault"]]
        for _ in range(9):
            await asyncio.sleep(1)
            after = await self.ha.states(ids)
            status, fault = after[ents["status"]]["state"], after[ents["fault"]]["state"]
            if fault not in ("No Faults", None) or status not in (RESERVED, WORKING):
                break
        return status, _int(after[ents["left_time"]]["state"]), fault

    # ---- programs ---------------------------------------------------------------------------
    def program(self, drum: str) -> dict:
        return normalize_program(drum, self.store.get_setting(f"washer_program_{drum}", {}))

    def save_program(self, drum: str, prog: dict) -> dict:
        # 单烘干 is a one-off (see start()); saved, 一键预约 would only dry from then on
        prog = normalize_program(drum, {**prog, "dry_only": False})
        self.store.set_setting(f"washer_program_{drum}", prog)
        return prog


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _hm(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(TZ).strftime("%H:%M")


def _run_json(r) -> dict:
    return {
        "start": r.start.astimezone(TZ).strftime("%m-%d %H:%M"),
        "end": _hm(r.end), "mode": r.mode, "reserved": r.reserved,
        "duration_min": None if r.duration_min is None else round(r.duration_min),
        "estimate_min": None if r.estimate_min is None else round(r.estimate_min),
        "overrun_min": None if r.overrun_min is None else round(r.overrun_min),
    }
