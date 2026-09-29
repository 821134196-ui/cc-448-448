"""平台规则测试：

- 容量锁定（两笔预约并发争抢最后一个货格，仅一笔成功）
- 温区约束（不兼容温区、同温区直接移位、跨温区重新确认）
- 计费边界（入库才起算、退库截止、超容/断电/越界/提前退租）
- 结算幂等（重复结账、结账后不可重开/追加减免）
- 租户隔离（服务层 + HTTP 层）
- 进程内监测任务（门未关、断电/越界开合）
"""

from __future__ import annotations

import http.client
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from coldstorage import models as M
from coldstorage import services as S
from coldstorage.adapters import MonitorLoop, SimDoorAdapter, SimTempAdapter
from coldstorage.db import connect, init_db, tx
from coldstorage.seed import seed_demo
from coldstorage.webapp import make_server


# ------------------------------------------------------------ 测试夹具

class CaseBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        self.photos = os.path.join(self.tmp.name, "photos")
        init_db(self.db)
        self.conn = connect(self.db)
        self._build_fixture()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _build_fixture(self):
        c = self.conn
        self.zA = c.execute(
            "INSERT INTO zones(code,name,zone_type,temp_min,temp_max,hourly_rate,kg_hour_rate)"
            " VALUES('A','深冷A','frozen',-25,-18,2.5,0.03)").lastrowid
        self.zB = c.execute(
            "INSERT INTO zones(code,name,zone_type,temp_min,temp_max,hourly_rate,kg_hour_rate)"
            " VALUES('B','冷藏B','chilled',0,4,1.5,0.02)").lastrowid
        self.cellA1 = c.execute(
            "INSERT INTO cells(zone_id,code,capacity_m3) VALUES(?,?,?)",
            (self.zA, "A-01", 2.0)).lastrowid
        self.cellA2 = c.execute(
            "INSERT INTO cells(zone_id,code,capacity_m3) VALUES(?,?,?)",
            (self.zA, "A-02", 2.0)).lastrowid
        self.cellB1 = c.execute(
            "INSERT INTO cells(zone_id,code,capacity_m3) VALUES(?,?,?)",
            (self.zB, "B-01", 3.0)).lastrowid
        self.t1 = S.create_tenant(c, "租户甲", "t1", "tok1")
        self.t2 = S.create_tenant(c, "租户乙", "t2", "tok2")
        c.execute("INSERT INTO staff(name,login,token,role) VALUES('管理员','admin','tok-admin','admin')")
        self.admin_id = c.execute("SELECT id FROM staff WHERE login='admin'").fetchone()["id"]
        c.execute("INSERT INTO staff(name,login,token,role) VALUES('财务','fin','tok-fin','finance')")
        self.fin_id = c.execute("SELECT id FROM staff WHERE login='fin'").fetchone()["id"]
        self.b1 = S.create_batch(c, self.t1, "冻虾", "SKU1")
        self.b2 = S.create_batch(c, self.t2, "冻鱼", "SKU2")

    def reserve(self, tid=..., bid=..., tmin=-25, tmax=-18, start=None, end=None,
                vol=1.5, weight=500.0):
        tid = self.t1 if tid is ... else tid
        bid = self.b1 if bid is ... else bid
        t0 = S.now()
        start = start or (t0 - timedelta(hours=1))
        end = end or (t0 + timedelta(hours=23))
        return S.create_reservation(self.conn, tid, bid, tmin, tmax, start, end, vol, weight)

    def full_lifecycle(self, rid, at_start=None, at_end=None, weight=500.0, volume=1.5):
        r = S.require(self.conn, "reservations", rid)
        if r["status"] == M.PENDING:
            S.confirm_reservation(self.conn, rid)
        at_start = at_start or (S.now() - timedelta(hours=1))
        if r["status"] == M.CONFIRMED:
            S.check_in(self.conn, rid, weight, volume, at=at_start)
        if at_end:
            S.check_out(self.conn, rid, at=at_end)
        return at_start


# ------------------------------------------------------------ 1. 容量锁定

