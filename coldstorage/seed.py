"""虚构演示数据：库区/货格/租户/租约全流程样例。

一条本地启动命令（python3 run.py --demo）会：
- 建库并写入虚构经营数据；
- 制造“两笔预约争抢最后一个货格，仅一笔成功”；
- 回填温度采样，包含断电、越界、临时降温窗口；
- 生成已退库待结账与已结账账单、移格、门未关、渗漏等留痕；
- 在本地目录生成货品“照片”（SVG 占位图）。
"""

from __future__ import annotations

import math
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import models as M
from . import services as S
from .db import connect, init_db

PHOTO_SVG = """<svg xmlns='http://www.w3.org/2000/svg' width='320' height='220'>
<rect width='100%' height='100%' fill='#eef6f8'/>
<rect x='14' y='14' width='292' height='192' rx='10' fill='#d7eef0' stroke='#7fb7bd' stroke-width='2'/>
<path d='M60 150 q50 -60 100 0 t100 0' fill='none' stroke='#0d9488' stroke-width='4'/>
<text x='160' y='110' font-family='sans-serif' font-size='20' fill='#2e2a3a'
 text-anchor='middle'>{label}</text>
<text x='160' y='180' font-family='sans-serif' font-size='13' fill='#5b5470'
 text-anchor='middle'>虚构货品照片 · 仅演示</text>
</svg>"""


