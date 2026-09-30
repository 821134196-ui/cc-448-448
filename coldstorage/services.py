"""领域服务：库区/货格、租户、预约、租期、移格、异常留痕。

关键并发约定：
* 所有"先查后写"的容量判定在单条 ``BEGIN IMMEDIATE`` 事务里完成，
  SQLite 写锁把两笔争抢同一货格的预约串行化，只有一笔能成功。
* 计费只认 lease（入库复核通过后才创建）；预约确认只锁定容量与温区。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from . import config
from .db import connect
from .errors import DomainError
from .util import hours_between, now_iso


# ---- 基础数据 ---------------------------------------------------------------

def seed_catalog(conn: sqlite3.Connection) -> None:
    for code in config.ZONE_ORDER:
        z = config.ZONES[code]
        conn.execute(
            "INSERT OR IGNORE INTO zones(code,name,temp_min,temp_max,target_temp,rate)"
            " VALUES(?,?,?,?,?,?)",
            (code, z["name"], z["temp_min"], z["temp_max"], z["target"], z["rate"]),
        )
    # 每个温区 8 个货格，纵向排列；冷冻区末尾故意做小格子以制造容量争抢
    existing = conn.execute("SELECT COUNT(*) c FROM cells").fetchone()["c"]
    if existing == 0:
        specs = {
            "F":  [6.0] * 7 + [4.0],
            "C":  [8.0] * 8,
            "AC": [10.0] * 8,
        }
        for code, vols in specs.items():
            for i, vol in enumerate(vols, 1):
                conn.execute(
                    "INSERT INTO cells(id,zone_code,volume_m3,seq) VALUES(?,?,?,?)",
                    (f"{code}-{i:02d}", code, vol, i),
                )


def effective_band(conn: sqlite3.Connection, cell_id: str) -> tuple[float, float]:
    """货格当前生效温区带（管理员手工改温覆盖温区默认值）。"""
    row = conn.execute(
        "SELECT c.zone_code, o.temp_min mn, o.temp_max mx FROM cells c"
        " LEFT JOIN cell_temp_overrides o ON o.cell_id=c.id WHERE c.id=?",
        (cell_id,),
    ).fetchone()
    if row is None:
        raise DomainError("货格不存在")
    if row["mn"] is not None:
        return row["mn"], row["mx"]
    z = config.ZONES[row["zone_code"]]
    return z["temp_min"], z["temp_max"]


# ---- 预约 -------------------------------------------------------------------

def _free_cells(conn: sqlite3.Connection, zone_code: str, need_volume: float,
                start_ts: str, end_ts: str, exclude_cell: Optional[str] = None):
    """在立即写事务内挑选满足容量且时段空闲的货格（最佳适配：剩余浪费最少）。"""
    sql = (
        "SELECT c.* FROM cells c WHERE c.zone_code=? AND c.active=1 AND c.volume_m3>=?"
        " AND c.id NOT IN ("
        "  SELECT cell_id FROM reservations"
        "  WHERE status IN ('confirmed','checked_in') AND cell_id IS NOT NULL"
        "   AND start_ts < ? AND end_ts > ?"
        " )"
        " AND c.id NOT IN (SELECT cell_id FROM leases WHERE status='open')"
    )
    params: list = [zone_code, need_volume, end_ts, start_ts]
    if exclude_cell:
        sql += " AND c.id != ?"
        params.append(exclude_cell)
    sql += " ORDER BY c.volume_m3 ASC, c.seq ASC"
    return conn.execute(sql, params).fetchall()


def create_reservation(conn: sqlite3.Connection, tenant_id: str, zone_code: str,
                       need_volume_m3: float, start_ts: str, end_ts: str,
                       note: str = "") -> int:
    if zone_code not in config.ZONES:
        raise DomainError("温区不存在")
    if need_volume_m3 <= 0:
        raise DomainError("需求容量必须大于 0")
    if end_ts <= start_ts:
        raise DomainError("租用时段结束时间必须晚于开始时间")
    if not conn.execute("SELECT 1 FROM tenants WHERE id=?", (tenant_id,)).fetchone():
        raise DomainError("租户不存在")
    z = config.ZONES[zone_code]
    cur = conn.execute(
        "INSERT INTO reservations(tenant_id,zone_code,need_volume_m3,"
        "target_temp_min,target_temp_max,start_ts,end_ts,note,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (tenant_id, zone_code, need_volume_m3, z["temp_min"], z["temp_max"],
         start_ts, end_ts, note, now_iso()),
    )
    return cur.lastrowid


def confirm_reservation(conn: sqlite3.Connection, reservation_id: int) -> dict:
    """确认预约：锁定具体货格与温区。并发争抢时只有一笔成功。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        r = conn.execute("SELECT * FROM reservations WHERE id=?",
                         (reservation_id,)).fetchone()
        if r is None:
            raise DomainError("预约不存在")
        if r["status"] == "confirmed":
            return {"cell_id": r["cell_id"], "idempotent": True}
        if r["status"] != "pending":
            raise DomainError(f"预约状态为 {r['status']}，无法确认")
        free = _free_cells(conn, r["zone_code"], r["need_volume_m3"],
                           r["start_ts"], r["end_ts"])
        if not free:
            raise DomainError("该温区在此时段没有满足容量的空货格")
        cell = free[0]
        # 温区以确认时刻为准快照进预约
        z = config.ZONES[r["zone_code"]]
        conn.execute(
            "UPDATE reservations SET status='confirmed',cell_id=?,"
            "target_temp_min=?,target_temp_max=? WHERE id=?",
            (cell["id"], z["temp_min"], z["temp_max"], reservation_id),
        )
        conn.execute("COMMIT")
        return {"cell_id": cell["id"], "idempotent": False}
    except Exception:
        conn.execute("ROLLBACK")
        raise


