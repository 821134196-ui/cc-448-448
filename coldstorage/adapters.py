"""模拟适配器：产生门磁与温度读数（全部为虚构数据）。

* 温度适配器：按货格生效温区的目标温度叠加小噪声；可注入"越界/断电"情景。
* 门磁适配器：写 door_events（开/关），超时未关由监控任务计费。
* 历史回填用于演示与测试。
"""
from __future__ import annotations

import math
import random
import sqlite3
from datetime import timedelta
from typing import Optional

from . import config
from .services import effective_band
from .util import iso, now, parse

# 进程内情景表：cell_id -> {"kind": "excursion"/"outage", "until": dt, "bias": float}
_scenarios: dict[str, dict] = {}
_rng = random.Random(20260929)


def set_scenario(cell_id: str, kind: str, minutes: float, bias: float = 6.0) -> None:
    until = now() + timedelta(minutes=minutes)
    _scenarios[cell_id] = {"kind": kind, "until": until, "bias": bias}


def clear_scenarios() -> None:
    _scenarios.clear()


def insert_sample(conn: sqlite3.Connection, cell_id: str, ts: str,
                  temp_c: Optional[float], power_on: bool = True,
                  source: str = "sim") -> None:
    conn.execute(
        "INSERT INTO temp_samples(cell_id,ts,temp_c,power_on,source)"
        " VALUES(?,?,?,?,?)",
        (cell_id, ts, None if temp_c is None else round(temp_c, 2),
         1 if power_on else 0, source),
    )


def simulate_reading(conn: sqlite3.Connection, cell_id: str,
                     at=None) -> None:
    """产生一个读数。at 为 datetime（测试可注入），默认当前时刻。"""
    at = at or now()
    lo, hi = effective_band(conn, cell_id)
    target = (lo + hi) / 2.0
    sc = _scenarios.get(cell_id)
    if sc and at < sc["until"]:
        if sc["kind"] == "outage":
            insert_sample(conn, cell_id, iso(at), None, power_on=False)
            return
        # 越界：在上限（或下限）外侧漂移
        side = sc["bias"]
        edge = hi if side > 0 else lo
        temp = edge + side + _rng.uniform(-0.4, 0.4)
        insert_sample(conn, cell_id, iso(at), temp, power_on=True)
        return
    if sc and at >= sc["until"]:
        _scenarios.pop(cell_id, None)
    temp = target + _rng.uniform(-0.6, 0.6)
    insert_sample(conn, cell_id, iso(at), temp, power_on=True)


def backfill_series(conn: sqlite3.Connection, cell_id: str, start_ts: str,
                    end_ts: str, interval_min: float = 5.0,
                    outage_windows: Optional[list[tuple[str, str]]] = None,
                    excursion_windows: Optional[list[tuple[str, str, float]]] = None,
                    noise: float = 0.5, source: str = "sim-seed") -> int:
    """按固定节奏回填一段读数；断电窗内写 NULL/power_on=0，越界窗内写带外温度。

    返回插入条数。窗口边界会对齐到采样栅格，保证越界/断电连续可被积分。
    """
    lo, hi = effective_band(conn, cell_id)
    target = (lo + hi) / 2.0
    outage_windows = outage_windows or []
    excursion_windows = excursion_windows or []
    t0 = parse(start_ts)
    t1 = parse(end_ts)
    step = timedelta(minutes=interval_min)
    count = 0
    cur_t = t0
    local_rng = random.Random(hash((cell_id, start_ts)) & 0xFFFFFFFF)
    while cur_t <= t1:
        ts = iso(cur_t)
        in_outage = any(parse(a) <= cur_t <= parse(b) for a, b in outage_windows)
        if in_outage:
            insert_sample(conn, cell_id, ts, None, power_on=False, source=source)
        else:
            temp = target + local_rng.uniform(-noise, noise)
            for a, b, bias in excursion_windows:
                if parse(a) <= cur_t <= parse(b):
                    edge = hi if bias > 0 else lo
                    temp = edge + bias + local_rng.uniform(-0.3, 0.3)
            insert_sample(conn, cell_id, ts, temp, power_on=True, source=source)
        count += 1
        cur_t += step
    return count


# ---- 门磁 -------------------------------------------------------------------

def door_open(conn: sqlite3.Connection, cell_id: str, at: Optional[str] = None,
              note: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO door_events(cell_id,opened_at,source,note) VALUES(?,?,?,?)",
        (cell_id, at or iso(now()), "sim", note),
    )
    return cur.lastrowid


def door_close(conn: sqlite3.Connection, event_id: int, at: Optional[str] = None) -> None:
    conn.execute("UPDATE door_events SET closed_at=? WHERE id=? AND closed_at IS NULL",
                 (at or iso(now()), event_id))


def door_event(conn: sqlite3.Connection, cell_id: str, opened_ts: str,
               closed_ts: str, note: str = "") -> int:
    eid = door_open(conn, cell_id, opened_ts, note)
    door_close(conn, eid, closed_ts)
    return eid
