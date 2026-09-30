"""HTTP 服务：JSON API + 静态前端，仅用标准库。

鉴权：登录后下发随机会话 Cookie（内存），角色 admin / finance / tenant。
租户隔离：tenant 角色访问预约、租期、账单、货格详情时强制按 tenant_id 过滤。
"""
from __future__ import annotations

import base64
import json
import secrets
import sqlite3
import threading
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from . import adapters, billing, config
from .db import connect
from .errors import DomainError
from .monitor import detect_all, occupancy_intervals
from .services import (add_batch, cancel_reservation, cell_map, check_in,
                       check_out, confirm_reservation, create_reservation,
                       decide_move, override_temp, record_leak, request_move,
                       seed_catalog, temp_pull)
from .util import (hash_password, hours_between, iso, minutes_between, now,
                   now_iso, parse, shift_iso, verify_password)

STATIC_DIR = Path(__file__).resolve().parent / "static"

_SESSIONS: dict[str, dict] = {}
_SESSION_LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ---- 请求/响应封装 ----------------------------------------------------------

class Ctx:
    def __init__(self, handler, match: dict):
        self.h = handler
        self.match = match
        self.user: dict | None = None
        self.body: dict = {}

    def q(self, key: str, default: str = "") -> str:
        return self.h.query.get(key, [default])[0]


Handler = Callable[[Ctx, sqlite3.Connection], object]

_ROUTES: list[tuple[str, str, Handler, set]] = []


def route(method: str, pattern: str, roles: set | None = None):
    def deco(fn: Handler) -> Handler:
        _ROUTES.append((method, pattern, fn, roles or set()))
        return fn
    return deco


def _match(pattern: str, path: str) -> dict | None:
    pp, qp = pattern.strip("/").split("/") if pattern.strip("/") else ("", ""), None
    parts = [p for p in pattern.strip("/").split("/") if True]
    segs = [s for s in path.strip("/").split("/") if s != ""]
    if len(parts) != len(segs):
        return None
    out = {}
    for p, s in zip(parts, segs):
        if p.startswith("{") and p.endswith("}"):
            out[p[1:-1]] = s
        elif p != s:
            return None
    return out


# ---- 鉴权与租户过滤 ----------------------------------------------------------

def _current_user(conn: sqlite3.Connection, token: str | None) -> dict | None:
    if not token:
        return None
    with _SESSION_LOCK:
        return _SESSIONS.get(token)


def _require(ctx: Ctx, roles: set) -> dict:
    if not ctx.user:
        raise ApiError(401, "请先登录")
    if roles and ctx.user["role"] not in roles:
        raise ApiError(403, "当前角色无权执行此操作")
    return ctx.user


def _tenant_row(conn: sqlite3.Connection, table: str, rid: int,
                id_col: str = "id") -> sqlite3.Row | None:
    return conn.execute(f"SELECT * FROM {table} WHERE {id_col}=?", (rid,)).fetchone()


def _own_reservation(conn: sqlite3.Connection, user: dict, rid: int) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
    if r is None:
        raise ApiError(404, "预约不存在")
    if user["role"] == "tenant" and r["tenant_id"] != user["tenant_id"]:
        raise ApiError(403, "租户只能查看自己的预约")
    return r


def _own_lease(conn: sqlite3.Connection, user: dict, lease_id: int) -> sqlite3.Row:
    lease = conn.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
    if lease is None:
        raise ApiError(404, "租期不存在")
    r = conn.execute("SELECT * FROM reservations WHERE id=?",
                     (lease["reservation_id"],)).fetchone()
    if user["role"] == "tenant" and r["tenant_id"] != user["tenant_id"]:
        raise ApiError(403, "租户只能查看自己的货格")
    return lease


# ---- 认证 -------------------------------------------------------------------

