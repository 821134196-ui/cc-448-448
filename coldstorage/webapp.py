"""Web 应用：库区格位图、预约日历、入退库复核、温度曲线、移格、异常补偿、账单明细。

仅依赖 Python 标准库。会话用 cookie 中的演示令牌；租户只能访问自己的单据。
"""

from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import urllib.parse
import warnings
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import models as M
from . import services as S
from .adapters import SimDoorAdapter
from .db import connect
from .models import (
    STATUS_LABEL, INCIDENT_LABEL, SEG_LABEL, ZONE_TYPE_LABEL,
)

warnings.filterwarnings("ignore", category=DeprecationWarning)
import cgi  # noqa: E402  标准库多部分表单解析（演示环境无第三方依赖）

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def fmt_dt(s: str | None) -> str:
    if not s:
        return "—"
    try:
        return datetime.strptime(s, S.NOW_FMT).strftime("%m-%d %H:%M")
    except ValueError:
        return s


def fmt_full(s: str | None) -> str:
    if not s:
        return "—"
    try:
        return datetime.strptime(s, S.NOW_FMT).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return s


def money(v) -> str:
    return f"{float(v or 0):,.2f}"


def status_pill(status: str) -> str:
    cls = {
        M.PENDING: "warn", M.CONFIRMED: "violet", M.CHECKED_IN: "good",
        M.MOVE_PENDING: "warn", M.CHECKED_OUT: "warn", M.SETTLED: "gray",
        M.REJECTED: "danger", M.CANCELLED: "gray",
    }.get(status, "")
    return f'<span class="pill {cls}">{esc(STATUS_LABEL.get(status, status))}</span>'


class Actor:
    def __init__(self, kind: str, row):
        self.kind = kind          # tenant / admin / finance / anon
        self.row = row

    @property
    def id(self):
        return self.row["id"]

    @property
    def name(self):
        return self.row["name"] if "name" in self.row.keys() else self.row["login"]

    @property
    def is_staff(self) -> bool:
        return self.kind in ("admin", "finance")

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin"

    @property
    def is_finance(self) -> bool:
        return self.kind == "finance"


# ================================================================ 页面片段

PAGE_SHELL = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 霜格云仓</title>
<link rel="stylesheet" href="/static/style.css">
</head><body>
<header class="topbar">
  <span class="brand">🧊 霜格云仓 <span style="font-weight:400;font-size:12px">共享冷库租格与温度计费平台</span></span>
  <nav>{nav}</nav>
  <span class="who">{who}</span>