def seed_demo(db_path: str, photo_dir: str, anchor: datetime | None = None) -> dict:
    """写入全套演示数据。返回样例 id 与冲突结果说明。"""
    anchor = anchor or S.now()
    Path(photo_dir).mkdir(parents=True, exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    # 清理可能存在的 WAL 残留
    for suffix in ("-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    init_db(db_path)
    conn = connect(db_path)
    report: dict = {}
    try:
        report.update(_seed(conn, photo_dir, anchor))
        conn.commit()
    finally:
        conn.close()
    return report


def _photo(photo_dir: str, batch_id: int, label: str) -> str:
    path = Path(photo_dir) / f"batch_{batch_id}.svg"
    path.write_text(PHOTO_SVG.replace("{label}", label), encoding="utf-8")
    return str(path)


def _seed(conn: sqlite3.Connection, photo_dir: str, t0: datetime) -> dict:
    # ------------------------------------------------ 库区
    zones = {
        "A": ("深冷库A", M.ZONE_FROZEN, -25, -18, 2.50, 0.03),
        "B": ("冷藏库B", M.ZONE_CHILLED, 0, 4, 1.50, 0.02),
        "C": ("恒温库C", M.ZONE_AMBIENT, 8, 12, 0.90, 0.01),
    }
    zid = {}
    for code, (name, zt, lo, hi, rate, kgr) in zones.items():
        cur = conn.execute(
            "INSERT INTO zones(code,name,zone_type,temp_min,temp_max,hourly_rate,kg_hour_rate)"
            " VALUES(?,?,?,?,?,?,?)",
            (code, name, zt, lo, hi, rate, kgr))
        zid[code] = cur.lastrowid

    def add_cell(code, zone_code, cap):
        cur = conn.execute(
            "INSERT INTO cells(zone_id,code,capacity_m3) VALUES(?,?,?)",
            (zid[zone_code], code, cap))
        return cur.lastrowid

    cells = {}
    for code, cap in [("A-01", 2.0), ("A-02", 2.0), ("A-03", 2.0), ("A-04", 2.0),
                      ("A-05", 2.0), ("A-06", 2.0)]:
        cells[code] = add_cell(code, "A", cap)
    for code, cap in [("B-01", 3.0), ("B-02", 3.0), ("B-03", 3.0), ("B-04", 3.0)]:
        cells[code] = add_cell(code, "B", cap)
    cells["C-01"] = add_cell("C-01", "C", 4.0)
    cells["C-02"] = add_cell("C-02", "C", 4.0)

    # ------------------------------------------------ 人员
    def tenant(name, login, token):
        return S.create_tenant(conn, name, login, token)

    t_fresh = tenant("冰鲜达冷链", "tenant1", "tok-tenant1")
    t_ocean = tenant("海味坊食材", "tenant2", "tok-tenant2")
    t_farm = tenant("雪原果蔬", "tenant3", "tok-tenant3")

    cur = conn.execute("INSERT INTO staff(name,login,token,role) VALUES(?,?,?,?)",
                       ("库管·孟衡", "admin", "tok-admin", "admin"))
    admin_id = cur.lastrowid
    cur = conn.execute("INSERT INTO staff(name,login,token,role) VALUES(?,?,?,?)",
                       ("财务·费谨", "finance", "tok-finance", "finance"))
    fin_id = cur.lastrowid

    # ------------------------------------------------ 货品批次 + 照片
    def batch(tid, name, sku):
        bid = S.create_batch(conn, tid, name, sku)
        S.add_photo(conn, bid, _photo(photo_dir, bid, name), "入库外观照")
        return bid

    b_salmon = batch(t_fresh, "冻三文鱼块", "FRO-SAL-2207")
    b_squid = batch(t_fresh, "冻鱿鱼筒", "FRO-SQU-1180")
    b_shrimp = batch(t_ocean, "冻南美白虾", "FRO-SHR-3302")
    b_berry = batch(t_ocean, "速冻蓝莓", "FRO-BER-0915")
    b_yogurt = batch(t_farm, "低温酸奶杯", "CHL-YOG-7710")
    b_greens = batch(t_farm, "保鲜叶菜箱", "CHL-GRN-4421")
    b_cheese = batch(t_farm, "恒温熟成芝士", "AMB-CHE-0133")
    b_peas = batch(t_fresh, "速冻甜豌豆", "FRO-PEA-5566")
    b_tuna = batch(t_ocean, "冻金枪鱼块", "FRO-TUN-8801")

    # ------------------------------------------------ 历史租约（12 天前）
    h_start = t0 - timedelta(days=12, hours=2)
    h_end = t0 - timedelta(days=10)

    # 已退库待结账：超容 + 断电 + 越界，减免一批（部分已批、一笔待财务复核）
    r_open = _resv(conn, t_farm, b_yogurt, 0, 4, h_start, h_end + timedelta(hours=6),
                   3.0, 1200)
    S.confirm_reservation(conn, r_open, "seed")
    S.check_in(conn, r_open, 1240, 3.35, "seed", at=h_start)   # 超容 0.35 m³
    S.open_incident(conn, M.INC_OUTAGE, "B 区断电（历史）",
                    zone_id=zid["B"], cell_id=cells["B-01"], reservation_id=r_open,
                    severity="critical",
                    detail="市政线路检修，备用电源延迟接入", at=h_start + timedelta(hours=10))
    S.close_incident(conn, _last_inc(conn, r_open, M.INC_OUTAGE), at=h_start + timedelta(hours=10, minutes=40))
    S.open_incident(conn, M.INC_BREACH, "B 区温度越界（历史）",
                    zone_id=zid["B"], cell_id=cells["B-01"], reservation_id=r_open,
                    severity="critical", detail="冷机故障，库温升至 7.8℃",
                    at=h_start + timedelta(hours=30), metric_value=7.8)
    S.close_incident(conn, _last_inc(conn, r_open, M.INC_BREACH),
                     at=h_start + timedelta(hours=31, minutes=20))
    S.check_out(conn, r_open, "seed", at=h_end)
    # 财务批了断电补偿，越界补偿仍待复核
    adjs = list(conn.execute(
        "SELECT id,kind FROM adjustments WHERE reservation_id=? ORDER BY id", (r_open,)))
    outage_adj = next(a for a in adjs if a["kind"] == "outage")
    S.review_adjustment(conn, outage_adj["id"], True, fin_id, "finance")

    # 已结账：正常 + 提前退租，快照关闭
    e_start = t0 - timedelta(days=20)
    e_end = t0 - timedelta(days=17)
    r_done = _resv(conn, t_farm, b_cheese, 8, 12, e_start, e_end + timedelta(days=1),
                   3.6, 800)
    S.confirm_reservation(conn, r_done, "seed")
    S.check_in(conn, r_done, 812, 3.5, "seed", at=e_start)
    S.check_out(conn, r_done, "seed", at=e_end)   # 提前约 1 天
    for a in conn.execute("SELECT id FROM adjustments WHERE reservation_id=? AND status='pending'",
                          (r_done,)):
        S.review_adjustment(conn, a["id"], True, fin_id, "finance")
    S.settle(conn, r_done, "finance")

    # ------------------------------------------------ 当前在租（A-01）：含温度异常
    a_start = t0 - timedelta(hours=26)
    a_plan_end = t0 + timedelta(hours=8)
    r_active = _resv(conn, t_fresh, b_salmon, -25, -18, a_start, a_plan_end,
                     1.2, 480)
    S.confirm_reservation(conn, r_active, "seed")
    S.check_in(conn, r_active, 472, 1.18, "seed", at=a_start)

    # 临时降温（22 小时前，持续 30 分钟）
    S.open_incident(conn, M.INC_PULL_DOWN, "临时降温：突击深冷锁鲜",
                    zone_id=zid["A"], cell_id=cells["A-01"], reservation_id=r_active,
                    severity="info", detail="租户申请 -23℃ 突击降温 30 分钟",
                    at=t0 - timedelta(hours=22))
    S.close_incident(conn, _last_inc(conn, r_active, M.INC_PULL_DOWN),
                     at=t0 - timedelta(hours=21, minutes=30))
    # 断电（8 小时前，24 分钟）
    S.open_incident(conn, M.INC_OUTAGE, "A 区断电：配电室跳闸",
                    zone_id=zid["A"], cell_id=cells["A-01"], reservation_id=r_active,
                    severity="critical", detail="配电室跳闸，24 分钟恢复",
                    at=t0 - timedelta(hours=8))
    S.close_incident(conn, _last_inc(conn, r_active, M.INC_OUTAGE),
                     at=t0 - timedelta(hours=7, minutes=36))
    # 温度越界（5 小时前，30 分钟，升至 -12.4℃）
    S.open_incident(conn, M.INC_BREACH, "A 区温度越界：除霜异常",
                    zone_id=zid["A"], cell_id=cells["A-01"], reservation_id=r_active,
                    severity="critical", detail="蒸发器结霜，库温升至 -12.4℃",
                    metric_value=-12.4, at=t0 - timedelta(hours=5))
    S.close_incident(conn, _last_inc(conn, r_active, M.INC_BREACH),
                     at=t0 - timedelta(hours=4, minutes=30))

    # 门磁：开门 2 小时前正常开合一次；6 小时前门未关 18 分钟
    _door(conn, cells["A-01"], "open", t0 - timedelta(hours=2))
    _door(conn, cells["A-01"], "close", t0 - timedelta(hours=2) + timedelta(minutes=4))
    _door(conn, cells["A-01"], "open", t0 - timedelta(hours=6, minutes=18))
    S.open_incident(conn, M.INC_DOOR_OPEN, "货格 A-01 门未关",
                    zone_id=zid["A"], cell_id=cells["A-01"], reservation_id=r_active,
                    severity="warning",
                    detail="作业后门磁持续打开 18 分钟", at=t0 - timedelta(hours=6, minutes=18))
    S.close_incident(conn, _last_inc(conn, r_active, M.INC_DOOR_OPEN),
                     at=t0 - timedelta(hours=6))

    # ------------------------------------------------ 同温区移位已完成（A-02 → A-03）
    m_start = t0 - timedelta(hours=10)
    r_move = _resv(conn, t_ocean, b_shrimp, -24, -19, m_start, t0 + timedelta(hours=20),
                   1.5, 610)
    S.confirm_reservation(conn, r_move, "seed")
    S.check_in(conn, r_move, 605, 1.46, "seed", at=m_start)
    direct, move_id = S.request_move(conn, r_move, cells["A-03"], "风机维护，同温区调格", "seed")
    assert direct

    # 跨温区移格待重新确认：A-04（深冷）→ B-03（冷藏）
    x_start = t0 - timedelta(hours=6)
    r_cross = _resv(conn, t_fresh, b_squid, -25, -18, x_start, t0 + timedelta(hours=30),
                    1.1, 300)
    S.confirm_reservation(conn, r_cross, "seed")
    S.check_in(conn, r_cross, 298, 1.05, "seed", at=x_start)
    S.request_move(conn, r_cross, cells["B-03"], "解冻周转，需跨温区", "seed")

    # ------------------------------------------------ B-01 在租：门未关 + 渗漏留痕
    d_start = t0 - timedelta(hours=18)
    r_door = _resv(conn, t_farm, b_greens, 0, 4, d_start, t0 + timedelta(hours=12),
                   2.2, 350)
    S.confirm_reservation(conn, r_door, "seed")
    S.check_in(conn, r_door, 346, 2.1, "seed", at=d_start)
    _door(conn, cells["B-01"], "open", t0 - timedelta(minutes=40))
    # 暂不关门（监测任务会把门未关记出来）
    S.log_leak(conn, cells["B-01"], "叶菜箱凝露水外渗，已铺设吸水垫", "seed")

    # A-05 长期锁定（他租户）
    r_lock = _resv(conn, t_ocean, b_berry, -24, -20, t0 - timedelta(hours=2),
                   t0 + timedelta(hours=70), 1.8, 520)
    S.confirm_reservation(conn, r_lock, "seed")

    # A-02 占住（确认未入库），使 A-06 成为兼容温区唯一空格，争抢真实发生
    r_fill = _resv(conn, t_ocean, b_tuna, -24, -18, t0 + timedelta(hours=2),
                   t0 + timedelta(hours=50), 1.9, 640)
    S.confirm_reservation(conn, r_fill, "seed")

    # B-03 锁定
    r_lock2 = _resv(conn, t_farm, b_cheese, 1, 4, t0 + timedelta(hours=1),
                    t0 + timedelta(hours=40), 2.5, 400)
    S.confirm_reservation(conn, r_lock2, "seed")

    # ------------------------------------------------ 争抢最后一个货格 A-06
    c1 = _resv(conn, t_fresh, b_peas, -23, -19, t0 + timedelta(hours=3),
               t0 + timedelta(hours=27), 1.6, 700, note="快航优选档")
    c2 = _resv(conn, t_ocean, b_berry, -22, -20, t0 + timedelta(hours=4),
               t0 + timedelta(hours=28), 1.6, 690, note="海味坊同档抢订")
    S.confirm_reservation(conn, c1, "admin")
    contention = {"winner": c1, "loser": c2, "error": ""}
    try:
        S.confirm_reservation(conn, c2, "admin")
    except M.CellCapacityError as exc:
        contention["error"] = str(exc)

    # 管理员手工改温样例（C 区，说明原因）
    S.manual_temp_change(conn, zid["C"], 9, 13,
                         "熟成芝士客户要求提高 1℃，已签署工艺变更单", "admin")

    # ------------------------------------------------ 回填温度采样（30 小时，每 10 分钟）
    _backfill_samples(conn, zid, t0, a_start)

    return {
        "reservation_active": r_active,
        "reservation_open_bill": r_open,
        "reservation_settled": r_done,
        "reservation_cross_move": r_cross,
        "contention": contention,
        "admin_id": admin_id,
        "finance_id": fin_id,
        "tenant_ids": {"fresh": t_fresh, "ocean": t_ocean, "farm": t_farm},
    }


def _resv(conn, tid, bid, tmin, tmax, start, end, vol, weight, note=""):
    return S.create_reservation(conn, tid, bid, tmin, tmax, start, end, vol, weight, note)


def _last_inc(conn, reservation_id: int, kind: str) -> int:
    return conn.execute(
        "SELECT id FROM incidents WHERE reservation_id=? AND kind=? ORDER BY id DESC LIMIT 1",
        (reservation_id, kind)).fetchone()["id"]


def _door(conn, cell_id, kind, at):
    conn.execute(
        "INSERT INTO door_events(cell_id,kind,event_at,source,note) VALUES(?,?,?,'sim','模拟门磁')",
        (cell_id, kind, S.iso(at)))


def _backfill_samples(conn, zid: dict, t0: datetime, since: datetime) -> None:
    """近 30 小时历史曲线：稳态小幅波动 + A 区注入断电/越界/降温窗口。"""
    start = t0 - timedelta(hours=30)
    rows = []
    step = timedelta(minutes=10)
    t = start
    k = 0
    zones_meta = {
        "A": (-21.5, [
            (t0 - timedelta(hours=22), timedelta(minutes=30), -24.0),   # 临时降温
            (t0 - timedelta(hours=8), timedelta(minutes=24), 14.0),     # 断电升温
            (t0 - timedelta(hours=5), timedelta(minutes=30), -12.4),    # 越界
        ]),
        "B": (2.0, []),
        "C": (10.5, []),
    }
    while t <= t0:
        for code, (base, windows) in zones_meta.items():
            value = base + math.sin(k / 5.0) * 0.35
            for ws, dur, target in windows:
                if ws <= t < ws + dur:
                    # 线性靠近目标值，断电按指数爬升
                    frac = (t - ws).total_seconds() / dur.total_seconds()
                    if target > 10:
                        value = base + (target - base) * min(1.0, frac * 1.6)
                    else:
                        value = target + math.sin(k / 3.0) * 0.2
            rows.append((zid[code], round(value, 2), S.iso(t), "sim"))
        t += step
        k += 1
    conn.executemany(
        "INSERT INTO temp_samples(zone_id,celsius,sampled_at,source) VALUES(?,?,?,?)", rows)