def cancel_reservation(conn: sqlite3.Connection, reservation_id: int) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        r = conn.execute("SELECT * FROM reservations WHERE id=?",
                         (reservation_id,)).fetchone()
        if r is None:
            raise DomainError("预约不存在")
        if r["status"] in ("checked_in", "checked_out", "settled"):
            raise DomainError("已入库的预约不能取消")
        conn.execute("UPDATE reservations SET status='cancelled' WHERE id=?",
                     (reservation_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---- 入库复核 ---------------------------------------------------------------

def add_batch(conn: sqlite3.Connection, reservation_id: int, name: str,
              declared_weight_kg: Optional[float], actual_weight_kg: Optional[float],
              volume_m3: Optional[float], photo_path: Optional[str] = None,
              inbound_note: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO batches(reservation_id,name,declared_weight_kg,actual_weight_kg,"
        "volume_m3,photo_path,inbound_note,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (reservation_id, name, declared_weight_kg, actual_weight_kg, volume_m3,
         photo_path, inbound_note, now_iso()),
    )
    return cur.lastrowid


def check_in(conn: sqlite3.Connection, reservation_id: int, passed: bool,
             reviewer_note: str = "") -> dict:
    """入库复核。通过 → 创建 lease，此刻才开始计费；不通过 → 不放行、不计费。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        r = conn.execute("SELECT * FROM reservations WHERE id=?",
                         (reservation_id,)).fetchone()
        if r is None:
            raise DomainError("预约不存在")
        if not passed:
            conn.execute(
                "UPDATE reservations SET note=COALESCE(note,'')||? WHERE id=?",
                (f"\n[复核未通过 {now_iso()}] {reviewer_note}", reservation_id),
            )
            conn.execute("COMMIT")
            return {"passed": False}
        if r["status"] != "confirmed":
            raise DomainError(f"预约状态为 {r['status']}，无法入库复核")
        batches = conn.execute(
            "SELECT * FROM batches WHERE reservation_id=?", (reservation_id,)
        ).fetchall()
        weighed = [b for b in batches if b["actual_weight_kg"] is not None
                   and b["volume_m3"] is not None]
        if not weighed:
            raise DomainError("至少需要一个批次完成实称重量与体积登记才能复核通过")
        total_w = round(sum(b["actual_weight_kg"] for b in weighed), 3)
        total_v = round(sum(b["volume_m3"] for b in weighed), 3)

        cell_id = r["cell_id"]
        cell = conn.execute("SELECT * FROM cells WHERE id=?", (cell_id,)).fetchone()
        over = round(max(0.0, total_v - cell["volume_m3"]), 3)

        cur = conn.execute(
            "INSERT INTO leases(reservation_id,cell_id,zone_code,volume_m3,weight_kg,"
            "temp_min,temp_max,started_at,status,overcapacity_m3)"
            " VALUES(?,?,?,?,?,?,?,?,'open',?)",
            (reservation_id, cell_id, r["zone_code"], cell["volume_m3"], total_w,
             r["target_temp_min"], r["target_temp_max"], now_iso(), over),
        )
        lease_id = cur.lastrowid
        conn.execute("UPDATE reservations SET status='checked_in' WHERE id=?",
                     (reservation_id,))
        if over > 0:
            conn.execute(
                "INSERT INTO incidents(lease_id,cell_id,kind,ts,detail,amount,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (lease_id, cell_id, "overcapacity", now_iso(),
                 f"实测体积 {total_v}m³ 超出货格容积 {cell['volume_m3']}m³，超出 {over}m³",
                 round(over * config.OVERCAPACITY_FEE_PER_M3, 2), now_iso()),
            )
        conn.execute("COMMIT")
        return {"passed": True, "lease_id": lease_id, "cell_id": cell_id,
                "weight_kg": total_w, "volume_m3": total_v, "overcapacity_m3": over}
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---- 管理员改温 / 临时降温 ----------------------------------------------------

def override_temp(conn: sqlite3.Connection, cell_id: str, new_min: float,
                  new_max: float, reason: str, by_user: str) -> int:
    if not reason or not reason.strip():
        raise DomainError("手工改温必须填写原因")
    if new_min >= new_max:
        raise DomainError("温区下限必须低于上限")
    cell = conn.execute("SELECT * FROM cells WHERE id=?", (cell_id,)).fetchone()
    if cell is None:
        raise DomainError("货格不存在")
    z = config.ZONES[cell["zone_code"]]
    cur = conn.execute(
        "INSERT INTO temp_overrides(cell_id,old_min,old_max,new_min,new_max,reason,"
        "by_user,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (cell_id, z["temp_min"], z["temp_max"], new_min, new_max, reason,
         by_user, now_iso()),
    )
    oid = cur.lastrowid
    conn.execute(
        "INSERT INTO cell_temp_overrides(cell_id,temp_min,temp_max,override_id)"
        " VALUES(?,?,?,?) ON CONFLICT(cell_id) DO UPDATE SET"
        " temp_min=excluded.temp_min,temp_max=excluded.temp_max,"
        " override_id=excluded.override_id",
        (cell_id, new_min, new_max, oid),
    )
    return oid


def temp_pull(conn: sqlite3.Connection, lease_id: int, hours: float,
              by_user: str, note: str = "") -> int:
    """临时降温：按小时加收，留痕 temp_pull（正金额）。"""
    if hours <= 0:
        raise DomainError("降温时长必须大于 0")
    lease = conn.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
    if lease is None or lease["status"] != "open":
        raise DomainError("租期不存在或已结束")
    amount = round(hours * config.TEMP_PULL_RATE_PER_HOUR, 2)
    cur = conn.execute(
        "INSERT INTO incidents(lease_id,cell_id,kind,ts,start_ts,end_ts,detail,"
        "amount,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (lease_id, lease["cell_id"], "temp_pull", now_iso(), now_iso(), None,
         f"{by_user} 登记临时降温 {hours}h。{note}", amount, now_iso()),
    )
    return cur.lastrowid


def record_leak(conn: sqlite3.Connection, lease_id: int, detail: str,
                by_user: str) -> int:
    lease = conn.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
    if lease is None:
        raise DomainError("租期不存在")
    if lease["status"] != "open":
        raise DomainError("租期已结账，不能补登异常")
    cur = conn.execute(
        "INSERT INTO incidents(lease_id,cell_id,kind,ts,detail,amount,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (lease_id, lease["cell_id"], "leak", now_iso(),
         f"{by_user} 登记：{detail}", config.LEAK_FEE, now_iso()),
    )
    return cur.lastrowid


# ---- 移格 -------------------------------------------------------------------

def request_move(conn: sqlite3.Connection, lease_id: int, to_cell: str,
                 reason: str) -> dict:
    """同温区空格 → 直接移位；跨温区 → 申请待管理员重新确认。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        lease = conn.execute("SELECT * FROM leases WHERE id=?",
                             (lease_id,)).fetchone()
        if lease is None:
            raise DomainError("租期不存在")
        if lease["status"] != "open":
            raise DomainError("租期已结束，不能移格")
        target = conn.execute("SELECT * FROM cells WHERE id=?",
                              (to_cell,)).fetchone()
        if target is None:
            raise DomainError("目标货格不存在")
        if target["id"] == lease["cell_id"]:
            raise DomainError("目标货格与当前货格相同")
        busy = conn.execute(
            "SELECT 1 FROM leases WHERE cell_id=? AND status='open'", (to_cell,)
        ).fetchone()
        if busy:
            raise DomainError("目标货格正被占用")
        if target["volume_m3"] < lease["volume_m3"]:
            raise DomainError(
                f"目标货格容积 {target['volume_m3']}m³ 小于当前锁定容量 "
                f"{lease['volume_m3']}m³")
        same_zone = target["zone_code"] == lease["zone_code"]
        cur = conn.execute(
            "INSERT INTO moves(lease_id,from_cell,to_cell,same_zone,reason,status,"
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (lease_id, lease["cell_id"], to_cell, 1 if same_zone else 0, reason,
             "requested", now_iso()),
        )
        move_id = cur.lastrowid
        executed = False
        if same_zone:
            _execute_move_locked(conn, move_id, by="system-auto")
            executed = True
        conn.execute("COMMIT")
        return {"move_id": move_id, "same_zone": bool(same_zone),
                "executed": executed}
    except Exception:
        conn.execute("ROLLBACK")
        raise


def decide_move(conn: sqlite3.Connection, move_id: int, approve: bool,
                by_user: str) -> None:
    """跨温区移格必须管理员重新确认。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        m = conn.execute("SELECT * FROM moves WHERE id=?", (move_id,)).fetchone()
        if m is None:
            raise DomainError("移格申请不存在")
        if m["status"] != "requested":
            raise DomainError(f"申请状态为 {m['status']}")
        if m["same_zone"]:
            raise DomainError("同温区移格无需审批")
        if approve:
            target = conn.execute("SELECT * FROM cells WHERE id=?",
                                  (m["to_cell"],)).fetchone()
            busy = conn.execute(
                "SELECT 1 FROM leases WHERE cell_id=? AND status='open'",
                (m["to_cell"],)).fetchone()
            if busy:
                raise DomainError("目标货格已被占用")
            conn.execute(
                "UPDATE moves SET status='approved',decided_by=?,decided_at=?",
                (by_user, now_iso()),
            )
            _execute_move_locked(conn, move_id, by=by_user)
            # 跨温区重新确认温区：lease 的计费温区带更新为目标温区
            z = config.ZONES[target["zone_code"]]
            conn.execute(
                "UPDATE leases SET zone_code=?,temp_min=?,temp_max=? WHERE id=?",
                (target["zone_code"], z["temp_min"], z["temp_max"], m["lease_id"]),
            )
            conn.execute(
                "UPDATE reservations SET zone_code=?,target_temp_min=?,"
                "target_temp_max=? WHERE id=(SELECT reservation_id FROM leases WHERE id=?)",
                (target["zone_code"], z["temp_min"], z["temp_max"], m["lease_id"]),
            )
        else:
            conn.execute(
                "UPDATE moves SET status='rejected',decided_by=?,decided_at=?",
                (by_user, now_iso()),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _execute_move_locked(conn: sqlite3.Connection, move_id: int, by: str) -> None:
    m = conn.execute("SELECT * FROM moves WHERE id=?", (move_id,)).fetchone()
    conn.execute("UPDATE leases SET cell_id=? WHERE id=?",
                 (m["to_cell"], m["lease_id"]))
    conn.execute(
        "UPDATE moves SET status='done',done_at=?,decided_by=COALESCE(decided_by,?)",
        (now_iso(), by),
    )


# ---- 退库 -------------------------------------------------------------------

def early_exit_hours(conn: sqlite3.Connection, lease: sqlite3.Row,
                     end_ts: str) -> float:
    r = conn.execute("SELECT * FROM reservations WHERE id=?",
                     (lease["reservation_id"],)).fetchone()
    return max(0.0, hours_between(end_ts, r["end_ts"]))


def check_out(conn: sqlite3.Connection, lease_id: int, by_user: str) -> dict:
    """退库清点结束 → 关闭租期并生成费用快照（与 billing 在同一事务，幂等）。"""
    from .billing import build_snapshot, persist_bill  # 避免循环导入
    from .monitor import detect_all  # 结算前先补齐越界/断电/门未关留痕

    conn.execute("BEGIN IMMEDIATE")
    try:
        lease = conn.execute("SELECT * FROM leases WHERE id=?",
                             (lease_id,)).fetchone()
        if lease is None:
            raise DomainError("租期不存在")
        existing = conn.execute("SELECT id FROM bills WHERE lease_id=?",
                                (lease_id,)).fetchone()
        if existing is not None:
            # 已结账租期不可重新打开：重复退库直接返回原账单
            return {"lease_id": lease_id, "bill_id": existing["id"],
                    "idempotent": True}
        if lease["status"] != "open":
            raise DomainError("租期已关闭且无账单（数据异常）")
        end_ts = now_iso()

        # 结算前补齐监控留痕（后台节拍可能尚未扫描到最新读数）
        detect_all(conn)

        # 提前退租留痕（相对预约结束时间提前）
        remaining = early_exit_hours(conn, lease, end_ts)
        if remaining > 0:
            z = config.ZONES[lease["zone_code"]]
            penalty = round(
                z["rate"] * lease["volume_m3"] * remaining
                * config.EARLY_EXIT_PENALTY_RATE, 2)
            conn.execute(
                "INSERT INTO incidents(lease_id,cell_id,kind,ts,start_ts,detail,"
                "amount,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (lease_id, lease["cell_id"], "early_exit", end_ts, end_ts,
                 f"较预约时段提前 {remaining:.1f} 小时退租，按 {int(config.EARLY_EXIT_PENALTY_RATE*100)}% 计违约金",
                 penalty, now_iso()),
            )

        snapshot = build_snapshot(conn, lease, end_ts)
        bill_id = persist_bill(conn, lease, snapshot, end_ts)
        conn.execute("UPDATE leases SET status='closed',ended_at=? WHERE id=?",
                     (end_ts, lease_id))
        conn.execute("UPDATE reservations SET status='checked_out' WHERE id=?",
                     (lease["reservation_id"],))
        conn.execute("COMMIT")
        return {"lease_id": lease_id, "bill_id": bill_id, "idempotent": False,
                "total": snapshot["total"]}
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---- 读取模型 ---------------------------------------------------------------

def cell_map(conn: sqlite3.Connection) -> list[dict]:
    """库区格位图：按库区纵向分组，含剩余容积与占用状态。"""
    zones_rows = conn.execute("SELECT * FROM zones ORDER BY CASE code "
                              "WHEN 'F' THEN 0 WHEN 'C' THEN 1 ELSE 2 END").fetchall()
    out = []
    for z in zones_rows:
        cells = []
        for c in conn.execute("SELECT * FROM cells WHERE zone_code=? ORDER BY seq",
                              (z["code"],)).fetchall():
            ov = conn.execute(
                "SELECT temp_min,temp_max FROM cell_temp_overrides WHERE cell_id=?",
                (c["id"],)).fetchone()
            lease = conn.execute(
                "SELECT l.id, t.name tenant_name FROM leases l"
                " JOIN reservations r ON r.id=l.reservation_id"
                " JOIN tenants t ON t.id=r.tenant_id"
                " WHERE l.cell_id=? AND l.status='open'", (c["id"],)).fetchone()
            conf = conn.execute(
                "SELECT r.id,t.name tenant_name FROM reservations r"
                " JOIN tenants t ON t.id=r.tenant_id"
                " WHERE r.cell_id=? AND r.status='confirmed'", (c["id"],)).fetchone()
            if lease:
                status, who, rid = "occupied", lease["tenant_name"], lease["id"]
            elif conf:
                status, who, rid = "reserved", conf["tenant_name"], conf["id"]
            else:
                status, who, rid = "free", None, None
            cells.append({
                "id": c["id"], "volume_m3": c["volume_m3"], "seq": c["seq"],
                "status": status, "who": who, "lease_id": rid,
                "remaining_m3": 0.0 if status != "free" else c["volume_m3"],
                "band_override": [ov["temp_min"], ov["temp_max"]] if ov else None,
            })
        occupied_vol = sum(c["volume_m3"] for c in cells if c["status"] != "free")
        out.append({
            "code": z["code"], "name": z["name"],
            "temp_min": z["temp_min"], "temp_max": z["temp_max"],
            "rate": z["rate"],
            "total_m3": round(sum(c["volume_m3"] for c in cells), 2),
            "remaining_m3": round(sum(c["remaining_m3"] for c in cells), 2),
            "occupied_m3": round(occupied_vol, 2),
            "cells": cells,
        })
    return out