</header>
<main class="layout">{flash}{body}</main>
{footer}
</body></html>"""


def make_server(db_path: str, photo_dir: str, host: str = "127.0.0.1",
                port: int = 8000, monitor=None) -> ThreadingHTTPServer:
    door = SimDoorAdapter()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ColdStorage/1.0"

        def log_message(self, fmt, *args):
            pass

        # ---------------------------------------------- 基础
        def _db(self):
            return connect(db_path)

        def _actor(self) -> Actor:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookie.get("session")
            if not token:
                return Actor("anon", {"id": 0, "name": "访客", "login": ""})
            tok = token.value
            conn = self._db()
            try:
                row = conn.execute("SELECT * FROM staff WHERE token=?", (tok,)).fetchone()
                if row:
                    return Actor(row["role"], row)
                row = conn.execute("SELECT * FROM tenants WHERE token=?", (tok,)).fetchone()
                if row:
                    return Actor("tenant", row)
                return Actor("anon", {"id": 0, "name": "访客", "login": ""})
            finally:
                conn.close()

        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8",
                  extra_headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or []):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _html(self, body: str, title: str, actor: Actor, active: str = "",
                  flash_ok="", flash_err=""):
            nav = self._nav(actor, active)
            if actor.kind == "anon":
                who = '<a href="/login" style="color:#fff">登录</a>'
            else:
                role = {"admin": "管理员", "finance": "财务", "tenant": "租户"}[actor.kind]
                who = f'{esc(actor.name)}（{role}） · <a href="/logout" style="color:#bfe3e2">退出</a>'
            flash = ""
            if flash_ok:
                flash = f'<div class="flash ok">✓ {esc(flash_ok)}</div>'
            if flash_err:
                flash = f'<div class="flash err">✕ {esc(flash_err)}</div>'
            page = PAGE_SHELL.format(
                title=esc(title), nav=nav, who=who, flash=flash, body=body,
                footer='<div class="footer-note">本平台数据均为虚构经营数据，仅用于演示</div>')
            self._send(200, page.encode())

        def _nav(self, actor: Actor, active: str) -> str:
            items = [("/cells", "库区格位图", "cells"),
                     ("/calendar", "预约日历", "calendar"),
                     ("/temperature", "温度曲线", "temperature"),
                     ("/incidents", "异常留痕", "incidents")]
            if actor.kind == "tenant":
                items.insert(2, ("/reservations", "我的租约", "reservations"))
            elif actor.is_staff:
                items.insert(2, ("/reservations", "租约管理", "reservations"))
                if actor.is_finance:
                    items.append(("/finance", "财务复核", "finance"))
            links = []
            for href, label, key in items:
                cls = ' class="active"' if key == active else ""
                links.append(f'<a href="{href}"{cls}>{label}</a>')
            return "".join(links)

        def redirect(self, path: str):
            # Location 头只能是 latin-1：对路径中的非 ASCII（中文提示语）做百分号编码
            safe = urllib.parse.quote(path, safe="/?&=:%#")
            self._send(303, b"", extra_headers=[("Location", safe)])

        def parse_qs(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length", 0))
            ctype = self.headers.get("Content-Type", "")
            if ctype.startswith("multipart/form-data"):
                fs = cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                                      environ={"REQUEST_METHOD": "POST",
                                               "CONTENT_TYPE": ctype},
                                      keep_blank_values=True)
                return {k: fs[k].value for k in fs.keys()}
            raw = self.rfile.read(length)
            parsed = urllib.parse.parse_qs(raw.decode(), keep_blank_values=True)
            return {k: v[-1] for k, v in parsed.items()}

        # ---------------------------------------------- 路由
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
            actor = self._actor()
            try:
                if path == "/login":
                    return self.page_login()
                if path == "/logout":
                    self._send(303, b"", extra_headers=[
                        ("Location", "/login"),
                        ("Set-Cookie", "session=; Path=/; Max-Age=0")])
                    return
                if path.startswith("/static/"):
                    return self.serve_static(path)
                if path == "/photos" or path.startswith("/photos/"):
                    return self.serve_photo(path, actor)
                if actor.kind == "anon":
                    return self.redirect("/login")
                if path == "/":
                    return self.redirect("/cells")
                if path == "/cells":
                    return self.page_cells(actor, qs)
                if path == "/calendar":
                    return self.page_calendar(actor, qs)
                if path == "/reservations":
                    return self.page_reservations(actor, qs)
                if path == "/reservations/new":
                    return self.page_new_reservation(actor, qs)
                m = re.fullmatch(r"/reservations/(\d+)", path)
                if m:
                    return self.page_reservation(actor, int(m.group(1)), qs)
                if path == "/temperature":
                    return self.page_temperature(actor, qs)
                if path == "/incidents":
                    return self.page_incidents(actor, qs)
                if path == "/finance":
                    return self.page_finance(actor, qs)
                self._send(404, b"<h1>404</h1>")
            except M.PermissionDenied as exc:
                self._html(f'<div class="card"><h2>无权访问</h2><p>{esc(exc)}</p>'
                           f'<p><a class="btn ghost" href="/cells">返回格位图</a></p></div>',
                           "无权访问", actor, flash_err=str(exc))
            except M.PlatformError as exc:
                self._html(f'<div class="card"><h2>操作未完成</h2><p>{esc(exc)}</p></div>',
                           "提示", actor, flash_err=str(exc))

        def do_POST(self):
            actor = self._actor()
            parsed = urllib.parse.urlparse(self.path)
            path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
            if actor.kind == "anon":
                return self.redirect("/login")
            form = self.parse_qs()
            try:
                self.route_post(actor, path, form, query)
            except M.PlatformError as exc:
                back = form.get("_back", "/reservations")
                self.redirect(f"{back}?err={urllib.parse.quote(str(exc))}")

        def route_post(self, actor: Actor, path: str, form: dict, query: dict):
            conn = self._db()
            ok = ""
            try:
                # ---- 会话
                if path == "/login":
                    token = form.get("token", "")
                    self._send(303, b"", extra_headers=[
                        ("Location", "/cells"),
                        ("Set-Cookie", f"session={urllib.parse.quote(token)}; Path=/; HttpOnly")])
                    return

                rid_m = re.fullmatch(r"/reservations/(\d+)/(.+)", path)
                if path == "/reservations/new":
                    return self.action_create_reservation(conn, actor, form)
                if path == "/zones/temp-change":
                    self.require(actor.is_admin, "仅管理员可手工改温")
                    zid = int(form["zone_id"])
                    S.manual_temp_change(conn, zid, float(form["temp_min"]),
                                         float(form["temp_max"]), form.get("reason", ""),
                                         actor=f"admin:{actor.name}")
                    return self.redirect("/incidents?ok=" + urllib.parse.quote("改温已留痕"))
                if rid_m:
                    return self.route_reservation_action(conn, actor, int(rid_m.group(1)),
                                                         rid_m.group(2), form)
                self._send(404, b"not found")
            finally:
                conn.close()

        def route_reservation_action(self, conn, actor: Actor, rid: int, action: str,
                                     form: dict):
            r = S.require(conn, "reservations", rid)
            self.require_tenant_access(actor, r["tenant_id"])
            back = f"/reservations/{rid}"

            if action == "confirm":
                self.require(actor.is_admin, "仅管理员确认预约")
                cell_id = S.confirm_reservation(conn, rid, f"admin:{actor.name}")
                cell = S.require(conn, "cells", cell_id)
                msg = f"已锁定货格 {cell['code']}"
                return self.redirect(f"{back}?ok={urllib.parse.quote(msg)}")
            if action == "reject":
                self.require(actor.is_admin, "仅管理员拒绝预约")
                S.reject_reservation(conn, rid, form.get("reason", ""), f"admin:{actor.name}")
                return self.redirect(f"{back}?ok=预约已拒绝")
            if action == "cancel":
                S.cancel_reservation(conn, rid, f"{actor.kind}:{actor.name}")
                return self.redirect(f"{back}?ok=预约已取消，货格释放")
            if action == "checkin":
                self.require(actor.is_admin, "仅管理员办理入库复核")
                at = self._dt(form.get("actual_start"))
                S.check_in(conn, rid, float(form["weight"]), float(form["volume"]),
                           f"admin:{actor.name}", at=at)
                return self.redirect(f"{back}?ok=入库复核通过，计费开始")
            if action == "checkout":
                self.require(actor.is_admin, "仅管理员办理退库清点")
                at = self._dt(form.get("actual_end"))
                S.check_out(conn, rid, f"admin:{actor.name}", at=at)
                return self.redirect(f"{back}?ok=退库清点结束，费用快照已生成")
            if action == "move":
                direct, mid = S.request_move(
                    conn, rid, int(form["to_cell_id"]), form.get("reason", ""),
                    f"{actor.kind}:{actor.name}")
                msg = "同温区移位已完成" if direct else "跨温区移格申请已提交，待管理员重新确认"
                return self.redirect(f"{back}?ok={urllib.parse.quote(msg)}")
            if action == "move-decide":
                self.require(actor.is_admin, "仅管理员可确认跨温区移格")
                mid = int(form["move_id"])
                S.decide_move(conn, mid, form.get("approve") == "1",
                              f"admin:{actor.name}", staff_id=actor.id)
                return self.redirect(f"{back}?ok=移格申请已处理")
            if action == "door":
                self.require(actor.is_admin, "门磁操作仅管理员")
                cell_id = r["cell_id"]
                if form["kind"] == "open":
                    door.open(conn, cell_id, note=f"{actor.name} 模拟开门")
                else:
                    door.close(conn, cell_id, note=f"{actor.name} 模拟关门")
                return self.redirect(f"{back}?ok=门磁事件已记录")
            if action == "leak":
                self.require(actor.is_admin, "仅管理员登记渗漏")
                S.log_leak(conn, r["cell_id"], form.get("detail", ""),
                           f"admin:{actor.name}")
                return self.redirect(f"{back}?ok=货品渗漏已留痕")
            if action == "photo":
                return self.action_upload_photo(conn, actor, r, form)
            if action == "adjustment":
                self.require(actor.is_finance, "仅财务可登记减免")
                S.add_manual_adjustment(conn, rid, float(form["amount"]),
                                        form.get("reason", ""), f"finance:{actor.name}")
                return self.redirect(f"{back}?ok=减免已登记，待财务复核")
            if action == "settle":
                self.require(actor.is_finance, "仅财务可结账")
                res = S.settle(conn, rid, f"finance:{actor.name}")
                msg = "账单已结账（幂等返回）" if res["already_settled"] else "账单已结账，租期关闭"
                return self.redirect(f"{back}?ok={urllib.parse.quote(msg)}")
            if action == "adjustment-review":
                self.require(actor.is_finance, "仅财务复核减免")
                S.review_adjustment(conn, int(form["adjustment_id"]),
                                    form.get("approve") == "1", actor.id,
                                    f"finance:{actor.name}")
                return self.redirect("/finance?ok=减免已复核")
            self._send(404, b"unknown action")

        # ---------------------------------------------- 动作
        def action_create_reservation(self, conn, actor, form):
            self.require(actor.kind == "tenant", "仅租户可提交预约")
            bid = int(form["batch_id"])
            rid = S.create_reservation(
                conn, actor.id, bid, float(form["tmin"]), float(form["tmax"]),
                self._dt(form["planned_start"]), self._dt(form["planned_end"]),
                float(form["volume"]), float(form["weight"]), form.get("note", ""))
            self.redirect(f"/reservations/{rid}?ok=预约已提交，待管理员确认锁定货格")

        def action_upload_photo(self, conn, actor, r, form):
            # parse_qs 已读取 multipart；文件内容需重新解析的替代方案：
            # 这里接受字段 photo_data（data URL 或纯文本），演示照片以文本落盘为 .txt/.svg
            data = form.get("photo_data", "")
            label = form.get("label", "补充照片")
            bid = r["batch_id"]
            fname = f"batch_{bid}_{S.iso(S.now()).replace(':', '')}.svg"
            path = os.path.join(photo_dir, fname)
            if data.lstrip().startswith("<svg"):
                content = data
            else:
                content = (f"<svg xmlns='http://www.w3.org/2000/svg' width='320' height='220'>"
                           f"<rect width='100%' height='100%' fill='#eef6f8'/>"
                           f"<text x='160' y='110' font-size='16' text-anchor='middle' "
                           f"fill='#2e2a3a' font-family='sans-serif'>{esc(label)}</text></svg>")
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            S.add_photo(conn, bid, path, label)
            self.redirect(f"/reservations/{r['id']}?ok=照片已保存到本地")

        # ---------------------------------------------- 登录页
        def page_login(self):
            conn = self._db()
            try:
                staff = list(conn.execute("SELECT * FROM staff ORDER BY id"))
                tenants = list(conn.execute("SELECT * FROM tenants ORDER BY id"))
            finally:
                conn.close()
            btns = "".join(
                f'<button class="btn rolebtn" name="token" value="{esc(s["token"])}">'
                f'{"🛡️ 管理员" if s["role"]=="admin" else "💰 财务"} · {esc(s["name"])}</button>'
                for s in staff)
            tbtns = "".join(
                f'<button class="btn ghost rolebtn" name="token" value="{esc(t["token"])}">'
                f'🏢 {esc(t["name"])}</button>' for t in tenants)
            body = f"""