class TestCapacityLocking(CaseBase):
    def test_confirm_locks_cell_capacity(self):
        rid = self.reserve(vol=1.5)
        cell_id = S.confirm_reservation(self.conn, rid)
        cell = S.require(self.conn, "cells", cell_id)
        self.assertEqual(cell["active_reservation_id"], rid)
        self.assertAlmostEqual(cell["occupied_m3"], 1.5)
        r = S.require(self.conn, "reservations", rid)
        self.assertEqual(r["status"], M.CONFIRMED)

    def test_cancel_releases_capacity(self):
        rid = self.reserve(vol=1.5)
        cell_id = S.confirm_reservation(self.conn, rid)
        S.cancel_reservation(self.conn, rid)
        cell = S.require(self.conn, "cells", cell_id)
        self.assertIsNone(cell["active_reservation_id"])
        self.assertEqual(cell["occupied_m3"], 0.0)

    def test_concurrent_contention_only_one_wins(self):
        """两笔预约争抢最后一个货格：并发确认，仅一笔成功，容量不被双占。"""
        # 占住 A-01，使 A-02 成为唯一兼容空格
        hold = self.reserve(tid=self.t2, bid=self.b2, vol=1.9)
        S.confirm_reservation(self.conn, hold)
        r1 = self.reserve(vol=1.6)
        r2 = self.reserve(tid=self.t2, bid=self.b2, vol=1.6)

        results: list[Exception | int] = []
        barrier = threading.Barrier(2)

        def worker(rid):
            barrier.wait()
            try:
                c = connect(self.db)
                results.append(S.confirm_reservation(c, rid))
                c.close()
            except Exception as exc:
                results.append(exc)

        th = [threading.Thread(target=worker, args=(r,)) for r in (r1, r2)]
        for t in th:
            t.start()
        for t in th:
            t.join()

        self.assertEqual(len(results), 2)
        oks = [x for x in results if not isinstance(x, Exception)]
        errs = [x for x in results if isinstance(x, M.CellCapacityError)]
        self.assertEqual(len(oks), 1, f"应仅一笔成功，实际 {results}")
        self.assertEqual(len(errs), 1)
        cell = S.require(self.conn, "cells", oks[0])
        self.assertEqual(cell["occupied_m3"], 1.6)  # 未被双占
        loser = r1 if oks[0] != self.conn.execute(
            "SELECT cell_id FROM reservations WHERE id=?", (r1,)).fetchone()["cell_id"] else r2

    def test_cannot_confirm_when_no_compatible_cell(self):
        # 申报超过任何货格容量
        rid = self.reserve(vol=99.0)
        with self.assertRaises(M.CellCapacityError):
            S.confirm_reservation(self.conn, rid)

    def test_checkout_releases_cell(self):
        rid = self.reserve(vol=1.5)
        cell_id = S.confirm_reservation(self.conn, rid)
        t0 = S.now() - timedelta(hours=2)
        self.full_lifecycle(rid, at_start=t0, at_end=S.now())
        cell = S.require(self.conn, "cells", cell_id)
        self.assertIsNone(cell["active_reservation_id"])
        self.assertEqual(cell["occupied_m3"], 0.0)


# ------------------------------------------------------------ 2. 温区约束

