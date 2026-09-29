"""领域服务：预约锁定、入库复核、移格、异常留痕、费用快照与结账。

所有金额单位为元（虚构费率见 models.py），时间为 ISO8601 本地时间字符串。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Iterable

from . import models as M
from .db import tx

NOW_FMT = "%Y-%m-%dT%H:%M:%S"


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.strftime(NOW_FMT)


def parse(s: str) -> datetime:
    return datetime.strptime(s, NOW_FMT)


# ---------------------------------------------------------------- 基础查询

def get(conn: sqlite3.Connection, table: str, row_id: int) -> sqlite3.Row | None:
    return conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()


def require(conn: sqlite3.Connection, table: str, row_id: int) -> sqlite3.Row:
    row = get(conn, table, row_id)
    if row is None:
        raise M.PlatformError(f"{table}#{row_id} 不存在")
    return row


def audit(conn: sqlite3.Connection, actor: str, action: str, target: str = "", detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log(actor,action,target,detail,created_at) VALUES(?,?,?,?,?)",
        (actor, action, target, detail, iso(now())),
    )


def list_zones(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM zones ORDER BY id"))


def list_cells(conn: sqlite3.Connection, zone_id: int | None = None) -> list[sqlite3.Row]:
    if zone_id is None:
        return list(conn.execute("SELECT * FROM cells ORDER BY code"))
    return list(conn.execute("SELECT * FROM cells WHERE zone_id=? ORDER BY code", (zone_id,)))


def zone_cells_with_remaining(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """库区格位图数据：按库区纵向分组，含每个货格剩余容积。"""
    groups: list[dict[str, Any]] = []
    for z in list_zones(conn):
        cells = []
        for c in list_cells(conn, z["id"]):
            cells.append({
                "row": c,
                "remaining_m3": round(c["capacity_m3"] - c["occupied_m3"], 3),
            })
        total = sum(c["row"]["capacity_m3"] for c in cells)
        used = sum(c["row"]["occupied_m3"] for c in cells)
        groups.append({
            "zone": z,
            "cells": cells,
            "total_m3": round(total, 3),
            "used_m3": round(used, 3),
            "remaining_m3": round(total - used, 3),
        })
    return groups


# ---------------------------------------------------------------- 租户/货品

def create_tenant(conn: sqlite3.Connection, name: str, login: str, token: str) -> int:
    cur = conn.execute(
        "INSERT INTO tenants(name,login,token) VALUES(?,?,?)", (name, login, token)
    )
    return cur.lastrowid


def create_batch(conn: sqlite3.Connection, tenant_id: int, name: str, sku: str) -> int:
    cur = conn.execute(
        "INSERT INTO batches(tenant_id,name,sku,created_at) VALUES(?,?,?,?)",
        (tenant_id, name, sku, iso(now())),
    )
    return cur.lastrowid


def tenant_batches(conn: sqlite3.Connection, tenant_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM batches WHERE tenant_id=? ORDER BY id DESC", (tenant_id,)
    ))


def add_photo(conn: sqlite3.Connection, batch_id: int, path: str, label: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO batch_photos(batch_id,path,label,created_at) VALUES(?,?,?,?)",
        (batch_id, path, label, iso(now())),
    )
    return cur.lastrowid


# ---------------------------------------------------------------- 预约申请

def create_reservation(
    conn: sqlite3.Connection,
    tenant_id: int,
    batch_id: int,
    target_temp_min: float,
    target_temp_max: float,
    planned_start: datetime,
    planned_end: datetime,
    declared_volume_m3: float,
    declared_weight_kg: float,
    note: str = "",
) -> int:
    if target_temp_min >= target_temp_max:
        raise M.PlatformError("目标温区下限必须小于上限")
    if planned_end <= planned_start:
        raise M.PlatformError("租用结束时间必须晚于开始时间")
    if declared_volume_m3 <= 0 or declared_weight_kg <= 0:
        raise M.PlatformError("申报容积与重量必须为正数")
    batch = require(conn, "batches", batch_id)
    if batch["tenant_id"] != tenant_id:
        raise M.PermissionDenied("不能为他人货品创建预约")

    cur = conn.execute(
        """INSERT INTO reservations(tenant_id,batch_id,status,target_temp_min,target_temp_max,
               planned_start,planned_end,declared_volume_m3,declared_weight_kg,note,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tenant_id, batch_id, M.PENDING, target_temp_min, target_temp_max,
         iso(planned_start), iso(planned_end), declared_volume_m3, declared_weight_kg,
         note, iso(now()), iso(now())),
    )
    return cur.lastrowid