<div class="card login-box">
  <h2>选择演示身份 <span class="hint">虚构数据，一键登录</span></h2>
  <form method="post" action="/login" style="margin-top:12px">
    <label class="fld">库内员工</label>{btns}
    <label class="fld" style="margin-top:14px">租户（仅可见自己的货格与账单）</label>{tbtns}
  </form>
</div>"""
            self._html(body, "登录", Actor("anon", {"id": 0, "name": "访客", "login": ""}))

        # ---------------------------------------------- 格位图
        def page_cells(self, actor: Actor, qs: dict):
            conn = self._db()
            try:
                groups = S.zone_cells_with_remaining(conn)
                cards = []
                for g in groups:
                    z = g["zone"]
                    pct = int(g["used_m3"] / g["total_m3"] * 100) if g["total_m3"] else 0
                    bar_cls = "full" if pct >= 100 else ("warn" if pct >= 80 else "")
                    cells_html = []
                    for item in g["cells"]:
                        c = item["row"]
                        rem = item["remaining_m3"]
                        r = None
                        if c["active_reservation_id"]:
                            r = conn.execute(
                                "SELECT r.*, t.name tname FROM reservations r "
                                "JOIN tenants t ON t.id=r.tenant_id WHERE r.id=?",
                                (c["active_reservation_id"],)).fetchone()
                        if r:
                            over = c["occupied_m3"] > c["capacity_m3"] + 1e-9
                            cls = "over" if over else "used"
                            tag = '<span class="pill danger">超容</span>' if over else \
                                  '<span class="pill gray">占用</span>'
                            # 租户隔离：非本人且非员工，不显示租户信息
                            if actor.is_staff or (actor.kind == "tenant" and r["tenant_id"] == actor.id):
                                who = (f'<a class="ctenant" href="/reservations/{r["id"]}">'
                                       f'{esc(r["tname"])} · {esc(STATUS_LABEL[r["status"]])}</a>')
                            else:
                                who = '<span class="ctenan muted">已租（其他租户）</span>'
                            cells_html.append(f"""
<div class="cell {cls}">
  <span class="ccode">{esc(c['code'])}</span>
  {tag}
  {who}
  <span class="cmeta">容量 {c['capacity_m3']:.1f} / 占 {c['occupied_m3']:.2f} m³</span>
</div>""")
                        else:
                            cells_html.append(f"""
<div class="cell free">
  <span class="ccode">{esc(c['code'])}</span>
  <span class="pill good">空闲</span>
  <span class="cmeta">剩余 {rem:.2f} m³</span>
  <span class="cmeta">容量 {c['capacity_m3']:.1f} m³</span>
</div>""")
                    cards.append(f"""
<div class="zone-block">
  <div class="zone-head">
    <span class="zname">{esc(z['name'])}</span>
    <span class="ztag">{esc(ZONE_TYPE_LABEL.get(z['zone_type'], z['zone_type']))}</span>
    <span class="zrange">温区 {z['temp_min']:g}~{z['temp_max']:g}℃ ·
      费率 {z['hourly_rate']:g} 元/格时 + {z['kg_hour_rate']:g} 元/kg时</span>
    <span class="zremain">剩余容积 <b>{g['remaining_m3']:.2f}</b> / {g['total_m3']:.1f} m³</span>
    <span class="cap-bar"><i class="{bar_cls}" style="width:{pct}%"></i></span>
  </div>
  <div class="cells-row">{''.join(cells_html)}</div>
</div>""")

                kpis = self._kpis(conn)
                body = f"""
<div class="grid cols-4" style="margin-bottom:18px">{kpis}</div>
<div class="page-head"><h1>库区格位图</h1>
  <span class="sub">按库区纵向分组 · 显示每格剩余容积 · 预约确认即锁定容量</span></div>
<div class="zone-stack">{''.join(cards)}</div>"""
                self._html(body, "库区格位图", actor, "cells",
                           flash_ok=qs.get("ok", [""])[0], flash_err=qs.get("err", [""])[0])
            finally:
                conn.close()

        def _kpis(self, conn) -> str:
            total = conn.execute("SELECT COUNT(*) c, SUM(occupied_m3>0) u FROM cells").fetchone()
            active = conn.execute(
                "SELECT COUNT(*) c FROM reservations WHERE status=?", (M.CHECKED_IN,)).fetchone()["c"]
            open_inc = conn.execute(
                "SELECT COUNT(*) c FROM incidents WHERE resolved=0").fetchone()["c"]
            pend = conn.execute(
                "SELECT COUNT(*) c FROM reservations WHERE status=?", (M.PENDING,)).fetchone()["c"]
            def kpi(label, value, small=""):
                return (f'<div class="kpi"><div class="label">{label}</div>'
                        f'<div class="value">{value} <small>{small}</small></div></div>')
            return (kpi("货格占用", f'{total["u"] or 0}/{total["c"]}', "格")
                    + kpi("计费中租约", active, "笔")
                    + kpi("待确认预约", pend, "笔")
                    + kpi("未结异常", open_inc, "条"))

        # ---------------------------------------------- 日历
        def page_calendar(self, actor: Actor, qs: dict):
            conn = self._db()
            try:
                today = S.now().date()
                year = int(qs.get("y", [today.year])[0])
                month = int(qs.get("m", [today.month])[0])
                first = datetime(year, month, 1)
                start_day = (first.weekday() + 1) % 7  # 周一为首列
                if month == 12:
                    nxt = f"/calendar?y={year+1}&m=1"
                    prev = f"/calendar?y={year}&m={month-1}"
                elif month == 1:
                    nxt = f"/calendar?y={year}&m=2"
                    prev = f"/calendar?y={year-1}&m=12"
                else:
                    nxt = f"/calendar?y={year}&m={month+1}"
                    prev = f"/calendar?y={year}&m={month-1}"
                days_in = (datetime(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day

                q = """SELECT r.*, t.name tname, c.code ccode FROM reservations r
                       JOIN tenants t ON t.id=r.tenant_id
                       LEFT JOIN cells c ON c.id=r.cell_id
                       WHERE r.status NOT IN ('rejected','cancelled')"""
                params = []
                if actor.kind == "tenant":
                    q += " AND r.tenant_id=?"
                    params.append(actor.id)
                resv = list(conn.execute(q, params))

                cells = []
                for wi in range(6):
                    for dow in range(7):
                        dnum = wi * 7 + dow - start_day + 1
                        if dnum < 1 or dnum > days_in:
                            cells.append('<td class="out"></td>')
                            continue
                        day = datetime(year, month, dnum).date()
                        evs = []
                        for r in resv:
                            ps = datetime.strptime(r["planned_start"], S.NOW_FMT).date()
                            pe = datetime.strptime(r["planned_end"], S.NOW_FMT).date()
                            if r["actual_start"]:
                                as_ = datetime.strptime(r["actual_start"], S.NOW_FMT).date()
                                if as_ == day:
                                    evs.append(("ckin", f'入库 #{r["id"]} {esc(r["ccode"] or "")}'))
                            elif ps == day:
                                evs.append(("span", f'起租 #{r["id"]}'))
                            if r["actual_end"] and datetime.strptime(
                                    r["actual_end"], S.NOW_FMT).date() == day:
                                evs.append(("ckout", f'退库 #{r["id"]}'))
                            elif pe == day and not r["actual_end"]:
                                evs.append(("span", f'到期 #{r["id"]}'))
                            elif ps < day < pe and not r["actual_end"]:
                                if day.weekday() == 0 or dnum == 1:
                                    evs.append(("span", f'租用中 #{r["id"]} {esc(r["ccode"] or "")}'))
                        today_cls = " today" if day == today else ""
                        evhtml = "".join(
                            f'<a class="ev {k}" href="/reservations/{self._ev_id(v)}">{v}</a>'
                            for k, v in evs)
                        cells.append(f'<td class="{today_cls.strip()}"><div class="dnum">{dnum}</div>{evhtml}</td>')
                rows = ["<tr>" + "".join(cells[i*7:(i+1)*7]) + "</tr>" for i in range(6)]
                body = f"""