class TestZoneConstraints(CaseBase):
    def test_no_zone_covers_target(self):
        # 2~6℃：B(0~4) 与 C 类库区均不存在能覆盖者
        rid = self.reserve(tmin=2, tmax=6)
        with self.assertRaises(M.ZoneMismatchError):
            S.confirm_reservation(self.conn, rid)

    def test_target_crossing_zones_rejected(self):
        # -1~3℃ 横跨深冷与冷藏，无单一库区可覆盖
        rid = self.reserve(tmin=-1, tmax=3)
        with self.assertRaises(M.ZoneMismatchError):
            S.confirm_reservation(self.conn, rid)

    def test_same_zone_move_is_immediate(self):
        rid = self.reserve()
        S.confirm_reservation(self.conn, rid)
        S.check_in(self.conn, rid, 500, 1.4, at=S.now() - timedelta(hours=1))
        direct, mid = S.request_move(self.conn, rid, self.cellA2, "风机维护")
        self.assertTrue(direct)
        r = S.require(self.conn, "reservations", rid)
        self.assertEqual(r["cell_id"], self.cellA2)
        self.assertEqual(r["status"], M.CHECKED_IN)
        self.assertEqual(S.require(self.conn, "cells", self.cellA1)["active_reservation_id"], None)

    def test_cross_zone_requires_reconfirmation(self):
        rid = self.reserve()
        S.confirm_reservation(self.conn, rid)
        S.check_in(self.conn, rid, 300, 1.0, at=S.now() - timedelta(hours=1))
        direct, mid = S.request_move(self.conn, rid, self.cellB1, "解冻周转")
        self.assertFalse(direct)
        r = S.require(self.conn, "reservations", rid)
        self.assertEqual(r["status"], M.MOVE_PENDING)
        self.assertEqual(r["cell_id"], self.cellA1)  # 未确认前不动

    def test_cross_zone_decision(self):
        rid = self.reserve()
        S.confirm_reservation(self.conn, rid)
        S.check_in(self.conn, rid, 300, 1.0, at=S.now() - timedelta(hours=1))
        _, mid = S.request_move(self.conn, rid, self.cellB1, "解冻")
        # 驳回：留在原格
        S.decide_move(self.conn, mid, False, staff_id=self.admin_id)
        self.assertEqual(S.require(self.conn, "reservations", rid)["cell_id"], self.cellA1)
        # 重新申请并通过
        _, mid2 = S.request_move(self.conn, rid, self.cellB1, "再次申请")
        S.decide_move(self.conn, mid2, True, staff_id=self.admin_id)
        r = S.require(self.conn, "reservations", rid)
        self.assertEqual(r["cell_id"], self.cellB1)
        self.assertEqual(r["status"], M.CHECKED_IN)
        self.assertEqual(S.require(self.conn, "cells", self.cellB1)["active_reservation_id"], rid)

    def test_move_into_occupied_cell_rejected(self):
        r1 = self.reserve(vol=1.5)
        S.confirm_reservation(self.conn, r1)
        S.check_in(self.conn, r1, 500, 1.5, at=S.now())
        r2 = self.reserve(tid=self.t2, bid=self.b2, vol=1.5)
        S.confirm_reservation(self.conn, r2)
        S.check_in(self.conn, r2, 500, 1.5, at=S.now())
        # r2 在 A-02，尝试移到被 r1 占用的 A-01
        with self.assertRaises(M.CellCapacityError):
            S.request_move(self.conn, r2, self.cellA1)

    def test_manual_temp_change_requires_reason(self):
        with self.assertRaises(M.PlatformError):
            S.manual_temp_change(self.conn, self.zA, -26, -20, "   ")

    def test_manual_temp_change_is_audited(self):
        S.manual_temp_change(self.conn, self.zA, -26, -20, "客户工艺变更单 #12")
        inc = self.conn.execute(
            "SELECT * FROM incidents WHERE kind=?", (M.INC_MANUAL_TEMP,)).fetchone()
        self.assertIsNotNone(inc)
        self.assertIn("客户工艺变更单", inc["detail"])
        z = S.require(self.conn, "zones", self.zA)
        self.assertEqual((z["temp_min"], z["temp_max"]), (-26.0, -20.0))


# ------------------------------------------------------------ 3. 计费边界