@route("POST", "/api/login")
def login(ctx: Ctx, conn):
    uid = str(ctx.body.get("user", "")).strip()
    pw = str(ctx.body.get("password", ""))
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row or not verify_password(pw, row["pw_hash"]):
        raise ApiError(401, "账号或密码错误")
    token = secrets.token_urlsafe(24)
    user = {"id": row["id"], "name": row["display_name"],
            "role": row["role"], "tenant_id": row["tenant_id"] or None}
    with _SESSION_LOCK:
        _SESSIONS[token] = user
    ctx.h.extra_cookie = f"session={token}; Path=/; HttpOnly; SameSite=Lax"
    return user


@route("POST", "/api/logout")
def logout(ctx: Ctx, conn):
    token = ctx.h.cookie.get("session") if ctx.h.cookie else None
    if token:
        with _SESSION_LOCK:
            _SESSIONS.pop(token.value, None)
    ctx.h.extra_cookie = "session=; Path=/; Max-Age=0"
    return {"ok": True}


@route("GET", "/api/me")
def me(ctx: Ctx, conn):
    return ctx.user or {"role": "anonymous"}


# ---- 格位图 / 日历 -----------------------------------------------------------

@route("GET", "/api/map", {"admin", "finance", "tenant"})
def api_map(ctx: Ctx, conn):
    data = cell_map(conn)
    # 租户视角：其他租户的在租/已预约货格只显示占用，不暴露身份
    if ctx.user["role"] == "tenant":
        tid = ctx.user["tenant_id"]
        for z in data:
            for c in z["cells"]:
                if c["status"] == "free":
                    continue
                ref = conn.execute(
                    "SELECT r.tenant_id FROM reservations r WHERE r.cell_id=?"
                    " AND r.status IN ('confirmed','checked_in')"
                    " ORDER BY CASE r.status WHEN 'checked_in' THEN 0 ELSE 1 END",
                    (c["id"],)).fetchone()
                if not ref or ref["tenant_id"] != tid:
                    c["who"] = "占用"
                    c["lease_id"] = None
    return data


@route("GET", "/api/calendar", {"admin", "finance", "tenant"})
def api_calendar(ctx: Ctx, conn):
    frm = ctx.q("from", shift_iso(now_iso(), days=-2))
    to = ctx.q("to", shift_iso(now_iso(), days=10))
    sql = ("SELECT r.*, t.name tenant_name FROM reservations r JOIN tenants t"
           " ON t.id=r.tenant_id WHERE r.end_ts>=? AND r.start_ts<=?"
           " AND r.status!='cancelled' ORDER BY r.start_ts")
    rows = [dict(r) for r in conn.execute(sql, (frm, to)).fetchall()]
    if ctx.user["role"] == "tenant":
        for r in rows:
            if r["tenant_id"] != ctx.user["tenant_id"]:
                r["tenant_name"] = "其他租户"
                r["cell_id"] = None
                r["need_volume_m3"] = None
                r["note"] = ""
    return rows


# ---- 预约 -------------------------------------------------------------------

@route("GET", "/api/reservations", {"admin", "finance", "tenant"})
def list_reservations(ctx: Ctx, conn):
    sql = ("SELECT r.*, t.name tenant_name FROM reservations r"
           " JOIN tenants t ON t.id=r.tenant_id")
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY r.id DESC").fetchall()]
    if ctx.user["role"] == "tenant":
        rows = [r for r in rows if r["tenant_id"] == ctx.user["tenant_id"]]
    return rows


@route("POST", "/api/reservations", {"admin", "tenant"})
def make_reservation(ctx: Ctx, conn):
    user = _require(ctx, {"admin", "tenant"})
    b = ctx.body
    tenant_id = b.get("tenant_id") if user["role"] == "admin" else user["tenant_id"]
    rid = create_reservation(
        conn, tenant_id, b["zone_code"], float(b["need_volume_m3"]),
        b["start_ts"], b["end_ts"], b.get("note", ""))
    conn.commit()
    return {"id": rid}


