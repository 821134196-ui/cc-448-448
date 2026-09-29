"""模拟适配器：产生门磁与温度读数；进程内监测任务计算越界时长与异常留痕。

真实硬件不存在，全部为模拟：
- SimTempAdapter 按库区设定温区生成读数，支持注入“断电升温/临时降温”情景；
- SimDoorAdapter 记录开关门事件；
- MonitorLoop 周期采样，判定温度越界、断电、门未关，并按在租货格分摊异常。
"""

from __future__ import annotations

import math
import random
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

from . import models as M
from .db import connect
from .services import iso, now, open_incident, close_incident, tx

SAMPLING_SECONDS = 60          # 默认采样周期
DOOR_POLL_SECONDS = 30


class SimTempAdapter:
    """温度读数模拟器（每个库区一个）。"""

    def __init__(self, seed: int = 7):
        self._rng = random.Random(seed)
        self._phase = {1: 0.3}
        # 情景窗口：zone_id -> 结束时刻
        self.outage_until: dict[int, datetime] = {}
        self.pulldown_until: dict[int, datetime] = {}

    def read(self, zone: sqlite3.Row, at: datetime) -> float:
        zid = zone["id"]
        target = (zone["temp_min"] + zone["temp_max"]) / 2
        wobble = math.sin(self._phase.setdefault(zid, 0.0)) * 0.4 + self._rng.uniform(-0.15, 0.15)
        self._phase[zid] += 0.7
        if at < self.outage_until.get(zid, at):
            # 断电后温度向环境 18℃ 指数爬升
            remain = (self.outage_until[zid] - at).total_seconds()
            value = 18 - (18 - target) * min(1.0, remain / 600.0) + wobble
            return round(value, 2)
        if at < self.pulldown_until.get(zid, at):
            return round(target - 2.5 + wobble, 2)
        return round(target + wobble, 2)

    def inject_outage(self, zone_id: int, minutes: int, at: datetime | None = None) -> None:
        at = at or now()
        self.outage_until[zone_id] = at + timedelta(minutes=minutes)

    def inject_pulldown(self, zone_id: int, minutes: int, at: datetime | None = None) -> None:
        at = at or now()
        self.pulldown_until[zone_id] = at + timedelta(minutes=minutes)


class SimDoorAdapter:
    """门磁模拟器：成对产生开/关门事件。"""

    def __init__(self):
        self.open_since: dict[int, datetime] = {}

    def open(self, conn: sqlite3.Connection, cell_id: int, at: datetime | None = None,
             note: str = "模拟门磁：开门") -> None:
        at = at or now()
        with tx(conn):
            conn.execute(
                "INSERT INTO door_events(cell_id,kind,event_at,source,note) VALUES(?,?,?,'sim',?)",
                (cell_id, "open", iso(at), note))
        self.open_since[cell_id] = at

    def close(self, conn: sqlite3.Connection, cell_id: int, at: datetime | None = None,
              note: str = "模拟门磁：关门") -> None:
        at = at or now()
        with tx(conn):
            conn.execute(
                "INSERT INTO door_events(cell_id,kind,event_at,source,note) VALUES(?,?,?,'sim',?)",
                (cell_id, "close", iso(at), note))
        self.open_since.pop(cell_id, None)


def active_reservations_in_zone(conn: sqlite3.Connection, zone_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        """SELECT r.* FROM reservations r JOIN cells c ON c.id=r.cell_id
           WHERE c.zone_id=? AND r.status IN (?,?)""",
        (zone_id, M.CHECKED_IN, M.MOVE_PENDING)))


def _open_zone_incidents(conn: sqlite3.Connection, kind: str, zone_id: int,
                         at: datetime, title_fn, detail: str, severity: str):
    """为库区内在租的每笔租约开启一条异常（保证计费能归属到租约）。"""
    ids = []
    for r in active_reservations_in_zone(conn, zone_id):
        ids.append(open_incident(
            conn, kind, title_fn(r), zone_id=zone_id, cell_id=r["cell_id"],
            reservation_id=r["id"], severity=severity, detail=detail, at=at))
    return ids