def _compatible_zone(conn: sqlite3.Connection, tmin: float, tmax: float) -> list[sqlite3.Row]:
    """库区温区能容纳目标温区（库区上下限包住目标区间）。"""
    return list(conn.execute(
        "SELECT * FROM zones WHERE temp_min<=? AND temp_max>=? ORDER BY "
        "hourly_rate, kg_hour_rate, id",
        (tmin, tmax),
    ))


def confirm_reservation(conn: sqlite3.Connection, reservation_id: int, actor: str = "admin") -> int:
    """确认预约：在兼容温区中锁定一个容量足够的货格。

    两笔预约争抢同一（最后一个）货格时，使用 BEGIN IMMEDIATE 串行化：
    后到者看不到空余容量，抛 CellCapacityError，仅一笔成功。
    """
    with tx(conn):
        r = require(conn, "reservations", reservation_id)
        if r["status"] != M.PENDING:
            raise M.PlatformError(f"预约当前状态为 {r['status']}，不能确认")
        zones = _compatible_zone(conn, r["target_temp_min"], r["target_temp_max"])
        if not zones:
            raise M.ZoneMismatchError("没有能覆盖目标温区的库区")
        need = r["declared_volume_m3"]
        cell = conn.execute(
            """SELECT * FROM cells WHERE zone_id IN (%s)
                 AND active_reservation_id IS NULL
                 AND capacity_m3 - occupied_m3 >= ?
               ORDER BY capacity_m3, code LIMIT 1""" % ",".join(str(z["id"]) for z in zones),
            (need,),
        ).fetchone()
        if cell is None:
            audit(conn, actor, "confirm_failed", f"reservation:{reservation_id}",
                  f"目标温区 {r['target_temp_min']}~{r['target_temp_max']}℃ 无空余货格")
            raise M.CellCapacityError("兼容温区已无容量足够的货格，预约未能锁定")

        conn.execute(
            "UPDATE cells SET occupied_m3=occupied_m3+?, active_reservation_id=? WHERE id=?",
            (need, reservation_id, cell["id"]),
        )
        conn.execute(
            "UPDATE reservations SET status=?, cell_id=?, updated_at=? WHERE id=?",
            (M.CONFIRMED, cell["id"], iso(now()), reservation_id),
        )
        audit(conn, actor, "reservation_confirmed", f"reservation:{reservation_id}",
              f"锁定货格 {cell['code']}，容积 {need} m³，温区 "
              f"{r['target_temp_min']}~{r['target_temp_max']}℃")
        return cell["id"]


def reject_reservation(conn: sqlite3.Connection, reservation_id: int, reason: str, actor: str = "admin") -> None:
    with tx(conn):
        r = require(conn, "reservations", reservation_id)
        if r["status"] != M.PENDING:
            raise M.PlatformError("只有待确认预约可以拒绝")
        conn.execute("UPDATE reservations SET status=?, updated_at=? WHERE id=?",
                     (M.REJECTED, iso(now()), reservation_id))
        audit(conn, actor, "reservation_rejected", f"reservation:{reservation_id}", reason)


def cancel_reservation(conn: sqlite3.Connection, reservation_id: int, actor: str = "tenant") -> None:
    with tx(conn):
        r = require(conn, "reservations", reservation_id)
        if r["status"] in (M.SETTLED, M.CHECKED_OUT):
            raise M.PlatformError("已退库/已结账预约不能取消")
        _release_cell(conn, r)
        conn.execute("UPDATE reservations SET status=?, cell_id=NULL, updated_at=? WHERE id=?",
                     (M.CANCELLED, iso(now()), reservation_id))
        audit(conn, actor, "reservation_cancelled", f"reservation:{reservation_id}", "")


def _release_cell(conn: sqlite3.Connection, r: sqlite3.Row) -> None:
    if r["cell_id"] is None:
        return
    conn.execute(
        "UPDATE cells SET occupied_m3=MAX(0, occupied_m3-?), active_reservation_id=NULL "
        "WHERE id=? AND active_reservation_id=?",
        (r["declared_volume_m3"], r["cell_id"], r["id"]),
    )