@route("GET", "/api/reservations/{rid}", {"admin", "finance", "tenant"})
def get_reservation(ctx: Ctx, conn):
    rid = int(ctx.match["rid"])
    r = _own_reservation(conn, ctx.user, rid)
    d = dict(r)
    d["batches"] = [dict(b) for b in conn.execute(
        "SELECT * FROM batches WHERE reservation_id=? ORDER BY id", (rid,))]
    lease = conn.execute("SELECT * FROM leases WHERE reservation_id=?",
                         (rid,)).fetchone()
    d["lease"] = dict(lease) if lease else None
    return d


@route("POST", "/api/reservations/{rid}/confirm", {"admin"})
def do_confirm(ctx: Ctx, conn):
    rid = int(ctx.match["rid"])
    res = confirm_reservation(conn, rid)
    conn.commit()
    return res


@route("POST", "/api/reservations/{rid}/cancel", {"admin", "tenant"})
def do_cancel(ctx: Ctx, conn):
    rid = int(ctx.match["rid"])
    _own_reservation(conn, ctx.user, rid)
    cancel_reservation(conn, rid)
    conn.commit()
    return {"ok": True}


# ---- 入库复核 ---------------------------------------------------------------

@route("POST", "/api/reservations/{rid}/batches", {"admin", "tenant"})
def add_batch_api(ctx: Ctx, conn):
    rid = int(ctx.match["rid"])
    r = _own_reservation(conn, ctx.user, rid)
    b = ctx.body
    photo_path = None
    if b.get("photo_base64"):
        config.ensure_dirs()
        raw = base64.b64decode(b["photo_base64"])
        ext = {
            "image/png": ".png", "image/jpeg": ".jpg",
            "image/webp": ".webp", "image/gif": ".gif",
        }.get(b.get("photo_type", ""), ".bin")
        fname = f"r{rid}-{secrets.token_hex(4)}{ext}"
        (config.PHOTO_DIR / fname).write_bytes(raw)
        photo_path = fname
    bid = add_batch(
        conn, rid, b["name"],
        _f(b.get("declared_weight_kg")), _f(b.get("actual_weight_kg")),
        _f(b.get("volume_m3")), photo_path, b.get("inbound_note", ""))
    conn.commit()
    return {"id": bid}


@route("POST", "/api/reservations/{rid}/checkin", {"admin"})
def do_checkin(ctx: Ctx, conn):
    rid = int(ctx.match["rid"])
    res = check_in(conn, rid, bool(ctx.body.get("passed", True)),
                   ctx.body.get("reviewer_note", ""))
    conn.commit()
    return res


# ---- 租期 / 移格 / 退库 ------------------------------------------------------

@route("GET", "/api/leases", {"admin", "finance", "tenant"})
def list_leases(ctx: Ctx, conn):
    sql = ("SELECT l.*, t.name tenant_name, r.start_ts res_start, r.end_ts res_end"
           " FROM leases l JOIN reservations r ON r.id=l.reservation_id"
           " JOIN tenants t ON t.id=r.tenant_id ORDER BY l.id DESC")
    rows = [dict(x) for x in conn.execute(sql).fetchall()]
    if ctx.user["role"] == "tenant":
        rows = [x for x in rows
                if _tenant_of(conn, x["id"]) == ctx.user["tenant_id"]]
    for x in rows:
        x["moves"] = [dict(m) for m in conn.execute(
            "SELECT * FROM moves WHERE lease_id=? ORDER BY id", (x["id"],))]
        x["incidents"] = [dict(i) for i in conn.execute(
            "SELECT * FROM incidents WHERE lease_id=? ORDER BY ts", (x["id"],))]
    return rows


def _tenant_of(conn: sqlite3.Connection, lease_id: int) -> str:
    return conn.execute(
        "SELECT r.tenant_id FROM leases l JOIN reservations r"
        " ON r.id=l.reservation_id WHERE l.id=?", (lease_id,)).fetchone()["tenant_id"]