<div class="page-head"><h1>预约日历</h1>
  <span class="sub">{year} 年 {month} 月 · 蓝色入库 / 紫色退库 / 绿色租期</span></div>
<div class="card">
  <div style="margin-bottom:10px">
    <a class="btn ghost small-btn" href="{prev}">← 上月</a>
    <a class="btn ghost small-btn" href="/calendar">本月</a>
    <a class="btn ghost small-btn" href="{nxt}">下月 →</a>
  </div>
  <div class="cal-wrap"><table class="cal">
    <tr><th>周一</th><th>周二</th><th>周三</th><th>周四</th><th>周五</th><th>周六</th><th>周日</th></tr>
    {''.join(rows)}
  </table></div>
</div>"""
                self._html(body, "预约日历", actor, "calendar")
            finally:
                conn.close()

        @staticmethod
        def _ev_id(label_html: str) -> int:
            m = re.search(r"#(\d+)", label_html)
            return int(m.group(1)) if m else 0

        # ---------------------------------------------- 租约列表
        def page_reservations(self, actor: Actor, qs: dict):
            conn = self._db()
            try:
                q = """SELECT r.*, t.name tname, b.name bname, c.code ccode, z.name zname
                       FROM reservations r
                       JOIN tenants t ON t.id=r.tenant_id
                       JOIN batches b ON b.id=r.batch_id
                       LEFT JOIN cells c ON c.id=r.cell_id
                       LEFT JOIN zones z ON z.id=c.zone_id"""
                params = []
                if actor.kind == "tenant":
                    q += " WHERE r.tenant_id=?"
                    params.append(actor.id)
                status_f = qs.get("status", [""])[0]
                if status_f:
                    q += (" WHERE " if not params else " AND ") + "r.status=?"
                    params.append(status_f)
                q += " ORDER BY r.id DESC"
                rows = list(conn.execute(q, params))
                trs = []
                for r in rows:
                    acts = ""
                    if actor.is_admin and r["status"] == M.PENDING:
                        acts = (f'<a class="btn small-btn" href="/reservations/{r["id"]}">确认锁定</a>')
                    elif r["status"] in (M.CHECKED_IN, M.CONFIRMED, M.CHECKED_OUT, M.MOVE_PENDING):
                        acts = f'<a class="btn ghost small-btn" href="/reservations/{r["id"]}">办理</a>'
                    else:
                        acts = f'<a class="btn ghost small-btn" href="/reservations/{r["id"]}">查看</a>'
                    trs.append(f"""<tr>
<td>#{r['id']}</td>
<td>{esc(r['tname'])}</td>
<td>{esc(r['bname'])}</td>
<td>{esc(r['ccode'] or '—')} <span class="muted">{esc(r['zname'] or '')}</span></td>
<td>{r['target_temp_min']:g}~{r['target_temp_max']:g}℃</td>
<td class="small">{fmt_full(r['planned_start'])}<br>{fmt_full(r['planned_end'])}</td>
<td>{status_pill(r['status'])}</td>
<td class="row-actions">{acts}</td></tr>""")
                new_btn = ('<a class="btn" href="/reservations/new">＋ 新建预约</a>'
                           if actor.kind == "tenant" else "")
                filt = ''.join(
                    f'<a class="btn ghost small-btn" href="/reservations?status={s}">{esc(l)}</a> '
                    for s, l in [("pending", "待确认"), ("checked_in", "计费中"),
                                 ("move_pending", "待移格确认"), ("checked_out", "待结账"),
                                 ("settled", "已结账")])
                body = f"""
<div class="page-head"><h1>{'我的租约' if actor.kind=='tenant' else '租约管理'}</h1>
  <span class="sub" style="margin-left:auto">{new_btn}</span></div>
<div class="card">
  <div style="margin-bottom:10px">
    <a class="btn ghost small-btn" href="/reservations">全部</a> {filt}</div>
  <table class="tbl"><thead><tr>
  <th>单号</th><th>租户</th><th>货品</th><th>货格/库区</th><th>目标温区</th>
  <th>计划时段</th><th>状态</th><th>操作</th></tr></thead>
  <tbody>{''.join(trs) or '<tr><td colspan="8" class="muted">暂无单据</td></tr>'}</tbody></table>
</div>"""
                self._html(body, "租约", actor, "reservations",
                           flash_ok=qs.get("ok", [""])[0], flash_err=qs.get("err", [""])[0])
            finally:
                conn.close()

        # ---------------------------------------------- 新建预约
        def page_new_reservation(self, actor: Actor, qs):
            if actor.kind != "tenant":
                raise M.PermissionDenied("仅租户可提交预约")
            conn = self._db()
            try:
                batches = S.tenant_batches(conn, actor.id)
                opts = "".join(
                    f'<option value="{b["id"]}">{esc(b["name"])}（{esc(b["sku"])}）</option>'
                    for b in batches)
                t0 = S.now() + timedelta(hours=1)
                t1 = t0 + timedelta(days=2)
                def dv(dt):
                    return dt.strftime("%Y-%m-%dT%H:%M")
                body = f"""
<div class="page-head"><h1>新建预约</h1><span class="sub">确认后锁定货格容量与温区</span></div>
<div class="card" style="max-width:720px">
<form method="post" action="/reservations/new">
  <div class="form-grid">
    <div><label class="fld">货品批次</label><select name="batch_id">{opts}</select></div>
    <div><label class="fld">目标温区（℃，下限 / 上限）</label>
      <div style="display:flex;gap:8px"><input name="tmin" type="number" step="0.5" value="-22">
      <input name="tmax" type="number" step="0.5" value="-18"></div></div>
    <div><label class="fld">计划起租</label><input name="planned_start" type="datetime-local"
      value="{dv(t0)}" required></div>
    <div><label class="fld">计划退租</label><input name="planned_end" type="datetime-local"
      value="{dv(t1)}" required></div>
    <div><label class="fld">申报容积 m³</label><input name="volume" type="number" step="0.01" value="1.5"></div>
    <div><label class="fld">申报重量 kg</label><input name="weight" type="number" step="1" value="500"></div>
    <div class="full"><label class="fld">备注</label><input name="note" placeholder="如：到货前请预冷"></div>
  </div>
  <div style="margin-top:14px">
    <button class="btn" type="submit">提交预约申请</button>
    <a class="btn ghost" href="/reservations">返回</a></div>