# ---------------------------------------------------------------- 入库复核

def check_in(
    conn: sqlite3.Connection,
    reservation_id: int,
    checked_weight_kg: float,
    checked_volume_m3: float,
    actor: str = "staff",
    at: datetime | None = None,
) -> None:
    """入库复核：复核通过才开始计费（actual_start）。超容在此留痕。"""
    at = at or now()
    with tx(conn):
        r = require(conn, "reservations", reservation_id)
        if r["status"] != M.CONFIRMED:
            raise M.PlatformError("只有已锁定货格的预约可以办理入库复核")
        if checked_weight_kg <= 0 or checked_volume_m3 <= 0:
            raise M.PlatformError("复核重量/容积必须为正数")
        cell = require(conn, "cells", r["cell_id"])

        conn.execute(
            "UPDATE reservations SET status=?, checked_weight_kg=?, actual_start=?, updated_at=? WHERE id=?",
            (M.CHECKED_IN, checked_weight_kg, iso(at), iso(at), reservation_id),
        )
        overload = checked_volume_m3 - cell["capacity_m3"]
        detail = (f"申报 {r['declared_volume_m3']} m³ / 复核 {checked_volume_m3} m³，"
                  f"货格容量 {cell['capacity_m3']} m³")
        if overload > 0:
            # 超容占用：更新货格占用并开启超容异常（持续整个租期，退库时关闭）
            conn.execute(
                "UPDATE cells SET occupied_m3=? WHERE id=?",
                (cell["capacity_m3"] + overload, cell["id"]),
            )
            conn.execute(
                """INSERT INTO incidents(kind,zone_id,cell_id,reservation_id,severity,title,detail,
                         metric_value,started_at,resolved)
                   VALUES(?,?,?,?,?,?,?,?,?,0)""",
                (M.INC_OVERLOAD, cell["zone_id"], cell["id"], reservation_id, "warning",
                 f"货格 {cell['code']} 入库超容", detail, round(overload, 3), iso(at)),
            )
        audit(conn, actor, "check_in", f"reservation:{reservation_id}",
              f"复核重量 {checked_weight_kg} kg；{detail}；计费起点 {iso(at)}")


# ---------------------------------------------------------------- 移格

def request_move(
    conn: sqlite3.Connection,
    reservation_id: int,
    to_cell_id: int,
    reason: str = "",
    actor: str = "tenant",
) -> tuple[bool, int]:
    """移位。返回 (是否同温区直接完成, 移格单id)。

    同温区空格之间可直接移位；跨温区必须管理员重新确认。
    """
    with tx(conn):
        r = require(conn, "reservations", reservation_id)
        if r["status"] != M.CHECKED_IN:
            raise M.PlatformError("只有计费中的租约可以移位")
        if to_cell_id == r["cell_id"]:
            raise M.PlatformError("目标货格与当前货格相同")
        target = require(conn, "cells", to_cell_id)
        if target["active_reservation_id"] is not None:
            raise M.CellCapacityError("目标货格不是空格")
        source = require(conn, "cells", r["cell_id"])
        cross = int(target["zone_id"] != source["zone_id"])

        cur = conn.execute(
            """INSERT INTO move_requests(reservation_id,from_cell_id,to_cell_id,cross_zone,
                   status,reason,created_at) VALUES(?,?,?,?,?,?,?)""",
            (reservation_id, r["cell_id"], to_cell_id, cross,
             M.MR_REQUESTED, reason, iso(now())),
        )
        move_id = cur.lastrowid
        if not cross:
            _apply_move(conn, r, source, target, move_id, actor)
            return True, move_id
        conn.execute("UPDATE reservations SET status=?, updated_at=? WHERE id=?",
                     (M.MOVE_PENDING, iso(now()), reservation_id))
        audit(conn, actor, "move_requested", f"reservation:{reservation_id}",
              f"{source['code']} → {target['code']}（跨温区，待重新确认）。{reason}")
        return False, move_id