@route("GET", "/api/leases/{lid}", {"admin", "finance", "tenant"})
def get_lease(ctx: Ctx, conn):
    lid = int(ctx.match["lid"])
    lease = _own_lease(conn, ctx.user, lid)
    d = dict(lease)
    d["intervals"] = [dict(zip(("cell_id", "from", "to", "lo", "hi"), iv))
                      for iv in occupancy_intervals(conn, lease)]
    d["moves"] = [dict(m) for m in conn.execute(
        "SELECT * FROM moves WHERE lease_id=? ORDER BY id", (lid,))]
    d["incidents"] = [dict(i) for i in conn.execute(
        "SELECT * FROM incidents WHERE lease_id=? ORDER BY ts", (lid,))]
    bill = conn.execute("SELECT id FROM bills WHERE lease_id=?", (lid,)).fetchone()
    d["bill_id"] = bill["id"] if bill else None
    return d


@route("POST", "/api/leases/{lid}/move", {"admin", "tenant"})
def do_move(ctx: Ctx, conn):
    lid = int(ctx.match["lid"])
    _own_lease(conn, ctx.user, lid)
    res = request_move(conn, lid, ctx.body["to_cell"], ctx.body.get("reason", ""))
    conn.commit()
    return res


@route("POST", "/api/moves/{mid}/decide", {"admin"})
def do_decide_move(ctx: Ctx, conn):
    decide_move(conn, int(ctx.match["mid"]), bool(ctx.body.get("approve")),
                ctx.user["id"])
    conn.commit()
    return {"ok": True}


@route("POST", "/api/leases/{lid}/checkout", {"admin"})
def do_checkout(ctx: Ctx, conn):
    lid = int(ctx.match["lid"])
    res = check_out(conn, lid, ctx.user["id"])
    conn.commit()
    return res


@route("POST", "/api/leases/{lid}/temp-pull", {"admin"})
def do_temp_pull(ctx: Ctx, conn):
    lid = int(ctx.match["lid"])
    iid = temp_pull(conn, lid, float(ctx.body["hours"]), ctx.user["id"],
                    ctx.body.get("note", ""))
    conn.commit()
    return {"incident_id": iid}


@route("POST", "/api/leases/{lid}/leak", {"admin"})
def do_leak(ctx: Ctx, conn):
    lid = int(ctx.match["lid"])
    iid = record_leak(conn, lid, ctx.body.get("detail", ""), ctx.user["id"])
    conn.commit()
    return {"incident_id": iid}


@route("POST", "/api/leases/{lid}/detect", {"admin", "finance", "tenant"})
def do_detect(ctx: Ctx, conn):
    lid = int(ctx.match["lid"])
    _own_lease(conn, ctx.user, lid)
    created = detect_all(conn)
    conn.commit()
    return {"created_incident_ids": created}


# ---- 温度曲线数据 ------------------------------------------------------------