</form></div>"""
                self._html(body, "新建预约", actor, "reservations",
                           flash_err=qs.get("err", [""])[0])
            finally:
                conn.close()

        # ---------------------------------------------- 租约详情（核心工作台）
        def page_reservation(self, actor: Actor, rid: int, qs: dict):
            conn = self._db()
            try:
                r = conn.execute(
                    """SELECT r.*, t.name tname, b.name bname, b.sku bsku,
                          c.code ccode, z.name zname, z.temp_min zmin, z.temp_max zmax
                       FROM reservations r
                       JOIN tenants t ON t.id=r.tenant_id
                       JOIN batches b ON b.id=r.batch_id
                       LEFT JOIN cells c ON c.id=r.cell_id
                       LEFT JOIN zones z ON z.id=c.zone_id
                       WHERE r.id=?""", (rid,)).fetchone()
                if not r:
                    raise M.PlatformError("租约不存在")
                self.require_tenant_access(actor, r["tenant_id"])

                parts = [self._detail_head(conn, actor, r)]
                parts.append(self._detail_actions(conn, actor, r))
                parts.append(self._detail_photos(conn, actor, r))
                parts.append(self._detail_door(conn, actor, r))
                parts.append(self._detail_incidents(conn, r))
                if r["actual_start"]:
                    parts.append(self._detail_chart(conn, r))
                parts.append(self._detail_billing(conn, actor, r))
                body = f'<div class="page-head"><h1>租约 #{rid}</h1>{status_pill(r["status"])}</div>' + "".join(parts)
                self._html(body, f"租约 #{rid}", actor, "reservations",
                           flash_ok=qs.get("ok", [""])[0], flash_err=qs.get("err", [""])[0])
            finally:
                conn.close()

        def _detail_head(self, conn, actor, r) -> str:
            overload = ""
            if r["cell_id"]:
                c = S.require(conn, "cells", r["cell_id"])
                if c["occupied_m3"] > c["capacity_m3"] + 1e-9:
                    overload = (f'<span class="pill danger">超容 '
                                f'{c["occupied_m3"] - c["capacity_m3"]:.2f} m³</span>')
            weight_txt = f"{r['checked_weight_kg']:g} kg" if r["checked_weight_kg"] else "—"
            zmin_txt = r["zmin"] if r["zmin"] is not None else "—"
            zmax_txt = r["zmax"] if r["zmax"] is not None else "—"
            return f"""
<div class="card">
  <dl class="kv">
    <dt>租户</dt><dd>{esc(r['tname'])}</dd>
    <dt>货品批次</dt><dd>{esc(r['bname'])} <span class="muted">{esc(r['bsku'])}</span></dd>
    <dt>货格 / 库区</dt><dd>{esc(r['ccode'] or '未锁定')} · {esc(r['zname'] or '—')}
      <span class="muted">（库区温区 {zmin_txt}~
      {zmax_txt}℃）</span> {overload}</dd>
    <dt>目标温区</dt><dd><b>{r['target_temp_min']:g}~{r['target_temp_max']:g}℃</b></dd>
    <dt>计划时段</dt><dd>{fmt_full(r['planned_start'])} → {fmt_full(r['planned_end'])}</dd>
    <dt>计费时段</dt><dd>{fmt_full(r['actual_start'])} → {fmt_full(r['actual_end'])}
      <span class="muted">（入库复核通过起算，退库清点截止）</span></dd>
    <dt>申报 / 复核</dt><dd>{r['declared_volume_m3']:.2f} m³ · {r['declared_weight_kg']:g} kg ／
      复核重量 <b>{weight_txt}</b></dd>
    <dt>备注</dt><dd>{esc(r['note']) or '—'}</dd>
  </dl>
</div>"""

        def _detail_actions(self, conn, actor, r) -> str:
            rid = r["id"]
            blocks = []
            now_dt = S.now().replace(second=0, microsecond=0)
            def dv(dt): return dt.strftime("%Y-%m-%dT%H:%M")
            if actor.is_admin and r["status"] == M.PENDING:
                blocks.append(f"""
<form method="post" action="/reservations/{rid}/confirm" class="inline">
  <button class="btn">确认预约并锁定货格</button></form>
<form method="post" action="/reservations/{rid}/reject" class="inline"
  onsubmit="this.reason.value=prompt('拒绝原因（可留空）','')||''">
  <input type="hidden" name="reason"><button class="btn ghost">拒绝</button></form>""")
            if r["status"] in (M.PENDING, M.CONFIRMED) and (actor.is_admin or actor.kind == "tenant"):
                blocks.append(f"""
<form method="post" action="/reservations/{rid}/cancel" class="inline">
  <button class="btn ghost">取消预约</button></form>""")
            if actor.is_admin and r["status"] == M.CONFIRMED:
                blocks.append(f"""
<form method="post" action="/reservations/{rid}/checkin" class="inline"
  style="display:flex;gap:8px;align-items:end;flex-wrap:wrap;margin-top:8px">
  <div><label class="fld">入库复核重量 kg</label><input name="weight" type="number" step="1"
    value="{r['declared_weight_kg']:g}" style="width:140px"></div>
  <div><label class="fld">实测占用容积 m³（超容将留痕）</label><input name="volume" type="number"
    step="0.01" value="{r['declared_volume_m3']:.2f}" style="width:180px"></div>
  <div><label class="fld">入库时刻</label><input name="actual_start" type="datetime-local"
    value="{dv(now_dt)}" style="width:200px"></div>
  <button class="btn">复核通过，开始计费</button></form>""")
            if actor.is_admin and r["status"] == M.CHECKED_IN:
                blocks.append(f"""
<form method="post" action="/reservations/{rid}/checkout" class="inline"
  style="display:flex;gap:8px;align-items:end;flex-wrap:wrap;margin-top:8px">
  <div><label class="fld">退库清点时刻（早于计划结束记提前退租）</label>
    <input name="actual_end" type="datetime-local" value="{dv(now_dt)}" style="width:200px"></div>
  <button class="btn">退库清点结束，生成费用快照</button></form>""")
            # 移格
            if r["status"] in (M.CHECKED_IN, M.MOVE_PENDING):
                free_cells = list(conn.execute(
                    "SELECT c.*, z.name zname FROM cells c JOIN zones z ON z.id=c.zone_id "
                    "WHERE c.active_reservation_id IS NULL ORDER BY c.code"))
                opts = "".join(
                    f'<option value="{c["id"]}">{esc(c["code"])}（{esc(c["zname"])}，'
                    f'余 {c["capacity_m3"] - c["occupied_m3"]:.1f} m³）</option>'
                    for c in free_cells)
                if opts:
                    blocks.append(f"""
<form method="post" action="/reservations/{rid}/move" class="inline"
  style="display:flex;gap:8px;align-items:end;flex-wrap:wrap;margin-top:8px">
  <div><label class="fld">移至空格（跨温区须重新确认）</label><select name="to_cell_id"
    style="width:260px">{opts}</select></div>
  <div><label class="fld">原因</label><input name="reason" style="width:220px"></div>
  <button class="btn violet">提交移格</button></form>""")
            # 待处理移格申请
            if actor.is_admin and r["status"] == M.MOVE_PENDING:
                mrows = conn.execute(
                    """SELECT mr.*, c1.code fcode, c2.code tcode, z2.name tzname
                       FROM move_requests mr JOIN cells c1 ON c1.id=mr.from_cell_id
                       JOIN cells c2 ON c2.id=mr.to_cell_id JOIN zones z2 ON z2.id=c2.zone_id
                       WHERE mr.reservation_id=? AND mr.status='requested'""", (rid,)).fetchall()
                for m in mrows:
                    blocks.append(f"""
<div style="margin-top:10px;padding:10px 12px;border:1px dashed #d9a35b;border-radius:8px;background:#fdf8ef">
  跨温区移格申请 #{m['id']}：{esc(m['fcode'])} → <b>{esc(m['tcode'])}（{esc(m['tzname'])}）</b>
  <span class="muted">原因：{esc(m['reason']) or '—'}</span>
  <form method="post" action="/reservations/{rid}/move-decide" class="inline" style="margin-left:10px">
    <input type="hidden" name="move_id" value="{m['id']}">
    <input type="hidden" name="approve" value="1">
    <button class="btn small-btn">重新确认通过</button></form>
  <form method="post" action="/reservations/{rid}/move-decide" class="inline">
    <input type="hidden" name="move_id" value="{m['id']}">
    <input type="hidden" name="approve" value="0">
    <button class="btn ghost small-btn">驳回，留在原格</button></form>