def _apply_move(conn, r, source: sqlite3.Row, target: sqlite3.Row, move_id: int, actor: str,
                at: datetime | None = None) -> None:
    at = at or now()
    used = conn.execute(
        "SELECT occupied_m3 FROM cells WHERE id=?", (source["id"],)
    ).fetchone()["occupied_m3"]
    # 原格释放（按当前占用，可能含超容部分），新格占用相同体积
    conn.execute("UPDATE cells SET occupied_m3=0, active_reservation_id=NULL WHERE id=?",
                 (source["id"],))
    conn.execute("UPDATE cells SET occupied_m3=?, active_reservation_id=? WHERE id=?",
                 (used, r["id"], target["id"]))
    conn.execute("UPDATE reservations SET cell_id=?, updated_at=? WHERE id=?",
                 (target["id"], iso(at), r["id"]))
    conn.execute(
        "UPDATE move_requests SET status=?, decided_at=?, decided_by=NULL WHERE id=?",
        (M.MR_APPROVED, iso(at), move_id),
    )
    audit(conn, actor, "move_done", f"reservation:{r['id']}",
          f"{source['code']} → {target['code']}，移格单 #{move_id}")


def decide_move(conn: sqlite3.Connection, move_id: int, approve: bool, actor: str = "admin",
                staff_id: int | None = None) -> None:
    """跨温区移格重新确认。"""
    with tx(conn):
        m = require(conn, "move_requests", move_id)
        if m["status"] != M.MR_REQUESTED:
            raise M.PlatformError("该移格申请已处理")
        r = require(conn, "reservations", m["reservation_id"])
        if r["status"] != M.MOVE_PENDING:
            raise M.PlatformError("租约当前不在待移格状态")
        if approve:
            source = require(conn, "cells", m["from_cell_id"])
            target = require(conn, "cells", m["to_cell_id"])
            if target["active_reservation_id"] is not None:
                raise M.CellCapacityError("目标货格已被占用")
            _apply_move(conn, r, source, target, move_id, actor)
            conn.execute("UPDATE reservations SET status=?, updated_at=? WHERE id=?",
                         (M.CHECKED_IN, iso(now()), r["id"]))
            conn.execute("UPDATE move_requests SET decided_by=? WHERE id=?", (staff_id, move_id))
            audit(conn, actor, "move_confirmed_cross_zone", f"reservation:{r['id']}",
                  f"跨温区移格 #{move_id} 重新确认通过")
        else:
            conn.execute(
                "UPDATE move_requests SET status=?, decided_at=?, decided_by=? WHERE id=?",
                (M.MR_REJECTED, iso(now()), staff_id, move_id),
            )
            conn.execute("UPDATE reservations SET status=?, updated_at=? WHERE id=?",
                         (M.CHECKED_IN, iso(now()), r["id"]))
            audit(conn, actor, "move_rejected", f"reservation:{r['id']}",
                  f"跨温区移格 #{move_id} 被拒绝，留在原格")


# ---------------------------------------------------------------- 异常留痕

def open_incident(conn: sqlite3.Connection, kind: str, title: str, *,
                  zone_id: int | None = None, cell_id: int | None = None,
                  reservation_id: int | None = None, severity: str = "warning",
                  detail: str = "", metric_value: float | None = None,
                  at: datetime | None = None) -> int:
    at = at or now()
    cur = conn.execute(
        """INSERT INTO incidents(kind,zone_id,cell_id,reservation_id,severity,title,detail,
               metric_value,started_at,resolved) VALUES(?,?,?,?,?,?,?,?,?,0)""",
        (kind, zone_id, cell_id, reservation_id, severity, title, detail, metric_value, iso(at)),
    )
    return cur.lastrowid


def close_incident(conn: sqlite3.Connection, incident_id: int, at: datetime | None = None,
                   metric_value: float | None = None) -> None:
    at = at or now()
    if metric_value is None:
        conn.execute("UPDATE incidents SET resolved=1, ended_at=? WHERE id=?", (iso(at), incident_id))
    else:
        conn.execute("UPDATE incidents SET resolved=1, ended_at=?, metric_value=? WHERE id=?",
                     (iso(at), metric_value, incident_id))


def log_leak(conn: sqlite3.Connection, cell_id: int, detail: str, actor: str = "staff") -> int:
    cell = require(conn, "cells", cell_id)
    r = conn.execute("SELECT * FROM reservations WHERE id=?",
                     (cell["active_reservation_id"],)).fetchone() if cell["active_reservation_id"] else None
    with tx(conn):
        inc = open_incident(
            conn, M.INC_LEAK, f"货格 {cell['code']} 货品渗漏",
            zone_id=cell["zone_id"], cell_id=cell_id,
            reservation_id=r["id"] if r else None, severity="critical", detail=detail)
        audit(conn, actor, "leak_logged", f"cell:{cell_id}", detail)
        return inc


