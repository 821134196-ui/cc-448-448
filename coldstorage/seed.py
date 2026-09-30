"""演示数据种子（全部虚构）。

``python3 run.py --demo`` 会重建数据库并生成：
* 三个温区 24 个货格、3 个租户、管理员与财务账号；
* 两笔争抢冷冻区最后一个合适货格的预约（仅一笔确认成功）；
* 一个在租货格的温度越界 + 断电 + 门未关样例（回填采样）；
* 一张含异常补偿、等待财务复核的账单，以及一张已结清的提前退租账单；
* 同温区已完成移格、跨温区待审批移格、管理员改温（含原因）各一例。
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

from . import adapters, billing, config
from .db import connect
from .monitor import detect_all
from .services import (check_out, confirm_reservation, create_reservation,
                       override_temp, request_move, seed_catalog)
from .util import (hash_password, iso, now, now_iso, shift_iso)
from .errors import DomainError

PHOTO_SVG = """<svg xmlns='http://www.w3.org/2000/svg' width='320' height='200'>
<rect width='100%' height='100%' fill='#e7f4f3'/>
<rect x='14' y='14' width='292' height='172' rx='10' fill='none' stroke='#1b8a86' stroke-width='2'/>
<text x='160' y='96' font-family='sans-serif' font-size='22' fill='#2d314d' text-anchor='middle'>{name}</text>
<text x='160' y='128' font-family='sans-serif' font-size='13' fill='#5a6079' text-anchor='middle'>虚构货品照片 · {tag}</text>
</svg>"""


def _photo(name: str, tag: str) -> str:
    config.ensure_dirs()
    safe = "".join(ch for ch in tag if ch.isalnum() or ch in "-_")
    fname = f"{safe}.svg"
    (config.PHOTO_DIR / fname).write_text(PHOTO_SVG.format(name=name, tag=tag),
                                          encoding="utf-8")
    return fname


def _make_users(conn: sqlite3.Connection) -> None:
    aid, aname, apw = config.ADMIN_USER
    fid, fname, fpw = config.FINANCE_USER
    conn.execute("INSERT OR IGNORE INTO users(id,display_name,pw_hash,role,tenant_id)"
                 " VALUES(?,?,?,?,'')", (aid, aname, hash_password(apw), "admin"))
    conn.execute("INSERT OR IGNORE INTO users(id,display_name,pw_hash,role,tenant_id)"
                 " VALUES(?,?,?,?,'')", (fid, fname, hash_password(fpw), "finance"))
    for tid, name, pw in config.DEMO_TENANTS:
        conn.execute("INSERT OR IGNORE INTO tenants(id,name,contact,created_at)"
                     " VALUES(?,?,?,?)",
                     (tid, name, f"{name}·虚构联系人 1380000{tid[-3:]}", now_iso()))
        conn.execute("INSERT OR IGNORE INTO users(id,display_name,pw_hash,role,tenant_id)"
                     " VALUES(?,?,?,'tenant',?)",
                     (tid, name, hash_password(pw), tid))


def _reservation_row(conn: sqlite3.Connection, tenant: str, zone: str, cell: str,
                     start_ts: str, end_ts: str, need: float,
                     status: str) -> int:
    z = config.ZONES[zone]
    cur = conn.execute(
        "INSERT INTO reservations(tenant_id,zone_code,cell_id,need_volume_m3,"
        "target_temp_min,target_temp_max,start_ts,end_ts,status,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (tenant, zone, cell, need, z["temp_min"], z["temp_max"], start_ts, end_ts,
         status, now_iso()),
    )
    return cur.lastrowid


def _lease(conn: sqlite3.Connection, tenant: str, zone: str, cell: str,
           start_ts: str, weight_kg: float, goods: list[tuple[str, float, float]],
           end_ts: str | None = None, photo_tag: str | None = None) -> int:
    """直接构造一个在租（或已关闭）租期及批次，供样例使用。"""
    z = config.ZONES[zone]
    cell_row = conn.execute("SELECT volume_m3 FROM cells WHERE id=?",
                            (cell,)).fetchone()
    status = "checked_out" if end_ts else "checked_in"
    rid = _reservation_row(conn, tenant, zone, cell, start_ts,
                           shift_iso(end_ts or now_iso(), days=3),
                           cell_row["volume_m3"], status)
    total_v = 0.0
    for i, (gname, w, v) in enumerate(goods, 1):
        photo = _photo(gname, f"{photo_tag or cell}-b{i}") if photo_tag else None
        conn.execute(
            "INSERT INTO batches(reservation_id,name,declared_weight_kg,"
            "actual_weight_kg,volume_m3,photo_path,inbound_note,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (rid, gname, w * 0.96, w, v, photo, "入库复核已确认", start_ts),
        )
        total_v += v
    cur = conn.execute(
        "INSERT INTO leases(reservation_id,cell_id,zone_code,volume_m3,weight_kg,"
        "temp_min,temp_max,started_at,ended_at,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (rid, cell, zone, cell_row["volume_m3"], weight_kg, z["temp_min"],
         z["temp_max"], start_ts, end_ts, "closed" if end_ts else "open"),
    )
    return cur.lastrowid


def seed(reset: bool = True) -> dict:
    config.ensure_dirs()
    if reset:
        for p in (config.DB_PATH,):
            Path(p).unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(config.DB_PATH) + suffix).unlink(missing_ok=True)
    conn = connect()
    conn.execute("PRAGMA foreign_keys=ON")
    seed_catalog(conn)
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] > 0:
        conn.close()
        return {"note": "数据已存在，跳过播种"}
    _make_users(conn)

    t_now = now_iso()

    # ---- 冷冻区 F-01..F-06 占满（为"最后一个货格"争抢做铺垫） ----
    frozen_goods = [
        ("t_beiji",  [("冷冻三文鱼块", 1650, 2.6), ("冻虾仁", 700, 1.4)]),
        ("t_beiji",  [("冻鱿鱼圈", 1500, 3.6)]),
        ("t_lvyuan", [("速冻豌豆", 1300, 3.8)]),
        ("t_lvyuan", [("冻蓝莓", 1100, 3.4)]),
        ("t_tianshan", [("冰淇淋原料浆", 1700, 3.9)]),
        ("t_tianshan", [("黄油块", 1600, 3.7)]),
    ]
    for i, (tenant, goods) in enumerate(frozen_goods, 1):
        cell = f"F-{i:02d}"
        w = sum(g[1] for g in goods)
        _lease(conn, tenant, "F", cell, shift_iso(t_now, days=-2), w, goods,
               photo_tag=f"occupy-F{i:02d}")

    # ---- 冷藏区 C-01：在租，回填 越界 + 断电 + 门未关 ----
    c01_start = shift_iso(t_now, hours=-26)
    lease_anomaly = _lease(
        conn, "t_lvyuan", "C", "C-01", c01_start, 1450,
        [("奶油草莓", 900, 2.8), ("鲜切蜜瓜", 550, 1.6)], photo_tag="C01")
    adapters.backfill_series(
        conn, "C-01", c01_start, t_now, interval_min=5,
        outage_windows=[(shift_iso(t_now, hours=-6), shift_iso(t_now, hours=-5, minutes=-40))],
        excursion_windows=[(shift_iso(t_now, hours=-20),
                            shift_iso(t_now, hours=-19, minutes=-30), 4.5)],
    )
    adapters.door_event(conn, "C-01", shift_iso(t_now, hours=-3),
                        shift_iso(t_now, hours=-2, minutes=-48),
                        "装卸货时门开启过久")

    # ---- C-03 → C-04 同温区移格（已完成） ----
    c03_start = shift_iso(t_now, hours=-10)
    lease_move = _lease(conn, "t_lvyuan", "C", "C-03", c03_start, 1200,
                        [("有机牛奶箱", 1200, 3.0)], photo_tag="C03")
    move_done = shift_iso(t_now, hours=-4)
    conn.execute(
        "UPDATE leases SET cell_id='C-04' WHERE id=?", (lease_move,))
    conn.execute(
        "INSERT INTO moves(lease_id,from_cell,to_cell,same_zone,reason,status,"
        "decided_by,decided_at,created_at,done_at) VALUES(?,?,?,1,?,'done',?,?,?,?)",
        (lease_move, "C-03", "C-04", "邻近风机维护，平移到空格",
         "system-auto", move_done, c03_start, move_done),
    )
    adapters.backfill_series(conn, "C-03", c03_start, move_done, 5)
    adapters.backfill_series(conn, "C-04", move_done, t_now, 5)

    # ---- F-02 跨温区移格申请（待管理员重新确认） ----
    f02 = conn.execute("SELECT id FROM leases WHERE cell_id='F-02'").fetchone()["id"]
    conn.execute(
        "INSERT INTO moves(lease_id,from_cell,to_cell,same_zone,reason,status,created_at)"
        " VALUES(?,?,'AC-05',0,?,'requested',?)",
        (f02, "F-02", "货品改工艺需恒温醒发，申请转恒温区", t_now),
    )

    # ---- 恒温区：AC-01 管理员手工改温（有原因） ----
    # 先按默认带回填历史读数，再改温；改温后由监控线程按新带继续采样
    ac01 = _lease(conn, "t_tianshan", "AC", "AC-01", shift_iso(t_now, days=-1),
                  800, [("菌种培养箱", 800, 4.0)], photo_tag="AC01")
    adapters.backfill_series(conn, "AC-01", shift_iso(t_now, days=-1), t_now, 10)
    override_temp(conn, "AC-01", 10.0, 14.0,
                  "租户申请菌种低温培养试验，管理员现场确认并签字（虚构）",
                  "admin")

    # ---- C-02：已退库账单，含越界补偿（待财务复核）+ 提前退租 ----
    c02_start = shift_iso(t_now, hours=-30)
    lease_bill = _lease(conn, "t_beiji", "C", "C-02", c02_start, 1300,
                        [("冷鲜带鱼", 1300, 3.2)], photo_tag="C02")
    adapters.backfill_series(
        conn, "C-02", c02_start, shift_iso(t_now, hours=-1), interval_min=5,
        excursion_windows=[(shift_iso(t_now, hours=-28),
                            shift_iso(t_now, hours=-27, minutes=-20), 4.5)],
    )
    detect_all(conn)
    out1 = check_out(conn, lease_bill, "admin")  # 预约结束在 3 天后 → 提前退租

    # ---- AC-03：更早的已结清账单 ----
    ac03_start = shift_iso(t_now, days=-4)
    lease_paid = _lease(conn, "t_tianshan", "AC", "AC-03", ac03_start, 600,
                        [("红酒原浆", 600, 3.5)], photo_tag="AC03")
    out2 = check_out(conn, lease_paid, "admin")
    bill2 = billing.get_bill(conn, out2["bill_id"])
    # 该账单只有提前退租违约金（正数），无需减免复核，直接结清
    billing.mark_paid(conn, bill2["id"])

    # ---- 预约日历：若干已确认预约分布在未来 ----
    future_confirmed = [
        ("t_tianshan", "C", "C-06", 3.0, 1, 4),
        ("t_beiji", "AC", "AC-06", 4.0, 2, 6),
        ("t_lvyuan", "C", "C-07", 5.0, 5, 8),
    ]
    for tenant, zone, cell, need, d0, d1 in future_confirmed:
        rid = _reservation_row(conn, tenant, zone, None,
                               shift_iso(t_now, days=d0),
                               shift_iso(t_now, days=d1), need, "pending")
        # 直接指定空格确认
        conn.execute("UPDATE reservations SET cell_id=?,status='confirmed' WHERE id=?",
                     (cell, rid))

    # ---- 核心样例：两笔预约争抢冷冻区最后一个合适货格 ----
    need = 5.0  # F-07(6m³) 可容；F-08 只有 4m³
    r1 = create_reservation(conn, "t_lvyuan", "F", need,
                            shift_iso(t_now, hours=2), shift_iso(t_now, days=2),
                            "新增一批速冻蔬菜")
    r2 = create_reservation(conn, "t_beiji", "F", need,
                            shift_iso(t_now, hours=3), shift_iso(t_now, days=2),
                            "渔获到港紧急入库")
    win1 = win2 = None
    try:
        win1 = confirm_reservation(conn, r1)
    except DomainError as exc:
        win1 = {"error": str(exc)}
    try:
        win2 = confirm_reservation(conn, r2)
    except DomainError as exc:
        conn.execute("UPDATE reservations SET note=? WHERE id=?",
                     (f"确认失败：{exc}", r2))
        win2 = {"error": str(exc)}

    # 让监控线程继续给 C-01 产出 10 分钟的带外读数（实时异常演示）
    adapters.set_scenario("C-01", "excursion", minutes=10, bias=4.5)

    conn.commit()
    conn.close()
    return {
        "race": {"reservations": [r1, r2], "first": win1, "second": win2},
        "anomaly_lease": lease_anomaly,
        "open_bill": out1["bill_id"],
        "paid_bill": out2["bill_id"],
        "accounts": {
            "admin/admin1234": "管理员",
            "finance/finance1234": "财务",
            "t_lvyuan/pass1234": "绿源生鲜",
            "t_beiji/pass1234": "北极渔业",
            "t_tianshan/pass1234": "天山乳品",
        },
    }