</div>""")
            # 门磁模拟 + 渗漏
            if actor.is_admin and r["status"] == M.CHECKED_IN and r["cell_id"]:
                blocks.append(f"""
<div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
  <form method="post" action="/reservations/{rid}/door" class="inline">
    <input type="hidden" name="kind" value="open"><button class="btn ghost small-btn">门磁：开门</button></form>
  <form method="post" action="/reservations/{rid}/door" class="inline">
    <input type="hidden" name="kind" value="close"><button class="btn ghost small-btn">门磁：关门</button></form>
  <form method="post" action="/reservations/{rid}/leak" class="inline"
    onsubmit="this.detail.value=prompt('渗漏情况描述','')||''">
    <input type="hidden" name="detail">
    <button class="btn danger small-btn">登记货品渗漏</button></form>
</div>""")
            if not blocks:
                return ""
            return f'<div class="card"><h2>业务办理</h2>{"".join(blocks)}</div>'

        def _detail_photos(self, conn, actor, r) -> str:
            photos = list(conn.execute(
                "SELECT * FROM batch_photos WHERE batch_id=? ORDER BY id", (r["batch_id"],)))
            imgs = "".join(
                f'<figure style="margin:0"><img src="/photos?id={p["id"]}" alt="{esc(p["label"])}">'
                f'<figcaption class="small muted">{esc(p["label"])} · {fmt_dt(p["created_at"])}</figcaption></figure>'
                for p in photos)
            can_up = actor.is_admin or (actor.kind == "tenant" and r["tenant_id"] == actor.id)
            up = ""
            if can_up and r["status"] in (M.CONFIRMED, M.CHECKED_IN, M.PENDING):
                up = f"""
<form method="post" action="/reservations/{r['id']}/photo" style="margin-top:10px;display:flex;gap:8px;align-items:end">
  <div><label class="fld">补充照片说明（本地生成 SVG 占位并保存到 photos/ 目录）</label>
    <input name="label" placeholder="如：解冻后状态" style="width:260px"></div>
  <input type="hidden" name="photo_data" value="">
  <button class="btn ghost small-btn">保存照片</button></form>"""
            return f'<div class="card"><h2>货品照片 <span class="hint">文件存本地，数据库仅存路径</span></h2><div class="photos">{imgs or "<span class=muted>暂无</span>"}</div>{up}</div>'

        def _detail_door(self, conn, actor, r) -> str:
            if not r["cell_id"]:
                return ""
            evs = list(conn.execute(
                "SELECT * FROM door_events WHERE cell_id=? ORDER BY event_at DESC, id DESC LIMIT 12",
                (r["cell_id"],)))
            rows = "".join(
                f'<tr><td>{fmt_full(e["event_at"])}</td>'
                f'<td>{"🔓 开门" if e["kind"]=="open" else "🔒 关门"}</td>'
                f'<td class="muted">{esc(e["note"])}</td></tr>' for e in evs)
            return f"""<div class="card"><h2>开门记录（门磁适配器）</h2>
<table class="tbl"><thead><tr><th>时刻</th><th>事件</th><th>备注</th></tr></thead>
<tbody>{rows or '<tr><td colspan=3 class=muted>暂无</td></tr>'}</tbody></table></div>"""

        def _detail_incidents(self, conn, r) -> str:
            incs = list(conn.execute(
                "SELECT * FROM incidents WHERE reservation_id=? ORDER BY id DESC", (r["id"],)))
            rows = "".join(
                f'<tr><td><span class="pill {"danger" if i["severity"]=="critical" else "warn" if i["severity"]=="warning" else ""}">'
                f'{esc(INCIDENT_LABEL.get(i["kind"], i["kind"]))}</span></td>'
                f'<td>{esc(i["title"])}</td><td class="small muted">{esc(i["detail"])}</td>'
                f'<td class="small">{fmt_full(i["started_at"])}<br>{fmt_full(i["ended_at"])}</td>'
                f'<td>{"已结" if i["resolved"] else "<span class=pill warn>未结</span>"}</td></tr>'
                for i in incs)
            return f"""<div class="card"><h2>异常留痕
              <span class="hint">超容 · 临时降温 · 温度越界 · 断电 · 门未关 · 渗漏 · 提前退租</span></h2>
<table class="tbl"><thead><tr><th>类型</th><th>标题</th><th>详情</th><th>起止</th><th>状态</th></tr></thead>
<tbody>{rows or '<tr><td colspan=5 class=muted>无异常</td></tr>'}</tbody></table></div>"""

        def _detail_chart(self, conn, r) -> str:
            if not r["cell_id"]:
                return ""
            cell = S.require(conn, "cells", r["cell_id"])
            zone = S.require(conn, "zones", cell["zone_id"])
            samples = list(conn.execute(
                """SELECT * FROM temp_samples WHERE zone_id=? AND sampled_at>=? AND sampled_at<=?
                   ORDER BY sampled_at""",
                (zone["id"],
                 min(r["actual_start"], S.iso(S.now() - timedelta(hours=30)))
                     if r["actual_start"] else S.iso(S.now() - timedelta(hours=30)),
                 r["actual_end"] or S.iso(S.now()))))
            points = [{"t": s["sampled_at"], "c": s["celsius"],
                       "breach": s["celsius"] < r["target_temp_min"] or
                                 s["celsius"] > r["target_temp_max"]} for s in samples]
            payload = json.dumps({
                "points": points, "lo": r["target_temp_min"], "hi": r["target_temp_max"],
                "setLo": zone["temp_min"], "setHi": zone["temp_max"]},
                ensure_ascii=False).replace("</", "<\\/")
            return f"""<div class="card"><h2>温度曲线 · {esc(zone['name'])}
  <span class="hint">冰青带＝目标温区 {r['target_temp_min']:g}~{r['target_temp_max']:g}℃；
  紫虚线＝库区物理温区；红点＝越界采样</span></h2>
  <div class="chart-legend">
    <span><i class="sw" style="background:#0d9488"></i>实测温度</span>
    <span><i class="sw dash"></i>库区温区边界</span>
    <span><i class="sw band"></i>目标温区带</span>
    <span><i class="sw breach"></i>越界点</span></div>
  <div class="chart-wrap" id="tempChart"></div>
  <script>window.__CHART__={payload};</script>
  <script src="/static/chart.js"></script></div>"""

        def _detail_billing(self, conn, actor, r) -> str:
            snap = conn.execute("SELECT * FROM billing_snapshots WHERE reservation_id=?",
                                (r["id"],)).fetchone()
            if not snap:
                note = {"checked_in": "计费进行中，退库清点结束后生成费用快照。",
                        "confirmed": "尚未入库复核，计费未开始。",
                        "pending": "预约待确认。"}.get(r["status"], "")
                return f'<div class="card"><h2>账单明细</h2><p class="muted">{esc(note)}</p></div>'
            segs = list(conn.execute(
                "SELECT * FROM billing_segments WHERE reservation_id=? ORDER BY start_at,id",
                (r["id"],)))
            adjs = list(conn.execute(
                "SELECT a.*, s.start_at sstart FROM adjustments a "
                "LEFT JOIN billing_segments s ON s.id=a.segment_id "
                "WHERE a.reservation_id=? ORDER BY a.id", (r["id"],)))
            adj_by_seg: dict = {}
            loose = []
            for a in adjs:
                if a["segment_id"]:
                    adj_by_seg.setdefault(a["segment_id"], []).append(a)
                else:
                    loose.append(a)

            rows_html = []
            for s in segs:
                discs = adj_by_seg.get(s["id"], [])
                disc_html = "".join(
                    f'<div class="disc-row"><span class="chip {a["status"]}">减免 '
                    f'−{money(a["amount"])} · {esc({"pending":"待财务复核","approved":"已批准","rejected":"已驳回"}[a["status"]])}'
                    f'</span><span>{esc(a["reason"])}</span>'
                    + (self._review_buttons(actor, a) if a["status"] == "pending" else "")
                    + '</div>'
                    for a in discs)
                extra = []
                if s["surcharge"]:
                    extra.append(f'附加 +{money(s["surcharge"])}')
                rows_html.append(f"""