@route("GET", "/api/cells/{cid}/samples", {"admin", "finance", "tenant"})
def cell_samples(ctx: Ctx, conn):
    cid = ctx.match["cid"]
    if ctx.user["role"] == "tenant":
        own = conn.execute(
            "SELECT 1 FROM reservations r JOIN leases l ON l.reservation_id=r.id"
            " WHERE r.tenant_id=? AND l.cell_id=? LIMIT 1",
            (ctx.user["tenant_id"], cid)).fetchone()
        # 移格历史中曾占用的格也允许查看
        if not own:
            own = conn.execute(
                "SELECT 1 FROM reservations r JOIN leases l ON l.reservation_id=r.id"
                " JOIN moves m ON m.lease_id=l.id WHERE r.tenant_id=?"
                " AND m.from_cell=? LIMIT 1",
                (ctx.user["tenant_id"], cid)).fetchone()
        # 已确认预约也算"自己的货格"（锁格后、入库前）
        if not own:
            own = conn.execute(
                "SELECT 1 FROM reservations WHERE tenant_id=? AND cell_id=?"
                " AND status IN ('confirmed','checked_in') LIMIT 1",
                (ctx.user["tenant_id"], cid)).fetchone()
        if not own:
            raise ApiError(403, "租户只能查看自己的货格")
    frm = ctx.q("from", shift_iso(now_iso(), hours=-30))
    to = ctx.q("to", now_iso())
    rows = conn.execute(
        "SELECT ts,temp_c,power_on FROM temp_samples WHERE cell_id=? AND ts>=?"
        " AND ts<=? ORDER BY ts", (cid, frm, to)).fetchall()
    doors = conn.execute(
        "SELECT opened_at,closed_at FROM door_events WHERE cell_id=? AND closed_at>=?",
        (cid, frm)).fetchall()
    incidents = conn.execute(
        "SELECT id,kind,ts,start_ts,end_ts,detail,amount FROM incidents"
        " WHERE cell_id=? ORDER BY ts", (cid,)).fetchall()
    zone = conn.execute("SELECT zone_code FROM cells WHERE id=?",
                        (cid,)).fetchone()
    if not zone:
        raise ApiError(404, "货格不存在")
    z = config.ZONES[zone["zone_code"]]
    overrides = [dict(o) for o in conn.execute(
        "SELECT new_min,new_max,created_at,reason FROM temp_overrides"
        " WHERE cell_id=? ORDER BY created_at", (cid,))]
    return {
        "cell_id": cid, "zone": zone["zone_code"],
        "band": {"min": z["temp_min"], "max": z["temp_max"], "target": z["target"]},
        "overrides": overrides,
        "samples": [{"ts": r["ts"], "temp_c": r["temp_c"],
                     "power_on": bool(r["power_on"])} for r in rows],
        "doors": [{"from": d["opened_at"], "to": d["closed_at"]} for d in doors],
        "incidents": [dict(i) for i in incidents],
    }


# ---- 管理员改温 ---------------------------------------------------------------

@route("POST", "/api/admin/override", {"admin"})
def do_override(ctx: Ctx, conn):
    b = ctx.body
    oid = override_temp(conn, b["cell_id"], float(b["new_min"]),
                        float(b["new_max"]), b.get("reason", ""), ctx.user["id"])
    conn.commit()
    return {"id": oid}


# ---- 门磁（演示/管理员登记） ---------------------------------------------------

@route("POST", "/api/cells/{cid}/door", {"admin"})
def do_door(ctx: Ctx, conn):
    cid = ctx.match["cid"]
    minutes = float(ctx.body.get("minutes", 8))
    opened = shift_iso(now_iso(), minutes=-minutes)
    eid = adapters.door_event(conn, cid, opened, now_iso(),
                              ctx.body.get("note", "管理员登记"))
    conn.commit()
    detect_all(conn)
    conn.commit()
    return {"event_id": eid}


# ---- 异常与账单 ---------------------------------------------------------------

@route("GET", "/api/incidents", {"admin", "finance", "tenant"})
def list_incidents(ctx: Ctx, conn):
    sql = ("SELECT i.*, t.name tenant_name FROM incidents i"
           " LEFT JOIN leases l ON l.id=i.lease_id"
           " LEFT JOIN reservations r ON r.id=l.reservation_id"
           " LEFT JOIN tenants t ON t.id=r.tenant_id ORDER BY i.ts DESC")
    rows = [dict(x) for x in conn.execute(sql).fetchall()]
    if ctx.user["role"] == "tenant":
        rows = [x for x in rows if x["tenant_name"] and
                _name_of_tenant(conn, x) == ctx.user["tenant_id"]]
        for x in rows:
            x.pop("tenant_name", None)
    return rows


def _name_of_tenant(conn, x):
    row = conn.execute(
        "SELECT r.tenant_id FROM incidents i LEFT JOIN leases l ON l.id=i.lease_id"
        " LEFT JOIN reservations r ON r.id=l.reservation_id WHERE i.id=?",
        (x["id"],)).fetchone()
    return row["tenant_id"] if row else None


