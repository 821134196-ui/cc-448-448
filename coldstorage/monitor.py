"""进程内监控任务。

职责：
1. 定时为在租货格产生模拟温度读数（适配器）；
2. 扫描采样序列，识别"连续越界"与"断电"片段（超过抗抖宽限才入账，按分钟积分补偿）；
3. 扫描门磁记录，开门超过宽限期未关 → 门未关加收。

检测函数 ``detect_all`` 可独立调用（测试与演示回填后立即结算异常）。
每个异常片段以 (lease, kind, start_ts) 去重，重复扫描不重复入账。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import timedelta
from typing import Optional

from . import adapters, config
from .util import iso, minutes_between, now, now_iso, parse

_GAP_LIMIT_SEC = 900  # 相邻异常采样超过该间隔视为两个片段


# ---- 在租占用区间（含移格） --------------------------------------------------

def occupancy_intervals(conn: sqlite3.Connection, lease: sqlite3.Row):
    """返回 [(cell_id, from_ts, to_ts, lo, hi)]，移格时刻切分。

    每个区间按当时所在温区的阈值带判定越界（跨温区移格后不能拿新带判旧格）。
    """
    moves = conn.execute(
        "SELECT from_cell,to_cell,done_at FROM moves WHERE lease_id=?"
        " AND status='done' ORDER BY done_at", (lease["id"],)).fetchall()
    intervals = []
    first = conn.execute(
        "SELECT from_cell FROM moves WHERE lease_id=? ORDER BY done_at LIMIT 1",
        (lease["id"],)).fetchone()
    start_cell = first["from_cell"] if first else lease["cell_id"]
    cur_cell, cur_from = start_cell, lease["started_at"]
    for m in moves:
        z = conn.execute("SELECT zone_code FROM cells WHERE id=?",
                         (cur_cell,)).fetchone()["zone_code"]
        zc = config.ZONES[z]
        intervals.append((cur_cell, cur_from, m["done_at"],
                          zc["temp_min"], zc["temp_max"]))
        cur_cell, cur_from = m["to_cell"], m["done_at"]
    end = lease["ended_at"] or iso(now())
    z = conn.execute("SELECT zone_code FROM cells WHERE id=?",
                     (cur_cell,)).fetchone()["zone_code"]
    zc = config.ZONES[z]
    intervals.append((cur_cell, cur_from, end, zc["temp_min"], zc["temp_max"]))
    return intervals


def _existing(conn: sqlite3.Connection, lease_id: int, kind: str,
              start_ts: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM incidents WHERE lease_id=? AND kind=? AND start_ts=?",
        (lease_id, kind, start_ts)).fetchone() is not None


def _runs(rows: list[sqlite3.Row], predicate, start_from: str, end_to: str):
    """把采样行切成 predicate=True 的连续片段，返回 [(first_ts,last_ts,n)]。"""
    runs = []
    cur_start = None
    last_ts = None
    n = 0
    prev_ts = None
    for row in rows:
        ts = row["ts"]
        if not (start_from <= ts <= end_to):
            continue
        hit = predicate(row)
        if hit:
            if cur_start is None:
                cur_start = ts
            elif prev_ts is not None and (parse(ts) - parse(prev_ts)).total_seconds() > _GAP_LIMIT_SEC:
                runs.append((cur_start, last_ts, n))
                cur_start = ts
                n = 0
            n += 1
            last_ts = ts
        else:
            if cur_start is not None:
                runs.append((cur_start, last_ts, n))
                cur_start = None
                n = 0
        prev_ts = ts
    if cur_start is not None:
        runs.append((cur_start, last_ts, n))
    return runs


def detect_cell(conn: sqlite3.Connection, lease: sqlite3.Row,
                cell_id: str, from_ts: str, to_ts: str,
                lo: float, hi: float) -> list[int]:
    rows = conn.execute(
        "SELECT ts,temp_c,power_on FROM temp_samples WHERE cell_id=?"
        " AND ts>=? AND ts<=? ORDER BY ts", (cell_id, from_ts, to_ts)).fetchall()

    # 管理员改温按生效时刻切换判定带
    overrides = conn.execute(
        "SELECT new_min,new_max,created_at FROM temp_overrides WHERE cell_id=?"
        " ORDER BY created_at", (cell_id,)).fetchall()

    def band_at(ts: str) -> tuple[float, float]:
        band = (lo, hi)
        for ov in overrides:
            if ov["created_at"] <= ts:
                band = (ov["new_min"], ov["new_max"])
        return band

    created = []

    # 断电片段
    for s, e, n in _runs(rows, lambda r: not r["power_on"], from_ts, to_ts):
        dur_min = minutes_between(s, e)
        if dur_min < config.EXCURSION_GRACE_SEC / 60.0 or n < 2:
            continue
        if _existing(conn, lease["id"], "outage", s):
            continue
        amount = -round(dur_min * config.OUTAGE_RATE_PER_MIN, 2)
        cur = conn.execute(
            "INSERT INTO incidents(lease_id,cell_id,kind,ts,start_ts,end_ts,detail,"
            "amount,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (lease["id"], cell_id, "outage", s, s, e,
             f"断电 {dur_min:.0f} 分钟（{s} 至 {e}），按 {config.OUTAGE_RATE_PER_MIN} 元/分钟补偿",
             amount, now_iso()),
        )
        created.append(cur.lastrowid)

    # 越界片段（有电但温度在当时生效带外）。
    def out_of_band(r):
        blo, bhi = band_at(r["ts"])
        return bool(r["power_on"]) and r["temp_c"] is not None and (
            r["temp_c"] < blo or r["temp_c"] > bhi)

    for s, e, n in _runs(rows, out_of_band, from_ts, to_ts):
        dur_min = minutes_between(s, e)
        if dur_min < config.EXCURSION_GRACE_SEC / 60.0 or n < 2:
            continue
        if _existing(conn, lease["id"], "excursion", s):
            continue
        sample_temps = [r["temp_c"] for r in rows
                        if s <= r["ts"] <= e and r["temp_c"] is not None]
        blo, bhi = band_at(s)
        peak = max(sample_temps, key=lambda v: abs(v - (blo + bhi) / 2))
        side = "偏高" if peak > bhi else "偏低"
        amount = -round(dur_min * config.EXCURSION_RATE_PER_MIN, 2)
        cur = conn.execute(
            "INSERT INTO incidents(lease_id,cell_id,kind,ts,start_ts,end_ts,detail,"
            "amount,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (lease["id"], cell_id, "excursion", s, s, e,
             f"温度{side}（阈值带 {blo}~{bhi}℃，极值 {peak}℃）持续 {dur_min:.0f} 分钟，"
             f"按 {config.EXCURSION_RATE_PER_MIN} 元/分钟补偿",
             amount, now_iso()),
        )
        created.append(cur.lastrowid)

    return created


def detect_doors(conn: sqlite3.Connection) -> list[int]:
    """门未关：持续超过宽限期的开门记录每次加收 DOOR_OPEN_FEE（去重）。"""
    created = []
    rows = conn.execute(
        "SELECT d.* FROM door_events d WHERE d.closed_at IS NOT NULL ORDER BY d.id"
    ).fetchall()
    for ev in rows:
        dur_min = minutes_between(ev["opened_at"], ev["closed_at"])
        tag = f"door-event-{ev['id']}"
        if dur_min <= config.DOOR_OPEN_GRACE_MIN:
            continue
        dup = conn.execute(
            "SELECT 1 FROM incidents WHERE kind='door_open' AND detail LIKE ?",
            (f"%{tag}%",)).fetchone()
        if dup:
            continue
        lease = _lease_at(conn, ev["cell_id"], ev["opened_at"])
        if lease is None:
            continue
        cur = conn.execute(
            "INSERT INTO incidents(lease_id,cell_id,kind,ts,start_ts,end_ts,detail,"
            "amount,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (lease["id"], ev["cell_id"], "door_open", ev["opened_at"],
             ev["opened_at"], ev["closed_at"],
             f"门开启 {dur_min:.0f} 分钟（{tag}），超宽限 {config.DOOR_OPEN_GRACE_MIN} 分钟",
             config.DOOR_OPEN_FEE, now_iso()),
        )
        created.append(cur.lastrowid)
    return created


def _lease_at(conn: sqlite3.Connection, cell_id: str, ts: str):
    """找到某时刻占用某格的租期（含已移走的历史区间）。"""
    rows = conn.execute(
        "SELECT * FROM leases WHERE started_at<=? AND (ended_at IS NULL OR ended_at>=?)",
        (ts, ts)).fetchall()
    for lease in rows:
        for cid, a, b, _lo, _hi in occupancy_intervals(conn, lease):
            if cid == cell_id and a <= ts <= b:
                return lease
    return None


def detect_all(conn: sqlite3.Connection) -> list[int]:
    """扫描全部租期（开放与已关闭但未结账的均可补录）。幂等。"""
    created = []
    leases = conn.execute("SELECT * FROM leases").fetchall()
    for lease in leases:
        for cid, a, b, lo, hi in occupancy_intervals(conn, lease):
            created.extend(detect_cell(conn, lease, cid, a, b, lo, hi))
    created.extend(detect_doors(conn))
    return created


# ---- 后台线程 ---------------------------------------------------------------

class Monitor:
    """周期性产生读数并跑检测。"""

    def __init__(self, conn_factory, interval_sec: float = config.MONITOR_INTERVAL_SEC,
                 sample: bool = True):
        self._conn_factory = conn_factory
        self.interval = interval_sec
        self.sample = sample
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "Monitor":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._loop, name="cs-monitor",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def tick(self) -> list[int]:
        conn = self._conn_factory()
        try:
            if self.sample:
                for row in conn.execute(
                        "SELECT DISTINCT cell_id FROM leases WHERE status='open'"):
                    adapters.simulate_reading(conn, row["cell_id"])
            conn.commit()
            return detect_all(conn)
        finally:
            conn.commit()
            conn.close()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as exc:  # 监控线程不能因单次错误退出
                print(f"[monitor] tick failed: {exc!r}")