<div class="bill-seg {s['seg_type']}">
  <div class="stime">{fmt_full(s['start_at'])}<br>→ {fmt_full(s['end_at'])}</div>
  <div class="sbar">
    <span class="sname">{esc(SEG_LABEL.get(s['seg_type'], s['seg_type']))}</span>
    <span class="snum">{s['hours']:.2f} 小时 · {s['weight_kg']:g} kg</span>
    <span class="snum">占格 {money(s['base_amount'])} ＋ 重量 {money(s['weight_amount'])}
      {'＋ ' + esc(' '.join(extra)) if extra else ''}</span>
    <span class="samt">{money(s['gross_amount'])} 元</span>
  </div>
</div>{disc_html}""")
            loose_html = "".join(
                f'<div class="disc-row"><span class="chip {a["status"]}">整单减免 −{money(a["amount"])}'
                f' · {esc({"pending":"待财务复核","approved":"已批准","rejected":"已驳回"}[a["status"]])}'
                f'</span><span>{esc(a["reason"])}</span>'
                + (self._review_buttons(actor, a) if a["status"] == "pending" else "")
                + '</div>' for a in loose)

            closed = snap["status"] == "closed"
            badge = '<span class="badge-closed">已结账 · 不可重开</span>' if closed else \
                    '<span class="badge-open">待结账</span>'
            settle_btn = ""
            if actor.is_finance and not closed:
                pending_n = conn.execute(
                    "SELECT COUNT(*) c FROM adjustments WHERE reservation_id=? AND status='pending'",
                    (r["id"],)).fetchone()["c"]
                settle_btn = f"""
<form method="post" action="/reservations/{r['id']}/settle" class="inline">
  <button class="btn" {'disabled' if pending_n else ''}>结账并关闭租期</button></form>
<span class="small muted">{f'尚有 {pending_n} 笔减免待复核' if pending_n else ''}</span>"""
            add_adj = ""
            if actor.is_finance and not closed:
                add_adj = f"""
<form method="post" action="/reservations/{r['id']}/adjustment" class="inline"
  style="margin:10px 0;display:flex;gap:8px;align-items:end">
  <div><label class="fld">追加减免金额（元）</label><input name="amount" type="number" step="0.01"
    style="width:130px"></div>
  <div><label class="fld">原因（提交后需复核）</label><input name="reason" style="width:260px"></div>
  <button class="btn ghost small-btn">登记减免</button></form>"""
            body = f"""
<div class="card"><h2>账单明细 {badge} <span class="hint">计费区间按时间轴拆开，减免紧邻对应区间</span></h2>
  <div class="timeline">{''.join(rows_html)}</div>
  {loose_html}
  {add_adj}
  <div class="totals">
    <span>占格时长：{snap['occupancy_hours']:.2f} 小时
      {f'· 提前退租 {snap["early_hours"]:.2f} 小时' if snap['early_hours'] else ''}</span>
    <span>占格费 {money(snap['base_total'])} ＋ 重量附加 {money(snap['weight_total'])}
      ＋ 异常附加 {money(snap['surcharge_total'])} － 已批减免 {money(snap['discount_total'])}</span>
    <span class="grand {'closed' if closed else ''}">应收 {money(snap['final_amount'])} 元</span>
    {settle_btn}
  </div>
</div>"""
            return body

        @staticmethod
        def _review_buttons(actor: Actor, a) -> str:
            if not actor.is_finance:
                return ""
            rid = a["reservation_id"]
            return f"""<form method="post" action="/reservations/{rid}/adjustment-review" class="inline">
  <input type="hidden" name="adjustment_id" value="{a['id']}">
  <input type="hidden" name="approve" value="1">
  <button class="btn small-btn">复核通过</button></form>
<form method="post" action="/reservations/{rid}/adjustment-review" class="inline">
  <input type="hidden" name="adjustment_id" value="{a['id']}">
  <input type="hidden" name="approve" value="0">
  <button class="btn ghost small-btn">驳回</button></form>"""

        # ---------------------------------------------- 温度总览
        def page_temperature(self, actor: Actor, qs: dict):
            conn = self._db()
            try:
                zones = list(conn.execute("SELECT * FROM zones ORDER BY id"))
                cur_id = int(qs.get("zone", [zones[0]["id"] if zones else 0])[0])
                zone = next((z for z in zones if z["id"] == cur_id), zones[0])
                since = S.iso(S.now() - timedelta(hours=30))
                samples = list(conn.execute(
                    "SELECT * FROM temp_samples WHERE zone_id=? AND sampled_at>=? ORDER BY sampled_at",
                    (zone["id"], since)))
                points = [{"t": s["sampled_at"], "c": s["celsius"],
                           "breach": s["celsius"] < zone["temp_min"] or
                                     s["celsius"] > zone["temp_max"]} for s in samples]
                # 越界时长统计
                breach_secs = 0
                prev = None
                for p in points:
                    if prev and p["breach"] and prev["breach"]:
                        breach_secs += (datetime.strptime(p["t"], S.NOW_FMT) -
                                        datetime.strptime(prev["t"], S.NOW_FMT)).total_seconds()
                    prev = p
                latest = points[-1] if points else None
                tabs = "".join(
                    f'<a href="/temperature?zone={z["id"]}" class="{"on" if z["id"]==zone["id"] else ""}">'
                    f'{esc(z["name"])}（{z["temp_min"]:g}~{z["temp_max"]:g}℃）</a>'
                    for z in zones)
                payload = json.dumps({
                    "points": points, "lo": zone["temp_min"], "hi": zone["temp_max"],
                    "setLo": zone["temp_min"], "setHi": zone["temp_max"]},
                    ensure_ascii=False).replace("</", "<\\/")
                body = f"""
<div class="page-head"><h1>温度曲线 · {esc(zone['name'])}</h1>
  <span class="sub">最新 <b>{latest['c'] if latest else '—'}℃</b> ·
  近 30 小时累计越界 {breach_secs/60:.0f} 分钟</span></div>
<div class="card">
  <div class="zone-tabs">{tabs}</div>
  <div class="chart-legend">
    <span><i class="sw" style="background:#0d9488"></i>实测温度</span>
    <span><i class="sw dash"></i>库区温区边界</span>
    <span><i class="sw band"></i>合格温区带</span>
    <span><i class="sw breach"></i>越界点</span></div>
  <div class="chart-wrap" id="tempChart"></div>
  <script>window.__CHART__={payload};</script>
  <script src="/static/chart.js"></script>