class MonitorLoop:
    """后台线程：采样、越界/断电/门未关监测。"""

    def __init__(self, db_path: str, interval: int = SAMPLING_SECONDS,
                 temp_adapter: SimTempAdapter | None = None,
                 door_adapter: SimDoorAdapter | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.db_path = db_path
        self.interval = interval
        self.temp = temp_adapter or SimTempAdapter()
        self.door = door_adapter or SimDoorAdapter()
        self._sleep = sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 每库区当前越界/断电/降温中的异常 id 集合
        self.breach_open: dict[int, list[int]] = {}
        self.outage_open: dict[int, list[int]] = {}
        self.pull_open: dict[int, list[int]] = {}

    # ------------------------------------------------ 注入情景
    def inject_outage(self, zone_id: int, minutes: int) -> None:
        self.temp.inject_outage(zone_id, minutes)

    def inject_pulldown(self, zone_id: int, minutes: int) -> None:
        self.temp.inject_pulldown(zone_id, minutes)

    # ------------------------------------------------ 主循环
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.run, name="cold-monitor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                conn = connect(self.db_path)
                try:
                    self.tick(conn)
                finally:
                    conn.close()
            except Exception as exc:  # 监测任务不能因单次异常退出
                print(f"[monitor] tick 失败: {exc}")
            self._stop.wait(self.interval)

    def tick(self, conn: sqlite3.Connection, at: datetime | None = None) -> None:
        at = at or now()
        with tx(conn):
            self._sample_temps(conn, at)
            self._check_power_states(conn, at)
            self._check_doors(conn, at)

    # ------------------------------------------------ 温度
    def _sample_temps(self, conn: sqlite3.Connection, at: datetime) -> None:
        for z in conn.execute("SELECT * FROM zones ORDER BY id"):
            value = self.temp.read(z, at)
            conn.execute(
                "INSERT INTO temp_samples(zone_id,celsius,sampled_at,source) VALUES(?,?,?,'sim')",
                (z["id"], value, iso(at)))

    def _check_power_states(self, conn: sqlite3.Connection, at: datetime) -> None:
        for z in conn.execute("SELECT * FROM zones ORDER BY id"):
            latest = conn.execute(
                "SELECT celsius FROM temp_samples WHERE zone_id=? ORDER BY sampled_at DESC, id DESC LIMIT 1",
                (z["id"],)).fetchone()
            value = latest["celsius"] if latest else (z["temp_min"] + z["temp_max"]) / 2
            outage = at < self.temp.outage_until.get(z["id"], at)
            pull = at < self.temp.pulldown_until.get(z["id"], at)
            breach = value < z["temp_min"] or value > z["temp_max"]

            self._transition(conn, M.INC_OUTAGE, self.outage_open, z, outage, at,
                             lambda r: f"货格租约 #{r['id']} 断电",
                             f"{z['code']} 区断电，温度向环境漂移", "critical")
            self._transition(conn, M.INC_PULL_DOWN, self.pull_open, z, pull, at,
                             lambda r: f"货格租约 #{r['id']} 临时降温",
                             f"{z['code']} 区按运营要求临时下调设定温区", "info")
            # 断电期间的高温不重复记越界
            self._transition(conn, M.INC_BREACH, self.breach_open, z,
                             breach and not outage, at,
                             lambda r: f"货格租约 #{r['id']} 温度越界",
                             f"{z['code']} 区读数 {value}℃ 超出温区 {z['temp_min']}~{z['temp_max']}℃",
                             "critical", metric=value)

    def _transition(self, conn, kind, registry, zone, active, at, title_fn, detail,
                    severity, metric=None):
        open_ids = registry.get(zone["id"], [])
        if active and not open_ids:
            registry[zone["id"]] = _open_zone_incidents(
                conn, kind, zone["id"], at, title_fn, detail, severity)
        elif not active and open_ids:
            for iid in open_ids:
                close_incident(conn, iid, at=at, metric_value=metric)
            registry[zone["id"]] = []

    # ------------------------------------------------ 门磁
    def _check_doors(self, conn: sqlite3.Connection, at: datetime) -> None:
        for cell in conn.execute("SELECT * FROM cells ORDER BY id"):
            last = conn.execute(
                "SELECT * FROM door_events WHERE cell_id=? ORDER BY event_at DESC, id DESC LIMIT 1",
                (cell["id"],)).fetchone()
            open_inc = conn.execute(
                """SELECT * FROM incidents WHERE cell_id=? AND kind=? AND resolved=0
                   ORDER BY id DESC LIMIT 1""",
                (cell["id"], M.INC_DOOR_OPEN)).fetchone()
            if last and last["kind"] == "open" and not open_inc:
                opened = datetime.strptime(last["event_at"], "%Y-%m-%dT%H:%M:%S")
                if (at - opened).total_seconds() > M.DOOR_OPEN_LIMIT_SECONDS:
                    open_incident(
                        conn, M.INC_DOOR_OPEN,
                        f"货格 {cell['code']} 门未关",
                        zone_id=cell["zone_id"], cell_id=cell["id"],
                        reservation_id=cell["active_reservation_id"], severity="warning",
                        detail=f"自 {last['event_at']} 起开门超过 "
                               f"{M.DOOR_OPEN_LIMIT_SECONDS // 60} 分钟", at=opened)
            elif last and last["kind"] == "close" and open_inc:
                close_incident(conn, open_inc["id"], at=at)