def manual_temp_change(conn: sqlite3.Connection, zone_id: int,
                       new_min: float, new_max: float, reason: str, actor: str = "admin") -> None:
    """管理员手工改温：必须说明原因，留痕并审计。"""
    if not reason.strip():
        raise M.PlatformError("手工改温必须填写原因")
    if new_min >= new_max:
        raise M.PlatformError("温区下限必须小于上限")
    with tx(conn):
        z = require(conn, "zones", zone_id)
        conn.execute("UPDATE zones SET temp_min=?, temp_max=? WHERE id=?",
                     (new_min, new_max, zone_id))
        open_incident(conn, M.INC_MANUAL_TEMP,
                      f"库区 {z['code']} 手工改温：{z['temp_min']}~{z['temp_max']}℃ → {new_min}~{new_max}℃",
                      zone_id=zone_id, severity="info",
                      detail=f"原因：{reason}", metric_value=new_max - new_min)
        audit(conn, actor, "manual_temp_change", f"zone:{zone_id}",
              f"{z['temp_min']}~{z['temp_max']} → {new_min}~{new_max}；{reason}")


# ---------------------------------------------------------------- 退库清点

def check_out(conn: sqlite3.Connection, reservation_id: int, actor: str = "staff",
              at: datetime | None = None) -> None:
    """退库清点结束：actual_end 计费终点，提前退租留痕，并生成费用快照。"""
    at = at or now()
    with tx(conn):
        r = require(conn, "reservations", reservation_id)
        if r["status"] != M.CHECKED_IN:
            raise M.PlatformError("只有计费中的租约可以退库清点")
        early = parse(r["planned_end"]) - at
        conn.execute("UPDATE reservations SET status=?, actual_end=?, updated_at=? WHERE id=?",
                     (M.CHECKED_OUT, iso(at), iso(at), reservation_id))

        # 关闭该租期所有未结异常（超容、越界、门未关等）
        for inc in conn.execute(
            "SELECT * FROM incidents WHERE reservation_id=? AND resolved=0", (reservation_id,)
        ):
            close_incident(conn, inc["id"], at=at)

        if early > timedelta(seconds=M.EARLY_GRACE_SECONDS):
            open_incident(conn, M.INC_EARLY, f"预约 #{reservation_id} 提前退租",
                          cell_id=r["cell_id"], reservation_id=reservation_id,
                          severity="info", at=at,
                          detail=f"较计划结束提前 {M.hours(early.total_seconds())} 小时",
                          metric_value=M.hours(early.total_seconds()))
            close_incident(conn, conn.execute(
                "SELECT id FROM incidents WHERE reservation_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                (reservation_id, M.INC_EARLY)).fetchone()["id"], at=at)

        _release_cell(conn, r)
        audit(conn, actor, "check_out", f"reservation:{reservation_id}",
              f"计费终点 {iso(at)}，货格已释放")

    build_snapshot(conn, reservation_id)


# ---------------------------------------------------------------- 费用快照

def _rate_spans(conn: sqlite3.Connection, r: sqlite3.Row) -> list[tuple[datetime, datetime, sqlite3.Row]]:
    """计费时段沿时间轴拆分：跨温区移格以确认时刻为界，各段适用当时所在库区费率。

    依据已批准的跨区移格链重建轨迹（from_cell → to_cell），不依赖当前货格，
    因而即使租约最终停在别的温区，早期区间仍按移动前库区计费。
    """
    start, end = parse(r["actual_start"]), parse(r["actual_end"])
    moves = list(conn.execute(
        """SELECT * FROM move_requests WHERE reservation_id=? AND status=? AND cross_zone=1
               AND decided_at IS NOT NULL ORDER BY decided_at, id""",
        (r["id"], M.MR_APPROVED)))

    # 从移动链末端反推计费起点所在货格
    first_cell = r["cell_id"]
    if moves:
        first_cell = moves[0]["from_cell_id"]
    cuts: list[tuple[datetime, int]] = [(start, first_cell)]
    for m in moves:
        t = parse(m["decided_at"])
        if start < t < end:
            cuts.append((t, m["to_cell_id"]))
    spans = []
    for i, (t0, cell_id) in enumerate(cuts):
        t1 = cuts[i + 1][0] if i + 1 < len(cuts) else end
        if t1 <= t0:
            continue
        cell = require(conn, "cells", cell_id)
        zone = require(conn, "zones", cell["zone_id"])
        spans.append((t0, t1, zone))
    return spans


def _incident_windows(conn: sqlite3.Connection, r: sqlite3.Row,
                      kinds: Iterable[str]) -> list[tuple[datetime, datetime, str, sqlite3.Row]]:
    start, end = parse(r["actual_start"]), parse(r["actual_end"])
    windows = []
    for inc in conn.execute(
        f"""SELECT * FROM incidents WHERE reservation_id=? AND kind IN
            ({','.join('?' for _ in kinds)}) ORDER BY started_at""",
        (r["id"], *kinds),
    ):
        s = max(parse(inc["started_at"]), start)
        e = parse(inc["ended_at"] or r["actual_end"])
        e = min(e, end)
        if e > s:
            windows.append((s, e, inc["kind"], inc))
    return windows


_PRIORITY = {M.INC_OUTAGE: 0, M.INC_BREACH: 1, M.INC_PULL_DOWN: 2, M.INC_OVERLOAD: 3}


def build_snapshot(conn: sqlite3.Connection, reservation_id: int) -> dict[str, Any]:
    """退库清点结束后生成（或在未结账时重算）费用快照。

    账单沿时间轴拆为计费区间：正常 / 超容 / 临时降温 / 越界 / 断电；
    断电与越界区间生成待财务复核的减免。
    """
    with tx(conn):
        r = require(conn, "reservations", reservation_id)
        existing = conn.execute("SELECT * FROM billing_snapshots WHERE reservation_id=?",
                                (reservation_id,)).fetchone()
        if existing and existing["status"] == "closed":
            raise M.BillingClosedError(f"预约 #{reservation_id} 已结账，租期不可重新打开")
        if not r["actual_start"] or not r["actual_end"]:
            raise M.PlatformError("租约尚未完成入库/退库复核")

        # 已批准的减免保留（解除到旧区间的外键），待复核的减免随区间重算
        conn.execute("UPDATE adjustments SET segment_id=NULL WHERE reservation_id=?",
                     (reservation_id,))
        conn.execute("DELETE FROM billing_segments WHERE reservation_id=?", (reservation_id,))
        conn.execute("DELETE FROM adjustments WHERE reservation_id=? AND status='pending'",
                     (reservation_id,))
        # 已复核过的自动减免按去重键保留，避免重复补偿
        known_keys = {row["dedupe_key"] for row in conn.execute(
            "SELECT dedupe_key FROM adjustments WHERE reservation_id=? AND dedupe_key<>''",
            (reservation_id,))}

        specials = _incident_windows(
            conn, r, (M.INC_OVERLOAD, M.INC_PULL_DOWN, M.INC_BREACH, M.INC_OUTAGE))
        weight = r["checked_weight_kg"] or r["declared_weight_kg"]
        segments: list[dict[str, Any]] = []
        adjustments: list[dict[str, Any]] = []

        for span_start, span_end, zone in _rate_spans(conn, r):
            bounds = sorted({span_start, span_end} |
                            {max(s, span_start) for s, e, *_ in specials if e > span_start and s < span_end} |
                            {min(e, span_end) for s, e, *_ in specials if e > span_start and s < span_end})
            for t0, t1 in zip(bounds, bounds[1:]):
                if t1 <= t0:
                    continue
                active = [(s, e, k, inc) for s, e, k, inc in specials
                          if s < t1 and e > t0 and k in _PRIORITY]
                seg_type = min(active, key=lambda x: _PRIORITY[x[2]])[2] if active else "normal"
                match_incident = next((inc for _, _, k, inc in active if k == seg_type), None)
                secs = (t1 - t0).total_seconds()
                h = M.hours(secs)
                base = M.yuan(h * zone["hourly_rate"])
                wamt = M.yuan(h * weight * zone["kg_hour_rate"])
                surcharge = 0.0
                if seg_type == M.INC_OVERLOAD:
                    surcharge = M.yuan(base * M.OVERLOAD_SURCHARGE_RATE)
                elif seg_type == M.INC_PULL_DOWN:
                    surcharge = M.yuan(h * M.PULL_DOWN_HOUR_RATE)
                gross = M.yuan(base + wamt + surcharge)
                seg = {
                    "seg_type": seg_type, "start_at": iso(t0), "end_at": iso(t1),
                    "hours": h, "weight_kg": weight, "zone_code": zone["code"],
                    "base_amount": base, "weight_amount": wamt,
                    "surcharge": surcharge, "gross_amount": gross,
                    "incident_id": match_incident["id"] if match_incident else None,
                }
                segments.append(seg)

                if seg_type == M.INC_OUTAGE:
                    adjustments.append({
                        "kind": "outage", "amount": M.yuan((base + wamt) * M.OUTAGE_COMP_RATE),
                        "reason": f"断电补偿：{zone['code']} 区 {iso(t0)}–{iso(t1)}",
                        "seg_index": len(segments) - 1,
                        "dedupe_key": f"outage:{iso(t0)}:{iso(t1)}",
                    })
                elif seg_type == M.INC_BREACH:
                    adjustments.append({
                        "kind": "breach_comp", "amount": M.yuan((base + wamt) * M.BREACH_COMP_RATE),
                        "reason": f"温度越界补偿：{zone['code']} 区 {iso(t0)}–{iso(t1)}",
                        "seg_index": len(segments) - 1,
                        "dedupe_key": f"breach_comp:{iso(t0)}:{iso(t1)}",
                    })

        seg_ids: list[int] = []
        for s in segments:
            cur = conn.execute(
                """INSERT INTO billing_segments(reservation_id,seg_type,start_at,end_at,hours,
                       weight_kg,base_amount,weight_amount,surcharge,gross_amount,incident_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (reservation_id, s["seg_type"], s["start_at"], s["end_at"], s["hours"],
                 s["weight_kg"], s["base_amount"], s["weight_amount"], s["surcharge"],
                 s["gross_amount"], s["incident_id"]),
            )
            seg_ids.append(cur.lastrowid)
        for a in adjustments:
            if a.get("dedupe_key") and a["dedupe_key"] in known_keys:
                continue
            conn.execute(
                """INSERT INTO adjustments(reservation_id,segment_id,amount,kind,reason,
                       dedupe_key,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (reservation_id, seg_ids[a["seg_index"]], a["amount"], a["kind"],
                 a["reason"], a.get("dedupe_key", ""), "pending", iso(now())),
            )

        base_total = M.yuan(sum(s["base_amount"] for s in segments))
        weight_total = M.yuan(sum(s["weight_amount"] for s in segments))
        sur_total = M.yuan(sum(s["surcharge"] for s in segments))
        approved_disc = M.yuan(sum(
            row["amount"] for row in conn.execute(
                "SELECT amount FROM adjustments WHERE reservation_id=? AND status='approved'",
                (reservation_id,))))
        occ_hours = M.hours(sum((parse(s["end_at"]) - parse(s["start_at"])).total_seconds()
                                for s in segments))
        early_hours = 0.0
        ei = conn.execute(
            "SELECT metric_value FROM incidents WHERE reservation_id=? AND kind=? LIMIT 1",
            (reservation_id, M.INC_EARLY)).fetchone()
        if ei:
            early_hours = ei["metric_value"] or 0.0
        final_amount = M.yuan(base_total + weight_total + sur_total - approved_disc)

        detail = json.dumps({"segments": segments, "adjustments": adjustments},
                            ensure_ascii=False)
        if existing:
            conn.execute(
                """UPDATE billing_snapshots SET occupancy_hours=?,base_total=?,weight_total=?,
                       surcharge_total=?,discount_total=?,final_amount=?,early_hours=?,detail_json=?
                   WHERE reservation_id=?""",
                (occ_hours, base_total, weight_total, sur_total, approved_disc, final_amount,
                 early_hours, detail, reservation_id),
            )
            snap_id = existing["id"]
        else:
            cur = conn.execute(
                """INSERT INTO billing_snapshots(reservation_id,occupancy_hours,base_total,
                       weight_total,surcharge_total,discount_total,final_amount,early_hours,
                       detail_json,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'open',?)""",
                (reservation_id, occ_hours, base_total, weight_total, sur_total, approved_disc,
                 final_amount, early_hours, detail, iso(now())),
            )
            snap_id = cur.lastrowid
        audit(conn, "system", "snapshot_built", f"reservation:{reservation_id}",
              f"区间 {len(segments)} 段，应收 {final_amount} 元（含已批减免 {approved_disc}）")
        return {"id": snap_id, "final_amount": final_amount, "segments": len(segments)}


def review_adjustment(conn: sqlite3.Connection, adjustment_id: int, approve: bool,
                      staff_id: int, actor: str = "finance") -> None:
    """减免必须财务复核。复核通过后重算未结账快照。"""
    with tx(conn):
        a = require(conn, "adjustments", adjustment_id)
        if a["status"] != "pending":
            raise M.PlatformError("该减免已复核")
        snap = conn.execute("SELECT * FROM billing_snapshots WHERE reservation_id=?",
                            (a["reservation_id"],)).fetchone()
        if snap and snap["status"] == "closed":
            raise M.BillingClosedError("账单已结账，不可再复核减免")
        conn.execute("UPDATE adjustments SET status=?, reviewer_id=?, reviewed_at=? WHERE id=?",
                     ("approved" if approve else "rejected", staff_id, iso(now()), adjustment_id))
        audit(conn, actor, "adjustment_reviewed", f"adjustment:{adjustment_id}",
              f"{'通过' if approve else '驳回'} 减免 {a['amount']} 元：{a['reason']}")
    if approve:
        build_snapshot(conn, a["reservation_id"])


def add_manual_adjustment(conn: sqlite3.Connection, reservation_id: int, amount: float,
                          reason: str, actor: str = "finance") -> int:
    if amount <= 0:
        raise M.PlatformError("减免金额必须为正数")
    if not reason.strip():
        raise M.PlatformError("减免必须说明原因")
    snap = conn.execute("SELECT status FROM billing_snapshots WHERE reservation_id=?",
                        (reservation_id,)).fetchone()
    if snap and snap["status"] == "closed":
        raise M.BillingClosedError("账单已结账，不可追加减免")
    cur = conn.execute(
        """INSERT INTO adjustments(reservation_id,segment_id,amount,kind,reason,status,created_at)
           VALUES(?,NULL,?,'goodwill',?,'pending',?)""",
        (reservation_id, M.yuan(amount), reason, iso(now())),
    )
    audit(conn, actor, "adjustment_proposed", f"reservation:{reservation_id}",
          f"{amount} 元：{reason}（待财务复核）")
    return cur.lastrowid


def settle(conn: sqlite3.Connection, reservation_id: int, actor: str = "finance") -> dict[str, Any]:
    """结账（关闭快照）。幂等：重复结账返回同一快照，不重复收费、不重开租期。"""
    with tx(conn):
        r = require(conn, "reservations", reservation_id)
        snap = conn.execute("SELECT * FROM billing_snapshots WHERE reservation_id=?",
                            (reservation_id,)).fetchone()
        if snap is None:
            raise M.PlatformError("尚未生成费用快照")
        if snap["status"] == "closed" or r["status"] == M.SETTLED:
            return {"id": snap["id"], "final_amount": snap["final_amount"],
                    "already_settled": True}
        pending = conn.execute(
            "SELECT COUNT(*) c FROM adjustments WHERE reservation_id=? AND status='pending'",
            (reservation_id,)).fetchone()["c"]
        if pending:
            raise M.PlatformError(f"还有 {pending} 笔减免待财务复核，不能结账")
        conn.execute("UPDATE billing_snapshots SET status='closed', closed_at=? WHERE id=?",
                     (iso(now()), snap["id"]))
        conn.execute("UPDATE reservations SET status=?, updated_at=? WHERE id=?",
                     (M.SETTLED, iso(now()), reservation_id))
        audit(conn, actor, "settled", f"reservation:{reservation_id}",
              f"结账 {snap['final_amount']} 元，快照 #{snap['id']} 已关闭")
        return {"id": snap["id"], "final_amount": snap["final_amount"], "already_settled": False}


def reopen_after_settle(conn: sqlite3.Connection, reservation_id: int) -> None:
    """测试/守卫用：已结账租期不可重新打开。"""
    r = require(conn, "reservations", reservation_id)
    if r["status"] == M.SETTLED:
        raise M.BillingClosedError(f"预约 #{reservation_id} 已结账，租期不可重新打开")