</div>"""
                self._html(body, "温度曲线", actor, "temperature")
            finally:
                conn.close()

        # ---------------------------------------------- 异常页
        def page_incidents(self, actor: Actor, qs):
            conn = self._db()
            try:
                incs = list(conn.execute(
                    """SELECT i.*, c.code ccode, z.name zname FROM incidents i
                       LEFT JOIN cells c ON c.id=i.cell_id
                       LEFT JOIN zones z ON z.id=i.zone_id
                       ORDER BY i.id DESC LIMIT(100)"""))
                rows = "".join(
                    f'<tr><td><span class="pill {"danger" if i["severity"]=="critical" else "warn" if i["severity"]=="warning" else ""}">'
                    f'{esc(INCIDENT_LABEL.get(i["kind"], i["kind"]))}</span></td>'
                    f'<td>{esc(i["title"])}</td><td class="small muted">{esc(i["detail"])}</td>'
                    f'<td>{esc(i["zname"] or "")} {esc(i["ccode"] or "")}</td>'
                    f'<td class="small">{fmt_full(i["started_at"])}<br>{fmt_full(i["ended_at"])}</td>'
                    f'<td>{"已结" if i["resolved"] else "<span class=pill warn>未结</span>"}</td></tr>'
                    for i in incs)
                change_form = ""
                if actor.is_admin:
                    zones = list(conn.execute("SELECT * FROM zones ORDER BY id"))
                    zopts = "".join(
                        f'<option value="{z["id"]}">{esc(z["name"])} '
                        f'（当前 {z["temp_min"]:g}~{z["temp_max"]:g}℃）</option>' for z in zones)
                    change_form = f"""
<div class="card" style="max-width:760px"><h2>管理员手工改温 <span class="hint">必须填写原因并留痕</span></h2>
<form method="post" action="/zones/temp-change">
  <div class="form-grid">
    <div><label class="fld">库区</label><select name="zone_id">{zopts}</select></div>
    <div><label class="fld">新温区（℃）</label>
      <div style="display:flex;gap:8px"><input name="temp_min" type="number" step="0.5" value="-25">
      <input name="temp_max" type="number" step="0.5" value="-18"></div></div>
    <div class="full"><label class="fld">改温原因（必填）</label>
      <input name="reason" required placeholder="如：客户工艺变更，已签署确认单"></div>
  </div>
  <div style="margin-top:12px"><button class="btn">提交改温并留痕</button></div>
</form></div>"""
                body = f"""
<div class="page-head"><h1>异常留痕与补偿</h1></div>
{change_form}
<div class="card"><h2>近 100 条异常</h2>
<table class="tbl"><thead><tr><th>类型</th><th>标题</th><th>详情</th><th>位置</th><th>起止</th><th>状态</th></tr></thead>
<tbody>{rows or '<tr><td colspan=6 class=muted>暂无异常</td></tr>'}</tbody></table></div>"""
                self._html(body, "异常留痕", actor, "incidents",
                           flash_ok=qs.get("ok", [""])[0])
            finally:
                conn.close()

        # ---------------------------------------------- 财务复核
        def page_finance(self, actor: Actor, qs):
            self.require(actor.is_finance, "仅财务可访问复核台")
            conn = self._db()
            try:
                pend = list(conn.execute(
                    """SELECT a.*, t.name tname FROM adjustments a
                       JOIN reservations r ON r.id=a.reservation_id
                       JOIN tenants t ON t.id=r.tenant_id
                       WHERE a.status='pending' ORDER BY a.id"""))
                prow = "".join(
                    f'<tr><td>#{a["reservation_id"]} {esc(a["tname"])}</td>'
                    f'<td><span class="pill warn">{esc(INCIDENT_LABEL.get(a["kind"], a["kind"]))}</span> '
                    f'{esc(a["reason"])}</td><td class="num">{money(a["amount"])}</td>'
                    f'<td class="row-actions">'
                    f'<form method="post" action="/reservations/{a["reservation_id"]}/adjustment-review" class="inline">'
                    f'<input type="hidden" name="adjustment_id" value="{a["id"]}">'
                    f'<input type="hidden" name="approve" value="1">'
                    f'<button class="btn small-btn">通过</button></form>'
                    f'<form method="post" action="/reservations/{a["reservation_id"]}/adjustment-review" class="inline">'
                    f'<input type="hidden" name="adjustment_id" value="{a["id"]}">'
                    f'<input type="hidden" name="approve" value="0">'
                    f'<button class="btn ghost small-btn">驳回</button></form></td></tr>'
                    for a in pend)
                bills = list(conn.execute(
                    """SELECT bs.*, r.id rid, t.name tname, b.name bname FROM billing_snapshots bs
                       JOIN reservations r ON r.id=bs.reservation_id
                       JOIN tenants t ON t.id=r.tenant_id
                       JOIN batches b ON b.id=r.batch_id
                       ORDER BY bs.id DESC LIMIT 30"""))
                brow = "".join(
                    f'<tr><td><a href="/reservations/{b["rid"]}">#{b["rid"]}</a></td>'
                    f'<td>{esc(b["tname"])}</td><td>{esc(b["bname"])}</td>'
                    f'<td class="num">{b["occupancy_hours"]:.2f}</td>'
                    f'<td class="num">{money(b["final_amount"])}</td>'
                    f'<td>{"<span class=badge-closed>已结账</span>" if b["status"]=="closed" else "<span class=badge-open>待结账</span>"}</td></tr>'
                    for b in bills)
                body = f"""
<div class="page-head"><h1>财务复核台</h1><span class="sub">减免必须财务复核；结账后租期不可重开</span></div>
<div class="card"><h2>待复核减免（{len(pend)}）</h2>
<table class="tbl"><thead><tr><th>租约</th><th>事由</th><th class="num">金额</th><th>操作</th></tr></thead>
<tbody>{prow or '<tr><td colspan=4 class=muted>没有待复核项</td></tr>'}</tbody></table></div>
<div class="card"><h2>费用快照</h2>
<table class="tbl"><thead><tr><th>单号</th><th>租户</th><th>货品</th><th class="num">占格小时</th>
<th class="num">应收</th><th>状态</th></tr></thead>
<tbody>{brow}</tbody></table></div>"""
                self._html(body, "财务复核", actor, "finance",
                           flash_ok=qs.get("ok", [""])[0])
            finally:
                conn.close()

        # ---------------------------------------------- 资源
        def serve_static(self, path: str):
            fname = os.path.normpath(os.path.join(STATIC_DIR, path[len("/static/"):]))
            if not fname.startswith(STATIC_DIR) or not os.path.isfile(fname):
                return self._send(404, b"not found")
            ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
            with open(fname, "rb") as f:
                self._send(200, f.read(), ctype)

        def serve_photo(self, path: str, actor: Actor):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pid = qs.get("id", [""])[0]
            conn = self._db()
            try:
                p = conn.execute("SELECT p.*, b.tenant_id FROM batch_photos p "
                                 "JOIN batches b ON b.id=p.batch_id WHERE p.id=?", (pid,)).fetchone()
                if not p:
                    return self._send(404, b"not found")
                if actor.kind == "tenant" and p["tenant_id"] != actor.id:
                    return self._send(403, b"forbidden")
                fpath = p["path"]
                if not os.path.isfile(fpath):
                    return self._send(404, b"missing")
                ctype = mimetypes.guess_type(fpath)[0] or "image/svg+xml"
                with open(fpath, "rb") as f:
                    self._send(200, f.read(), ctype)
            finally:
                conn.close()

        # ---------------------------------------------- 工具
        @staticmethod
        def require(cond: bool, msg: str):
            if not cond:
                raise M.PermissionDenied(msg)

        @staticmethod
        def require_tenant_access(actor: Actor, tenant_id: int):
            if actor.kind == "tenant" and actor.id != tenant_id:
                raise M.PermissionDenied("租户只能查看和操作自己的货格与租约")

        @staticmethod
        def _dt(value: str | None):
            if not value:
                return None
            return datetime.strptime(value, "%Y-%m-%dT%H:%M")

    return ThreadingHTTPServer((host, port), Handler)
