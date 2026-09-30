"""计费引擎。

模型：
* 基础租金 = 温区单价 × 锁定容量 × 占格小时数；跨温区移格会在移格时刻把时间轴切开。
* 重量附加费 = max(0, 实称重量 − 标准密度×容量) 换算吨 × 小时 × 单价。
* 异常项来自 incidents：越界/断电是给租户的补偿（负数，需财务复核）；
  超容、临时降温、门未关、渗漏、提前退租是加收（正数）。
* 退库时生成不可变快照 detail_json；已存在账单的租期重复结算直接返回原账单。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import config
from .errors import DomainError
from .util import hours_between, now_iso, parse


@dataclass
class Line:
    kind: str
    label: str
    ts: str
    amount: float            # 正=加收，负=减免
    detail: str = ""
    reviewed: bool = False
    incident_id: int | None = None


def _zone_at(conn: sqlite3.Connection, lease: sqlite3.Row) -> list[tuple[str, str]]:
    """返回 (变更时刻, 温区code) 时间轴，首项为入库时温区。"""
    moves = conn.execute(
        "SELECT m.done_at, c.zone_code FROM moves m JOIN cells c ON c.id=m.to_cell"
        " WHERE m.lease_id=? AND m.status='done' ORDER BY m.done_at",
        (lease["id"],),
    ).fetchall()
    if not moves:
        return [(lease["started_at"], lease["zone_code"])]
    first_from = conn.execute(
        "SELECT zone_code FROM cells WHERE id=(SELECT from_cell FROM moves"
        " WHERE lease_id=? ORDER BY done_at LIMIT 1)", (lease["id"],)).fetchone()
    timeline = [(lease["started_at"], first_from["zone_code"])]
    for m in moves:
        timeline.append((m["done_at"], m["zone_code"]))
    return timeline


def _zone_for(timeline: list[tuple[str, str]], ts: str) -> str:
    current = timeline[0][1]
    for change_ts, zone in timeline:
        if ts >= change_ts:
            current = zone
        else:
            break
    return current


def build_snapshot(conn: sqlite3.Connection, lease: sqlite3.Row,
                   end_ts: str) -> dict:
    start_ts = lease["started_at"]
    timeline = _zone_at(conn, lease)

    incidents = conn.execute(
        "SELECT * FROM incidents WHERE lease_id=? ORDER BY ts, id",
        (lease["id"],),
    ).fetchall()

    # ---- 时间轴切分点：移格时刻 + 异常起止 ----
    bounds = {start_ts, end_ts}
    for change_ts, _ in timeline[1:]:
        if start_ts < change_ts < end_ts:
            bounds.add(change_ts)
    for inc in incidents:
        anchor = inc["start_ts"] or inc["ts"]
        if start_ts < anchor < end_ts:
            bounds.add(anchor)
        if inc["end_ts"] and start_ts < inc["end_ts"] < end_ts:
            bounds.add(inc["end_ts"])
    cuts = sorted(bounds, key=parse)

    def inc_anchor(inc: sqlite3.Row) -> str:
        return inc["start_ts"] or inc["ts"]

    segments: list[dict] = []
    for i in range(len(cuts) - 1):
        seg_from, seg_to = cuts[i], cuts[i + 1]
        hours = hours_between(seg_from, seg_to)
        zone_code = _zone_for(timeline, seg_from)
        z = config.ZONES[zone_code]
        base = round(z["rate"] * lease["volume_m3"] * hours, 2)

        excess_t = max(0.0, lease["weight_kg"]
                       - config.WEIGHT_DENSITY_KG_PER_M3 * lease["volume_m3"]) / 1000.0
        weight_fee = round(excess_t * config.WEIGHT_SURCHARGE_PER_TON_HOUR * hours, 2)

        items: list[dict] = []
        for inc in incidents:
            anchor = inc_anchor(inc)
            if seg_from <= anchor < seg_to or (i == len(cuts) - 2 and anchor == seg_to):
                items.append({
                    "incident_id": inc["id"],
                    "kind": inc["kind"],
                    "label": _INC_LABELS[inc["kind"]],
                    "ts": inc["ts"],
                    "start_ts": inc["start_ts"],
                    "end_ts": inc["end_ts"],
                    "detail": inc["detail"],
                    "amount": round(inc["amount"], 2),
                    "reviewed": bool(inc["reviewed"]),
                })
        segments.append({
            "from": seg_from, "to": seg_to,
            "hours": round(hours, 3),
            "zone": zone_code, "zone_name": z["name"],
            "rate": z["rate"], "volume_m3": lease["volume_m3"],
            "weight_kg": lease["weight_kg"],
            "base_rent": base,
            "weight_surcharge": weight_fee,
            "items": items,
        })

    # 锚点在租期窗口之外的异常（理论上不该有）也挂到首尾段，避免漏账
    anchored = {it["incident_id"] for s in segments for it in s["items"]}
    for inc in incidents:
        if inc["id"] in anchored:
            continue
        item = {
            "incident_id": inc["id"], "kind": inc["kind"],
            "label": _INC_LABELS[inc["kind"]], "ts": inc["ts"],
            "start_ts": inc["start_ts"], "end_ts": inc["end_ts"],
            "detail": inc["detail"], "amount": round(inc["amount"], 2),
            "reviewed": bool(inc["reviewed"]),
        }
        (segments[0] if inc["ts"] <= start_ts else segments[-1])["items"].append(item)

    subtotal = round(sum(s["base_rent"] for s in segments), 2)
    weight_total = round(sum(s["weight_surcharge"] for s in segments), 2)
    surcharge = round(weight_total + sum(
        it["amount"] for s in segments for it in s["items"] if it["amount"] > 0), 2)
    discount = round(-sum(
        it["amount"] for s in segments for it in s["items"] if it["amount"] < 0), 2)
    total = round(subtotal + surcharge - discount, 2)
    total_hours = hours_between(start_ts, end_ts)

    return {
        "start": start_ts, "end": end_ts,
        "total_hours": round(total_hours, 3),
        "cell_id": lease["cell_id"],
        "segments": segments,
        "subtotal": subtotal,
        "weight_surcharge_total": weight_total,
        "surcharge_total": surcharge,
        "discount_total": discount,
        "total": max(0.0, total),
        "needs_review": discount > 0,
        "generated_at": now_iso(),
    }


_INC_LABELS = {
    "overcapacity": "超容罚金",
    "temp_pull": "临时降温",
    "excursion": "温度越界补偿",
    "outage": "断电补偿",
    "door_open": "门未关加收",
    "leak": "货品渗漏清理",
    "early_exit": "提前退租违约金",
}


def persist_bill(conn: sqlite3.Connection, lease: sqlite3.Row,
                 snapshot: dict, end_ts: str) -> int:
    r = conn.execute("SELECT tenant_id FROM reservations WHERE id=?",
                     (lease["reservation_id"],)).fetchone()
    detail = json.dumps(snapshot, ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO bills(lease_id,tenant_id,created_at,period_from,period_to,"
        "subtotal,discount_total,surcharge_total,total,status,detail_json)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (lease["id"], r["tenant_id"], now_iso(), snapshot["start"], end_ts,
         snapshot["subtotal"], snapshot["discount_total"],
         snapshot["surcharge_total"], snapshot["total"],
         "reviewed" if not snapshot["needs_review"] else "issued", detail),
    )
    bill_id = cur.lastrowid
    # 标记异常项已入账，防止重复结算重复计入
    for seg in snapshot["segments"]:
        for it in seg["items"]:
            conn.execute("UPDATE incidents SET billing_tag=? WHERE id=?",
                         (f"bill-{bill_id}", it["incident_id"]))
    return bill_id


def get_bill(conn: sqlite3.Connection, bill_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["detail"] = json.loads(d.pop("detail_json"))
    return d


def list_bills(conn: sqlite3.Connection, tenant_id: str | None = None) -> list[dict]:
    sql = ("SELECT b.*, t.name tenant_name FROM bills b"
           " JOIN tenants t ON t.id=b.tenant_id")
    rows = conn.execute(sql).fetchall()
    out = []
    for row in rows:
        if tenant_id and row["tenant_id"] != tenant_id:
            continue
        d = dict(row)
        d.pop("detail_json", None)
        out.append(d)
    return out


def review_discount(conn: sqlite3.Connection, bill_id: int, incident_id: int,
                    approve: bool, by_user: str, note: str = "") -> dict:
    """财务复核负数减免项。驳回则把该项金额清零并重算账单（快照保留审计痕迹）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        bill_row = conn.execute("SELECT * FROM bills WHERE id=?",
                                (bill_id,)).fetchone()
        if bill_row is None:
            raise DomainError("账单不存在")
        if bill_row["status"] == "paid":
            raise DomainError("账单已支付，不能再复核")
        snapshot = json.loads(bill_row["detail_json"])
        target = None
        for seg in snapshot["segments"]:
            for it in seg["items"]:
                if it["incident_id"] == incident_id:
                    target = it
        if target is None:
            raise DomainError("该异常项不在此账单中")
        if target["amount"] >= 0:
            raise DomainError("只有减免/补偿项需要财务复核")
        if target["reviewed"]:
            raise DomainError("该项已复核")

        conn.execute(
            "INSERT INTO reviews(bill_id,incident_id,action,by_user,note,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (bill_id, incident_id, "approve" if approve else "reject",
             by_user, note, now_iso()),
        )
        old_amount = target["amount"]
        target["reviewed"] = True
        target["review_note"] = note
        target["review_action"] = "approve" if approve else "reject"
        if not approve:
            target["detail"] += f"（财务驳回减免，原金额 {old_amount} 元）"
            target["amount"] = 0.0
        conn.execute("UPDATE incidents SET reviewed=1 WHERE id=?", (incident_id,))

        _recalc(snapshot)
        conn.execute(
            "UPDATE bills SET subtotal=?,discount_total=?,surcharge_total=?,"
            "total=?,status=?,detail_json=? WHERE id=?",
            (snapshot["subtotal"], snapshot["discount_total"],
             snapshot["surcharge_total"], snapshot["total"],
             "reviewed" if not snapshot["needs_review"] else "issued",
             json.dumps(snapshot, ensure_ascii=False), bill_id),
        )
        conn.execute("COMMIT")
        return snapshot
    except Exception:
        conn.execute("ROLLBACK")
        raise


