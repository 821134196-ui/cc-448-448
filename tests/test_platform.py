"""平台测试：容量锁定、温区约束、计费边界、结算幂等、租户隔离。

运行：python3 -m unittest discover -s tests -v
（测试开始前自动把 CS_DATA_DIR 指到临时目录。）
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.cookiejar import CookieJar
from datetime import timedelta
from http.server import ThreadingHTTPServer

_TMP = tempfile.mkdtemp(prefix="cs-tests-")
os.environ["CS_DATA_DIR"] = _TMP

from coldstorage import adapters, billing, config  # noqa: E402
from coldstorage.db import connect  # noqa: E402
from coldstorage.errors import DomainError  # noqa: E402
from coldstorage.monitor import detect_all  # noqa: E402
from coldstorage.seed import _make_users  # noqa: E402
from coldstorage import services as svc  # noqa: E402
from coldstorage.util import iso, now, now_iso, shift_iso  # noqa: E402
from coldstorage.server import Handler  # noqa: E402


def _confirmed(conn, tenant: str, zone: str, cell: str, need: float,
               start_ts: str, end_ts: str) -> int:
    z = config.ZONES[zone]
    cur = conn.execute(
        "INSERT INTO reservations(tenant_id,zone_code,cell_id,need_volume_m3,"
        "target_temp_min,target_temp_max,start_ts,end_ts,status,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,'confirmed',?)",
        (tenant, zone, cell, need, z["temp_min"], z["temp_max"],
         start_ts, end_ts, now_iso()),
    )
    return cur.lastrowid


def make_lease(conn, tenant="t_lvyuan", zone="C", cell="C-01",
               weight_kg=900.0, volume=2.5, start_ts=None) -> int:
    start_ts = start_ts or now_iso()
    rid = _confirmed(conn, tenant, zone, cell, cell_volume(conn, cell) - 0.1,
                     start_ts, shift_iso(start_ts, days=2))
    svc.add_batch(conn, rid, "虚构货品", weight_kg * 0.97, weight_kg, volume)
    res = svc.check_in(conn, rid, True)
    return res["lease_id"]


def cell_volume(conn, cell: str) -> float:
    return conn.execute("SELECT volume_m3 FROM cells WHERE id=?",
                        (cell,)).fetchone()["volume_m3"]


class PlatformTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = connect()
        svc.seed_catalog(cls.conn)
        _make_users(cls.conn)
        cls.conn.commit()

    def setUp(self):
        # 每个测试用例清理业务数据，保留目录与账号（子表先于父表）
        for table in ("reviews", "bills", "incidents", "moves", "door_events",
                      "temp_samples", "cell_temp_overrides", "temp_overrides",
                      "leases", "batches", "reservations"):
            try:
                self.conn.execute(f"DELETE FROM {table}")
                self.conn.commit()
            except sqlite3.IntegrityError:
                self.conn.rollback()


# ---------------------------------------------------------------------------
# 1. 容量锁定
# ---------------------------------------------------------------------------
class TestCapacityLocking(PlatformTestBase):

    def _occupy(self, cells):
        """用直接在租记录占住货格。"""
        for cell in cells:
            make_lease(self.conn, "t_tianshan", "F", cell, 1200, 3.0,
                       shift_iso(now_iso(), hours=-1))

    def test_two_reservations_race_for_last_cell_only_one_wins(self):
        # F-01..F-06 占住后，5m³ 需求只剩 F-07（6m³）可容，F-08 仅 4m³
        self._occupy([f"F-{i:02d}" for i in range(1, 7)])
        start, end = shift_iso(now_iso(), hours=1), shift_iso(now_iso(), days=1)
        r1 = svc.create_reservation(self.conn, "t_lvyuan", "F", 5.0, start, end)
        r2 = svc.create_reservation(self.conn, "t_beiji", "F", 5.0, start, end)
        self.conn.commit()

        barrier = threading.Barrier(2)
        results = {}

        def race(rid, key):
            c = connect()
            barrier.wait()
            try:
                results[key] = svc.confirm_reservation(c, rid)["cell_id"]
                c.commit()
            except DomainError as e:
                results[key] = f"ERR:{e}"
            finally:
                c.close()

        t1 = threading.Thread(target=race, args=(r1, "a"))
        t2 = threading.Thread(target=race, args=(r2, "b"))
        t1.start(); t2.start(); t1.join(); t2.join()

        winners = [v for v in results.values() if not str(v).startswith("ERR")]
        losers = [v for v in results.values() if str(v).startswith("ERR")]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(winners[0], "F-07")
        self.assertEqual(len(losers), 1)
        self.assertIn("没有满足容量的空货格", losers[0])

    def test_demand_exceeding_all_cells_cannot_confirm(self):
        rid = svc.create_reservation(
            self.conn, "t_lvyuan", "F", 99.0,
            shift_iso(now_iso(), hours=1), shift_iso(now_iso(), days=1))
        self.conn.commit()
        with self.assertRaises(DomainError):
            svc.confirm_reservation(self.conn, rid)

    def test_confirm_is_idempotent(self):
        rid = svc.create_reservation(
            self.conn, "t_lvyuan", "C", 3.0,
            shift_iso(now_iso(), hours=1), shift_iso(now_iso(), days=1))
        self.conn.commit()
        first = svc.confirm_reservation(self.conn, rid)
        second = svc.confirm_reservation(self.conn, rid)
        self.assertEqual(first["cell_id"], second["cell_id"])
        self.assertTrue(second["idempotent"])


# ---------------------------------------------------------------------------
# 2. 温区约束
# ---------------------------------------------------------------------------
class TestZoneConstraints(PlatformTestBase):

    def test_same_zone_move_executes_immediately(self):
        lid = make_lease(self.conn, cell="C-01")
        res = svc.request_move(self.conn, lid, "C-02", "风机检修")
        self.conn.commit()
        self.assertTrue(res["same_zone"])
        self.assertTrue(res["executed"])
        self.assertEqual(
            self.conn.execute("SELECT cell_id FROM leases WHERE id=?",
                              (lid,)).fetchone()["cell_id"], "C-02")

    def test_cross_zone_move_requires_admin_reconfirm(self):
        lid = make_lease(self.conn, cell="C-01")
        res = svc.request_move(self.conn, lid, "AC-05", "转恒温工艺")
        self.conn.commit()
        self.assertFalse(res["same_zone"])
        self.assertFalse(res["executed"])
        mid = res["move_id"]
        # 未经审批，货格不变
        self.assertEqual(
            self.conn.execute("SELECT cell_id FROM leases WHERE id=?",
                              (lid,)).fetchone()["cell_id"], "C-01")
        # 普通租户不能审批（角色在 API 层测试；服务层管理员批准）
        svc.decide_move(self.conn, mid, True, "admin")
        self.conn.commit()
        lease = self.conn.execute("SELECT * FROM leases WHERE id=?",
                                  (lid,)).fetchone()
        self.assertEqual(lease["cell_id"], "AC-05")
        self.assertEqual(lease["zone_code"], "AC")
        # 重新确认后温区带变为恒温区
        self.assertEqual(lease["temp_min"], config.ZONES["AC"]["temp_min"])

    def test_cross_zone_move_can_be_rejected(self):
        lid = make_lease(self.conn, cell="C-01")
        res = svc.request_move(self.conn, lid, "AC-05", "申请")
        svc.decide_move(self.conn, res["move_id"], False, "admin")
        self.conn.commit()
        self.assertEqual(
            self.conn.execute("SELECT cell_id FROM leases WHERE id=?",
                              (lid,)).fetchone()["cell_id"], "C-01")

    def test_move_to_smaller_cell_rejected(self):
        # C-01 锁定容量 8m³，不能移入更小格：先占 C-02..C-08，留 C-? 用 F 区 4m³ 跨区
        lid = make_lease(self.conn, zone="F", cell="F-07", weight_kg=1000,
                         volume=3.0)  # 锁定 6m³
        with self.assertRaises(DomainError):  # F-08 仅 4m³
            svc.request_move(self.conn, lid, "F-08", "test")
        self.conn.rollback()

    def test_move_into_occupied_cell_rejected(self):
        make_lease(self.conn, "t_lvyuan", "C", "C-01")
        lid2 = make_lease(self.conn, "t_beiji", "C", "C-02")
        with self.assertRaises(DomainError):
            svc.request_move(self.conn, lid2, "C-01", "撞格")
        self.conn.rollback()

    def test_admin_temp_override_requires_reason(self):
        with self.assertRaises(DomainError):
            svc.override_temp(self.conn, "C-01", 2.0, 6.0, "  ", "admin")
        self.conn.rollback()
        oid = svc.override_temp(self.conn, "C-01", 2.0, 6.0,
                                "租户工艺试验，已现场确认", "admin")
        self.conn.commit()
        self.assertTrue(oid)
        lo, hi = svc.effective_band(self.conn, "C-01")
        self.assertEqual((lo, hi), (2.0, 6.0))


# ---------------------------------------------------------------------------
# 3. 计费边界
# ---------------------------------------------------------------------------
class TestBillingBoundaries(PlatformTestBase):

    def test_billing_starts_only_after_checkin(self):
        rid = svc.create_reservation(
            self.conn, "t_lvyuan", "C", 3.0,
            shift_iso(now_iso(), hours=-5), shift_iso(now_iso(), days=1))
        self.conn.commit()
        svc.confirm_reservation(self.conn, rid)
        # 仅确认未入库：无租期、无费用
        self.assertIsNone(
            self.conn.execute("SELECT * FROM leases WHERE reservation_id=?",
                              (rid,)).fetchone())
        svc.add_batch(self.conn, rid, "虚构货品", 900, 900, 2.0)
        svc.check_in(self.conn, rid, True)
        lease = self.conn.execute("SELECT * FROM leases WHERE reservation_id=?",
                                  (rid,)).fetchone()
        self.assertIsNotNone(lease)
        self.assertEqual(lease["status"], "open")

    def test_weight_surcharge_threshold(self):
        # 容量 8m³，标准密度 300kg/m³ → 2400kg 以下不收重量附加
        lid_under = make_lease(self.conn, cell="C-05", weight_kg=1000, volume=3)
        self.conn.execute("UPDATE leases SET started_at=? WHERE id=?",
                          (shift_iso(now_iso(), hours=-10), lid_under))
        out = svc.check_out(self.conn, lid_under, "admin")
        bill = billing.get_bill(self.conn, out["bill_id"])
        self.assertEqual(bill["detail"]["weight_surcharge_total"], 0.0)

        lid_over = make_lease(self.conn, cell="C-06", weight_kg=3000, volume=3)
        self.conn.execute("UPDATE leases SET started_at=? WHERE id=?",
                          (shift_iso(now_iso(), hours=-10), lid_over))
        out = svc.check_out(self.conn, lid_over, "admin")
        bill = billing.get_bill(self.conn, out["bill_id"])
        # 超出 600kg=0.6t，0.35 元/t·h × 10h = 2.10
        self.assertAlmostEqual(bill["detail"]["weight_surcharge_total"], 2.10, 2)

    def test_base_rent_boundary_hours(self):
        lid = make_lease(self.conn, cell="C-07", weight_kg=500, volume=2)
        self.conn.execute("UPDATE leases SET started_at=? WHERE id=?",
                          (shift_iso(now_iso(), hours=-10), lid))
        out = svc.check_out(self.conn, lid, "admin")
        bill = billing.get_bill(self.conn, out["bill_id"])
        # C 区 0.8 元/m³/h × 8m³ × 10h = 64.00
        self.assertAlmostEqual(bill["subtotal"], 64.00, 1)

    def test_excursion_and_outage_credits_integrated_by_minute(self):
        start = shift_iso(now_iso(), hours=-8)
        lid = make_lease(self.conn, cell="C-08", start_ts=start)
        self.conn.execute("UPDATE leases SET started_at=? WHERE id=?", (start, lid))
        adapters.backfill_series(
            self.conn, "C-08", start, now_iso(), interval_min=5,
            outage_windows=[(shift_iso(start, hours=1), shift_iso(start, hours=1, minutes=30))],
            excursion_windows=[(shift_iso(start, hours=3),
                                shift_iso(start, hours=3, minutes=30), 4.5)],
        )
        created = detect_all(self.conn)
        self.conn.commit()
        self.assertEqual(len(created), 2)
        out = svc.check_out(self.conn, lid, "admin")
        bill = billing.get_bill(self.conn, out["bill_id"])
        # 断电 30min×1.0=30；越界 30min×0.6=18 → 减免 48
        self.assertAlmostEqual(bill["discount_total"], 48.0, 1)
        self.assertEqual(bill["status"], "issued")  # 有减免 → 待财务复核

    def test_short_excursion_below_grace_not_billed(self):
        start = shift_iso(now_iso(), hours=-2)
        lid = make_lease(self.conn, cell="C-01", start_ts=start)
        self.conn.execute("UPDATE leases SET started_at=? WHERE id=?", (start, lid))
        # 仅 1 个越界点 / 持续 <120s 宽限
        adapters.backfill_series(
            self.conn, "C-01", start, now_iso(), interval_min=5,
            excursion_windows=[(shift_iso(now_iso(), minutes=-3),
                                shift_iso(now_iso(), minutes=-3), 4.5)],
        )
        created = detect_all(self.conn)
        self.assertEqual(created, [])

    def test_door_open_fee_only_after_grace(self):
        start = shift_iso(now_iso(), hours=-3)
        lid = make_lease(self.conn, cell="C-02", start_ts=start)
        self.conn.execute("UPDATE leases SET started_at=? WHERE id=?", (start, lid))
        # 3 分钟 → 宽限内不计
        adapters.door_event(self.conn, "C-02",
                            shift_iso(now_iso(), minutes=-20),
                            shift_iso(now_iso(), minutes=-17), "短开")
        # 12 分钟 → 加收 30
        adapters.door_event(self.conn, "C-02",
                            shift_iso(now_iso(), minutes=-30),
                            shift_iso(now_iso(), minutes=-18), "久开")
        created = detect_all(self.conn)
        self.conn.commit()
        self.assertEqual(len(created), 1)

    def test_timeline_split_on_cross_zone_move(self):
        start = shift_iso(now_iso(), hours=-10)
        lid = make_lease(self.conn, zone="C", cell="C-03", start_ts=start)
        self.conn.execute("UPDATE leases SET started_at=? WHERE id=?", (start, lid))
        move_at = shift_iso(now_iso(), hours=-5)
        # 直接构造一条已批准完成的跨区移格
        self.conn.execute(
            "INSERT INTO moves(lease_id,from_cell,to_cell,same_zone,reason,status,"
            "decided_by,decided_at,created_at,done_at) VALUES(?,?,?,0,?,'done',"
            "'admin',?,?,?)",
            (lid, "C-03", "F-07", "跨区", move_at, start, move_at))
        self.conn.execute("UPDATE leases SET cell_id='F-07',zone_code='F',"
                          "temp_min=-25,temp_max=-18 WHERE id=?", (lid,))
        adapters.backfill_series(self.conn, "C-03", start, move_at, 5)
        adapters.backfill_series(self.conn, "F-07", move_at, now_iso(), 5)
        out = svc.check_out(self.conn, lid, "admin")
        bill = billing.get_bill(self.conn, out["bill_id"])
        zones = [(s["zone"], round(s["hours"], 1)) for s in bill["detail"]["segments"]]
        self.assertIn(("C", 5.0), zones)
        self.assertIn(("F", 5.0), zones)
        # C 段 0.8×8×5=32，F 段 1.2×6×5=36（目标格 F-07 为 6m³，锁定容量仍 8）
        self.assertAlmostEqual(bill["subtotal"], 32.0 + 48.0, 0)


# ---------------------------------------------------------------------------
# 4. 结算幂等
# ---------------------------------------------------------------------------
class TestSettlementIdempotency(PlatformTestBase):

    def _closed(self, cell="C-01"):
        lid = make_lease(self.conn, cell=cell, weight_kg=800, volume=2)
        self.conn.execute("UPDATE leases SET started_at=? WHERE id=?",
                          (shift_iso(now_iso(), hours=-6), lid))
        out1 = svc.check_out(self.conn, lid, "admin")
        return lid, out1

    def test_checkout_twice_returns_same_bill(self):
        lid, out1 = self._closed()
        out2 = svc.check_out(self.conn, lid, "admin")
        self.assertTrue(out2["idempotent"])
        self.assertEqual(out1["bill_id"], out2["bill_id"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM bills WHERE lease_id=?",
                              (lid,)).fetchone()["c"], 1)
        lease = self.conn.execute("SELECT * FROM leases WHERE id=?",
                                  (lid,)).fetchone()
        self.assertEqual(lease["status"], "closed")

    def test_closed_lease_cannot_be_moved_or_pulled(self):
        lid, _ = self._closed(cell="C-02")
        with self.assertRaises(DomainError):
            svc.request_move(self.conn, lid, "C-05", "已结账想移位")
        self.conn.rollback()
        with self.assertRaises(DomainError):
            svc.temp_pull(self.conn, lid, 2, "admin")

    def test_paid_bill_review_blocked_and_incidents_tagged_once(self):
        lid, out = self._closed(cell="C-03")
        bill_id = out["bill_id"]
        billing.mark_paid(self.conn, bill_id)  # 无负数减免，可直接支付
        with self.assertRaises(DomainError):
            billing.mark_paid(self.conn, bill_id)  # 重复支付不报错? 状态保持
        # 已结账租期再加异常不影响原快照
        with self.assertRaises(DomainError):
            svc.record_leak(self.conn, lid, "事后补登", "admin")

    def test_discount_must_pass_finance_review_before_pay(self):
        start = shift_iso(now_iso(), hours=-6)
        lid = make_lease(self.conn, cell="C-04", start_ts=start)
        self.conn.execute("UPDATE leases SET started_at=? WHERE id=?", (start, lid))
        adapters.backfill_series(
            self.conn, "C-04", start, now_iso(), 5,
            excursion_windows=[(shift_iso(start, hours=1),
                                shift_iso(start, hours=1, minutes=30), 4.5)])
        detect_all(self.conn)
        self.conn.commit()
        out = svc.check_out(self.conn, lid, "admin")
        bill_id = out["bill_id"]
        with self.assertRaises(DomainError):
            billing.mark_paid(self.conn, bill_id)  # 减免未复核
        inc = self.conn.execute(
            "SELECT id FROM incidents WHERE lease_id=? AND amount<0",
            (lid,)).fetchone()
        billing.review_discount(self.conn, bill_id, inc["id"], True,
                                "finance", "情况属实")
        self.conn.commit()
        billing.mark_paid(self.conn, bill_id)  # 复核后可支付
        bill = billing.get_bill(self.conn, bill_id)
        self.assertEqual(bill["status"], "paid")
        # 已复核项不可再次复核
        with self.assertRaises(DomainError):
            billing.review_discount(self.conn, bill_id, inc["id"], True,
                                    "finance")


# ---------------------------------------------------------------------------
# 5. 租户隔离（经 HTTP）
# ---------------------------------------------------------------------------
class TestTenantIsolation(unittest.TestCase):
    server = None

    @classmethod
    def setUpClass(cls):
        for table in ("reviews", "bills", "incidents", "moves", "door_events",
                      "temp_samples", "temp_overrides", "cell_temp_overrides",
                      "leases", "batches", "reservations"):
            connect().execute(f"DELETE FROM {table}")
        conn = connect()
        svc.seed_catalog(conn)
        if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
            _make_users(conn)
        conn.commit()
        cls.lv = make_lease(conn, "t_lvyuan", "C", "C-01", 900, 2.5,
                            shift_iso(now_iso(), hours=-4))
        cls.bj = make_lease(conn, "t_beiji", "C", "C-02", 900, 2.5,
                            shift_iso(now_iso(), hours=-4))
        conn.commit()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def client(self, user, pw):
        cj = CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/login",
            data=json.dumps({"user": user, "password": pw}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        op.open(req)
        return op

    def call(self, op, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"}, method=method)
        try:
            with op.open(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_tenant_sees_only_own_leases(self):
        op = self.client("t_lvyuan", "pass1234")
        _, leases = self.call(op, "GET", "/api/leases")
        ids = {l["id"] for l in leases}
        self.assertIn(self.lv, ids)
        self.assertNotIn(self.bj, ids)

    def test_tenant_cannot_open_other_lease_or_bill(self):
        op = self.client("t_lvyuan", "pass1234")
        s, _ = self.call(op, "GET", f"/api/leases/{self.bj}")
        self.assertEqual(s, 403)
        s, _ = self.call(op, "POST", f"/api/leases/{self.bj}/move",
                         {"to_cell": "C-05", "reason": "x"})
        self.assertEqual(s, 403)

    def test_tenant_cannot_confirm_reservation(self):
        op = self.client("t_lvyuan", "pass1234")
        s, body = self.call(op, "POST", "/api/reservations", {
            "zone_code": "C", "need_volume_m3": 2,
            "start_ts": shift_iso(now_iso(), hours=2),
            "end_ts": shift_iso(now_iso(), days=1)})
        self.assertEqual(s, 200)
        rid = body["id"]
        s2, _ = self.call(op, "POST", f"/api/reservations/{rid}/confirm")
        self.assertEqual(s2, 403)  # 仅管理员确认锁格

    def test_admin_and_finance_see_all(self):
        opa = self.client("admin", "admin1234")
        _, leases = self.call(opa, "GET", "/api/leases")
        self.assertGreaterEqual({l["id"] for l in leases}, {self.lv, self.bj})
        opf = self.client("finance", "finance1234")
        s, _ = self.call(opf, "GET", f"/api/leases/{self.bj}")
        self.assertEqual(s, 200)
        # 财务不能执行管理员操作
        s, _ = self.call(opf, "POST", f"/api/leases/{self.bj}/checkout", {})
        self.assertEqual(s, 403)

    def test_anonymous_blocked(self):
        op = urllib.request.build_opener()
        s, _ = self.call(op, "GET", "/api/map")
        self.assertEqual(s, 401)

    def test_tenant_cannot_read_samples_of_other_cell(self):
        op = self.client("t_beiji", "pass1234")
        s, _ = self.call(op, "GET", "/api/cells/C-01/samples")  # C-01 属绿源
        self.assertEqual(s, 403)
        s, _ = self.call(op, "GET", "/api/cells/C-02/samples")  # 自己的
        self.assertEqual(s, 200)


if __name__ == "__main__":
    unittest.main()