@route("GET", "/api/bills", {"admin", "finance", "tenant"})
def bills_list(ctx: Ctx, conn):
    tid = ctx.user["tenant_id"] if ctx.user["role"] == "tenant" else None
    return billing.list_bills(conn, tid)


@route("GET", "/api/bills/{bid}", {"admin", "finance", "tenant"})
def bill_detail(ctx: Ctx, conn):
    bid = int(ctx.match["bid"])
    bill = billing.get_bill(conn, bid)
    if bill is None:
        raise ApiError(404, "账单不存在")
    if ctx.user["role"] == "tenant" and bill["tenant_id"] != ctx.user["tenant_id"]:
        raise ApiError(403, "租户只能查看自己的账单")
    return bill


@route("POST", "/api/bills/{bid}/review", {"finance"})
def bill_review(ctx: Ctx, conn):
    bid = int(ctx.match["bid"])
    snap = billing.review_discount(
        conn, bid, int(ctx.body["incident_id"]),
        bool(ctx.body.get("approve")), ctx.user["id"],
        ctx.body.get("note", ""))
    conn.commit()
    return snap


@route("POST", "/api/bills/{bid}/pay", {"finance", "admin"})
def bill_pay(ctx: Ctx, conn):
    billing.mark_paid(conn, int(ctx.match["bid"]))
    conn.commit()
    return {"ok": True}


# ---- 辅助 -------------------------------------------------------------------

def _f(v) -> float | None:
    if v in (None, "", "null"):
        return None
    return float(v)


# ---- WSGI 分发（由 BaseHTTPRequestHandler 调用） ------------------------------

def dispatch(handler) -> None:
    parsed = urlparse(handler.path)
    path = parsed.path
    handler.query = parse_qs(parsed.query)
    handler.cookie = SimpleCookie(handler.headers.get("Cookie", ""))
    handler.extra_cookie = None

    # 静态文件由 server 层先行处理，到这里说明异常
    if path == "/" or path.startswith("/static/"):
        handler.send_json(404, {"error": "static not served"})
        return

    for method, pattern, fn, roles in _ROUTES:
        if method != handler.command:
            continue
        m = _match(pattern, path)
        if m is None:
            continue
        conn = connect()
        ctx = Ctx(handler, m)
        try:
            token = None
            if "session" in handler.cookie:
                token = handler.cookie["session"].value
            ctx.user = _current_user(conn, token)
            if roles:
                _require(ctx, roles)
            if method in ("POST", "PUT", "PATCH"):
                length = int(handler.headers.get("Content-Length", 0) or 0)
                raw = handler.rfile.read(length) if length else b"{}"
                try:
                    ctx.body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    raise ApiError(400, "请求体不是合法 JSON")
            result = fn(ctx, conn)
            conn.commit()
            handler.send_json(200, result if result is not None else {"ok": True})
        except ApiError as e:
            conn.rollback()
            handler.send_json(e.status, {"error": str(e)})
        except DomainError as e:
            conn.rollback()
            handler.send_json(409, {"error": str(e)})
        except (KeyError, ValueError, TypeError) as e:
            conn.rollback()
            handler.send_json(400, {"error": f"参数错误：{e}"})
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            import traceback
            traceback.print_exc()
            handler.send_json(500, {"error": f"服务器错误：{e}"})
        finally:
            conn.close()
        return

    handler.send_json(404, {"error": f"未找到路径：{path}"})


def _serve_static(path: str) -> None:
    """通过全局 handler 发送（见 server 中绑定）。"""
    raise RuntimeError("由 server 层处理")


def serve_file(handler, path: str) -> None:
    if path == "/":
        target = STATIC_DIR / "index.html"
    else:
        rel = path[len("/static/"):]
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())):
            handler.send_json(403, {"error": "forbidden"})
            return
        if not target.is_file():
            handler.send_json(404, {"error": "not found"})
            return
    ctype = {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
    }.get(target.suffix, "application/octet-stream")
    body = target.read_bytes()
    handler.send_bytes(200, body, ctype)