def mark_paid(conn: sqlite3.Connection, bill_id: int) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
        if row is None:
            raise DomainError("账单不存在")
        if row["status"] == "paid":
            raise DomainError("账单已支付，不能重复结账")
        if json.loads(row["detail_json"]).get("needs_review"):
            # 快照里的 needs_review 是生成时的标志；以逐项 reviewed 为准
            snap = json.loads(row["detail_json"])
            pending = [it for s in snap["segments"] for it in s["items"]
                       if it["amount"] < 0 and not it.get("reviewed")]
            if pending:
                raise DomainError("尚有减免项未经财务复核，不能结账")
        conn.execute("UPDATE bills SET status='paid' WHERE id=?", (bill_id,))
        conn.execute("UPDATE reservations SET status='settled'"
                     " WHERE id=(SELECT reservation_id FROM leases WHERE id="
                     "(SELECT lease_id FROM bills WHERE id=?))", (bill_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _recalc(snapshot: dict) -> None:
    snapshot["subtotal"] = round(sum(s["base_rent"] for s in snapshot["segments"]), 2)
    weight_total = round(sum(s["weight_surcharge"] for s in snapshot["segments"]), 2)
    snapshot["weight_surcharge_total"] = weight_total
    snapshot["surcharge_total"] = round(weight_total + sum(
        it["amount"] for s in snapshot["segments"] for it in s["items"]
        if it["amount"] > 0), 2)
    snapshot["discount_total"] = round(-sum(
        it["amount"] for s in snapshot["segments"] for it in s["items"]
        if it["amount"] < 0), 2)
    snapshot["total"] = max(0.0, round(
        snapshot["subtotal"] + snapshot["surcharge_total"]
        - snapshot["discount_total"], 2))
    pending = any(it["amount"] < 0 and not it.get("reviewed")
                  for s in snapshot["segments"] for it in s["items"])
    snapshot["needs_review"] = pending