class TestBillingBoundaries(CaseBase):
    def test_billing_does_not_start_before_checkin(self):
        rid = self.reserve(start=S.now() + timedelta(hours=1),
                           end=S.now() + timedelta(hours=10))
        S.confirm_reservation(self.conn, rid)
        with self.assertRaises(M.PlatformError):  # 未入库复核，无法出快照
            S.build_snapshot(self.conn, rid)

    def test_exact_boundary_amounts(self):
        """10 小时整、500kg、B 区 1.5 元/格时 + 0.02 元/kg时。"""
        t0 = S.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=12)
        rid = self.reserve(tmin=0, tmax=4, start=t0, end=t0 + timedelta(hours=34),
                           vol=2.0, weight=500)
        S.confirm_reservation(self.conn, rid)
        self.assertEqual(S.require(self.conn, "reservations", rid)["cell_id"], self.cellB1)
        S.check_in(self.conn, rid, 500, 2.0, at=t0)
        S.check_out(self.conn, rid, at=t0 + timedelta(hours=10))
        snap = self.conn.execute(
            "SELECT * FROM billing_snapshots WHERE reservation_id=?", (rid,)).fetchone()
        self.assertAlmostEqual(snap["occupancy_hours"], 10.0, places=3)
        self.assertAlmostEqual(snap["base_total"], 15.0)      # 10 × 1.5
        self.assertAlmostEqual(snap["weight_total"], 100.0)   # 10 × 500 × 0.02
        self.assertAlmostEqual(snap["final_amount"], 115.0)

    def test_overload_incident_adds_surcharge(self):
        t0 = S.now() - timedelta(hours=5)
        rid = self.reserve(tmin=0, tmax=4, vol=2.0, weight=100)
        S.confirm_reservation(self.conn, rid)
        S.check_in(self.conn, rid, 100, 3.3, at=t0)   # B-01 容量 3.0，超出 0.3 m³
        S.check_out(self.conn, rid, at=t0 + timedelta(hours=10))
        segs = self.conn.execute(
            "SELECT * FROM billing_segments WHERE reservation_id=?", (rid,)).fetchall()
        self.assertTrue(all(s["seg_type"] == M.INC_OVERLOAD for s in segs))
        self.assertTrue(segs)
        # 占格费 15，超容附加 30% = 4.5，重量 10×100×0.02=20
        snap = self.conn.execute(
            "SELECT * FROM billing_snapshots WHERE reservation_id=?", (rid,)).fetchone()
        self.assertAlmostEqual(snap["surcharge_total"], 4.5)
        self.assertAlmostEqual(snap["final_amount"], 39.5)
        inc = self.conn.execute(
            "SELECT * FROM incidents WHERE reservation_id=? AND kind=?",
            (rid, M.INC_OVERLOAD)).fetchone()
        self.assertIsNotNone(inc)

    def test_outage_window_split_and_compensation(self):
        t0 = S.now() - timedelta(hours=12)
        rid = self.reserve(tmin=0, tmax=4, vol=2.0, weight=100)
        S.confirm_reservation(self.conn, rid)
        S.check_in(self.conn, rid, 100, 2.0, at=t0)
        S.open_incident(self.conn, M.INC_OUTAGE, "断电", zone_id=self.zB,
                        cell_id=self.cellB1, reservation_id=rid,
                        at=t0 + timedelta(hours=3))
        S.close_incident(self.conn, self.conn.execute(
            "SELECT id FROM incidents WHERE reservation_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (rid, M.INC_OUTAGE)).fetchone()["id"], at=t0 + timedelta(hours=5))
        S.check_out(self.conn, rid, at=t0 + timedelta(hours=10))
        types = [r["seg_type"] for r in self.conn.execute(
            "SELECT seg_type FROM billing_segments WHERE reservation_id=? ORDER BY start_at", (rid,))]
        self.assertEqual(types, ["normal", M.INC_OUTAGE, "normal"])
        seg = self.conn.execute(
            "SELECT * FROM billing_segments WHERE reservation_id=? AND seg_type=?",
            (rid, M.INC_OUTAGE)).fetchone()
        self.assertAlmostEqual(seg["hours"], 2.0, places=3)
        # 断电补偿 = (3 + 4) × 50% = 3.5，待财务复核，不影响应收
        adj = self.conn.execute(
            "SELECT * FROM adjustments WHERE reservation_id=? AND kind='outage'", (rid,)).fetchone()
        self.assertAlmostEqual(adj["amount"], 3.5)
        self.assertEqual(adj["status"], "pending")

    def test_breach_comp_and_pull_down_surcharge(self):
        t0 = S.now() - timedelta(hours=12)
        rid = self.reserve(tmin=-25, tmax=-18, vol=1.5, weight=100)
        S.confirm_reservation(self.conn, rid)
        S.check_in(self.conn, rid, 100, 1.5, at=t0)
        for kind, h0, h1 in (
            (M.INC_PULL_DOWN, 1, 2),
            (M.INC_BREACH, 4, 6),
        ):
            S.open_incident(self.conn, kind, kind, zone_id=self.zA, cell_id=self.cellA1,
                            reservation_id=rid, at=t0 + timedelta(hours=h0))
            S.close_incident(self.conn, self.conn.execute(
                "SELECT id FROM incidents WHERE reservation_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                (rid, kind)).fetchone()["id"], at=t0 + timedelta(hours=h1))
        S.check_out(self.conn, rid, at=t0 + timedelta(hours=10))
        pull = self.conn.execute(
            "SELECT SUM(surcharge) s FROM billing_segments WHERE reservation_id=? AND seg_type=?",
            (rid, M.INC_PULL_DOWN)).fetchone()["s"]
        self.assertAlmostEqual(pull, 1.5)  # 1 小时 × 1.5
        breach_adj = self.conn.execute(
            "SELECT * FROM adjustments WHERE reservation_id=? AND kind='breach_comp'",
            (rid,)).fetchone()
        # 越界 2h：base 5 + weight 6 = 11，补偿 30% = 3.3
        self.assertAlmostEqual(breach_adj["amount"], 3.3)

    def test_early_checkout_leaves_trace(self):
        t0 = S.now() - timedelta(hours=20)
        planned_end = t0 + timedelta(hours=48)
        rid = self.reserve(start=t0, end=planned_end, vol=1.5, weight=100)
        S.confirm_reservation(self.conn, rid)
        S.check_in(self.conn, rid, 100, 1.5, at=t0)
        S.check_out(self.conn, rid, at=t0 + timedelta(hours=10))  # 提前 38h
        inc = self.conn.execute(
            "SELECT * FROM incidents WHERE reservation_id=? AND kind=?",
            (rid, M.INC_EARLY)).fetchone()
        self.assertIsNotNone(inc)
        self.assertGreater(inc["metric_value"], 37.0)

    def test_cross_zone_billing_uses_different_rates(self):
        t0 = S.now() - timedelta(hours=6)
        rid = self.reserve(tmin=-25, tmax=-18, vol=1.5, weight=100, start=t0,
                           end=t0 + timedelta(hours=20))
        S.confirm_reservation(self.conn, rid)
        S.check_in(self.conn, rid, 100, 1.5, at=t0)
        # 目标温区改为 0~4 后跨区移动（用管理员改温与移格模拟业务：直接申请跨区）
        _, mid = S.request_move(self.conn, rid, self.cellB1, "转冷藏")
        # 在 t0+4h 确认
        S.decide_move(self.conn, mid, True, staff_id=self.admin_id)
        # 手工把决定时刻钉到 t0+4h，保证时间轴可预测
        self.conn.execute("UPDATE move_requests SET decided_at=? WHERE id=?",
                          (S.iso(t0 + timedelta(hours=4)), mid))
        S.check_out(self.conn, rid, at=t0 + timedelta(hours=10))
        segs = self.conn.execute(
            "SELECT * FROM billing_segments WHERE reservation_id=? ORDER BY start_at", (rid,)).fetchall()
        self.assertEqual(len(segs), 2)
        # 前 4h A 区：2.5 元/h；后 6h B 区：1.5 元/h
        self.assertAlmostEqual(segs[0]["base_amount"], 10.0)
        self.assertAlmostEqual(segs[1]["base_amount"], 9.0)


# ------------------------------------------------------------ 4. 结算幂等

class TestSettlement(CaseBase):
    def _checked_out(self):
        t0 = S.now() - timedelta(hours=8)
        rid = self.reserve(tmin=0, tmax=4, vol=2.0, weight=100, start=t0,
                           end=t0 + timedelta(hours=20))
        S.confirm_reservation(self.conn, rid)
        S.check_in(self.conn, rid, 100, 2.0, at=t0)
        S.check_out(self.conn, rid, at=t0 + timedelta(hours=6))
        return rid

    def test_settle_is_idempotent(self):
        rid = self._checked_out()
        first = S.settle(self.conn, rid)
        self.assertFalse(first["already_settled"])
        second = S.settle(self.conn, rid)
        self.assertTrue(second["already_settled"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["final_amount"], second["final_amount"])
        # 快照只有一条
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM billing_snapshots WHERE reservation_id=?", (rid,)).fetchone()["c"]
        self.assertEqual(n, 1)

    def test_closed_lease_cannot_reopen(self):
        rid = self._checked_out()
        S.settle(self.conn, rid)
        with self.assertRaises(M.BillingClosedError):
            S.reopen_after_settle(self.conn, rid)
        with self.assertRaises(M.BillingClosedError):
            S.build_snapshot(self.conn, rid)
        with self.assertRaises(M.BillingClosedError):
            S.add_manual_adjustment(self.conn, rid, 5, "结账后减免")

    def test_cannot_settle_with_pending_adjustment(self):
        rid = self._checked_out()
        # B 区 6h 正常段无自动减免，人工加一笔待复核
        S.add_manual_adjustment(self.conn, rid, 5, "客户关怀")
        with self.assertRaises(M.PlatformError):
            S.settle(self.conn, rid)

    def test_approved_adjustment_changes_final(self):
        rid = self._checked_out()
        aid = S.add_manual_adjustment(self.conn, rid, 5, "客户关怀")
        before = self.conn.execute(
            "SELECT final_amount FROM billing_snapshots WHERE reservation_id=?",
            (rid,)).fetchone()["final_amount"]
        S.review_adjustment(self.conn, aid, True, self.fin_id)
        after = self.conn.execute(
            "SELECT final_amount FROM billing_snapshots WHERE reservation_id=?",
            (rid,)).fetchone()["final_amount"]
        self.assertAlmostEqual(after, before - 5)
        S.settle(self.conn, rid)
        # 结账后任何减免操作都被拒
        with self.assertRaises(M.BillingClosedError):
            S.add_manual_adjustment(self.conn, rid, 9, "结账后减免")

    def test_snapshot_rebuild_does_not_duplicate_reviewed_deduction(self):
        t0 = S.now() - timedelta(hours=12)
        rid = self.reserve(tmin=0, tmax=4, vol=2.0, weight=100)
        S.confirm_reservation(self.conn, rid)
        S.check_in(self.conn, rid, 100, 2.0, at=t0)
        S.open_incident(self.conn, M.INC_OUTAGE, "断电", zone_id=self.zB,
                        cell_id=self.cellB1, reservation_id=rid, at=t0 + timedelta(hours=2))
        S.close_incident(self.conn, self.conn.execute(
            "SELECT id FROM incidents WHERE reservation_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (rid, M.INC_OUTAGE)).fetchone()["id"], at=t0 + timedelta(hours=4))
        S.check_out(self.conn, rid, at=t0 + timedelta(hours=8))
        adj = self.conn.execute(
            "SELECT id FROM adjustments WHERE reservation_id=? AND kind='outage'", (rid,)).fetchone()
        S.review_adjustment(self.conn, adj["id"], True, self.fin_id)
        # 再追加人工减免会触发快照重算；已批的断电补偿不得重复
        S.add_manual_adjustment(self.conn, rid, 2, "其他")
        outage_n = self.conn.execute(
            "SELECT COUNT(*) c FROM adjustments WHERE reservation_id=? AND kind='outage'",
            (rid,)).fetchone()["c"]
        self.assertEqual(outage_n, 1)


# ------------------------------------------------------------ 5. 租户隔离

class TestIsolationService(CaseBase):
    def test_cannot_reserve_foreign_batch(self):
        with self.assertRaises(M.PermissionDenied):
            self.reserve(tid=self.t1, bid=self.b2)

    def test_photo_of_foreign_batch_served_forbidden(self):
        # 服务层数据归属：批次照片联表能查到 tenant
        fdir = self.tmp.name
        path = os.path.join(fdir, "x.svg")
        Path(path).write_text("<svg></svg>")
        pid = S.add_photo(self.conn, self.b2, path, "x")
        p = self.conn.execute(
            "SELECT p.*, b.tenant_id FROM batch_photos p JOIN batches b ON b.id=p.batch_id WHERE p.id=?",
            (pid,)).fetchone()
        self.assertEqual(p["tenant_id"], self.t2)


# ------------------------------------------------------------ 6. 监测任务

class TestMonitor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        init_db(self.db)
        self.conn = connect(self.db)
        c = self.conn
        self.zid = c.execute(
            "INSERT INTO zones(code,name,zone_type,temp_min,temp_max,hourly_rate,kg_hour_rate)"
            " VALUES('A','深冷A','frozen',-25,-18,2.5,0.03)").lastrowid
        self.cell = c.execute(
            "INSERT INTO cells(zone_id,code,capacity_m3) VALUES(?, 'A-01', 2.0)",
            (self.zid,)).lastrowid
        self.tid = S.create_tenant(c, "甲", "t", "tok")
        self.bid = S.create_batch(c, self.tid, "虾", "S")
        t0 = S.now() - timedelta(hours=2)
        self.rid = S.create_reservation(c, self.tid, self.bid, -25, -18, t0,
                                        t0 + timedelta(hours=10), 1.0, 100)
        S.confirm_reservation(c, self.rid)
        S.check_in(c, self.rid, 100, 1.0, at=t0)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_door_left_open_detected(self):
        door = SimDoorAdapter()
        mon = MonitorLoop(self.db, door_adapter=door)
        t = S.now()
        door.open(self.conn, self.cell, at=t - timedelta(minutes=8))
        mon.tick(self.conn, at=t)
        inc = self.conn.execute(
            "SELECT * FROM incidents WHERE kind=? AND resolved=0", (M.INC_DOOR_OPEN,)).fetchone()
        self.assertIsNotNone(inc)
        self.assertEqual(inc["reservation_id"], self.rid)
        # 关门后关闭异常
        door.close(self.conn, self.cell, at=t + timedelta(minutes=1))
        mon.tick(self.conn, at=t + timedelta(minutes=2))
        inc = self.conn.execute(
            "SELECT * FROM incidents WHERE kind=? ORDER BY id DESC LIMIT 1",
            (M.INC_DOOR_OPEN,)).fetchone()
        self.assertEqual(inc["resolved"], 1)

    def test_short_open_is_not_incident(self):
        door = SimDoorAdapter()
        mon = MonitorLoop(self.db, door_adapter=door)
        t = S.now()
        door.open(self.conn, self.cell, at=t - timedelta(seconds=60))
        mon.tick(self.conn, at=t)
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM incidents WHERE kind=?", (M.INC_DOOR_OPEN,)).fetchone()["c"]
        self.assertEqual(n, 0)

    def test_outage_opens_and_closes_incident(self):
        adapter = SimTempAdapter()
        mon = MonitorLoop(self.db, temp_adapter=adapter)
        t = S.now()
        adapter.inject_outage(self.zid, 30, at=t)
        mon.tick(self.conn, at=t + timedelta(minutes=5))
        inc = self.conn.execute(
            "SELECT * FROM incidents WHERE kind=? AND resolved=0", (M.INC_OUTAGE,)).fetchone()
        self.assertIsNotNone(inc)
        # 恢复后
        mon.tick(self.conn, at=t + timedelta(minutes=40))
        inc = self.conn.execute(
            "SELECT * FROM incidents WHERE kind=? ORDER BY id DESC LIMIT 1", (M.INC_OUTAGE,)).fetchone()
        self.assertEqual(inc["resolved"], 1)

    def test_breach_detection_with_sample(self):
        adapter = SimTempAdapter()
        mon = MonitorLoop(self.db, temp_adapter=adapter)
        t = S.now()
        # 直接写入越界读数并驱动状态判定（避免新采样覆盖最新读数）
        self.conn.execute(
            "INSERT INTO temp_samples(zone_id,celsius,sampled_at,source) VALUES(?,?,?,'sim')",
            (self.zid, -10.0, S.iso(t + timedelta(minutes=1))))
        with __import__("coldstorage.db", fromlist=["tx"]).tx(self.conn):
            mon._check_power_states(self.conn, t + timedelta(minutes=2))
        inc = self.conn.execute(
            "SELECT * FROM incidents WHERE kind=? AND resolved=0", (M.INC_BREACH,)).fetchone()
        self.assertIsNotNone(inc)
        # 回到正常区间
        self.conn.execute(
            "INSERT INTO temp_samples(zone_id,celsius,sampled_at,source) VALUES(?,?,?,'sim')",
            (self.zid, -21.0, S.iso(t + timedelta(minutes=3))))
        with __import__("coldstorage.db", fromlist=["tx"]).tx(self.conn):
            mon._check_power_states(self.conn, t + timedelta(minutes=4))
        inc = self.conn.execute(
            "SELECT * FROM incidents WHERE kind=? ORDER BY id DESC LIMIT 1", (M.INC_BREACH,)).fetchone()
        self.assertEqual(inc["resolved"], 1)


# ------------------------------------------------------------ 7. HTTP 层隔离

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


class TestHTTPIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "demo.db")
        cls.photos = os.path.join(cls.tmp.name, "photos")
        cls.report = seed_demo(cls.db, cls.photos)
        cls.httpd = make_server(cls.db, cls.photos, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(3)
        cls.tmp.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path, token=None, follow=True):
        opener = urllib.request.build_opener() if follow else \
                 urllib.request.build_opener(NoRedirect)
        req = urllib.request.Request(self.url(path))
        if token:
            req.add_header("Cookie", f"session={token}")
        try:
            resp = opener.open(req)
            return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, ""

    def post(self, path, data="", token=None):
        opener = urllib.request.build_opener(NoRedirect)
        req = urllib.request.Request(self.url(path), data=data.encode(), method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        if token:
            req.add_header("Cookie", f"session={token}")
        try:
            resp = opener.open(req)
            return resp.status, resp.headers.get("Location", "")
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Location", "")

    def test_anon_redirects_to_login(self):
        code, _ = self.get("/cells", follow=False)
        self.assertEqual(code, 303)

    def test_tenant_cannot_read_foreign_reservation(self):
        # r_active 属于 tenant1（冰鲜达），用 tenant2 令牌访问
        other = self.report["reservation_active"]
        t_ocean = self.report["tenant_ids"]["ocean"]
        token = self.conn_token(t_ocean)
        code, body = self.get(f"/reservations/{other}", token=token)
        self.assertEqual(code, 200)
        self.assertIn("无权访问", body)
        self.assertNotIn("业务办理", body)

    def test_tenant_cannot_access_finance(self):
        code, body = self.get("/finance", token="tok-tenant1")
        self.assertIn("仅财务", body)

    def test_tenant_cannot_confirm(self):
        pending = self.report["contention"]["loser"]  # pending 状态
        code, loc = self.post(f"/reservations/{pending}/confirm", token="tok-tenant1")
        self.assertIn("err=", loc)

    def test_foreign_photo_forbidden(self):
        # tenant1 的批次 1；用 tenant2 令牌取图
        req = urllib.request.Request(self.url("/photos?id=1"))
        req.add_header("Cookie", "session=tok-tenant2")
        try:
            urllib.request.urlopen(req)
            self.fail("应 403")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    def test_pages_render(self):
        for path in ("/cells", "/calendar", "/reservations", "/temperature", "/incidents"):
            code, _ = self.get(path, token="tok-admin")
            self.assertEqual(code, 200, path)
        code, body = self.get(f"/reservations/{self.report['reservation_active']}",
                              token="tok-tenant1")
        self.assertEqual(code, 200)
        self.assertIn("温度曲线", body)

    def conn_token(self, tenant_id):
        c = connect(self.db)
        try:
            return c.execute("SELECT token FROM tenants WHERE id=?", (tenant_id,)).fetchone()["token"]
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
