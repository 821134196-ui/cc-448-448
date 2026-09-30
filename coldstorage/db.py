"""SQLite 连接与建表脚本。保存租期、预约、采样值、开门记录与账单快照。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional

from . import config

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS zones (
    code        TEXT PRIMARY KEY,           -- F / C / AC
    name        TEXT NOT NULL,
    temp_min    REAL NOT NULL,
    temp_max    REAL NOT NULL,
    target_temp REAL NOT NULL,
    rate        REAL NOT NULL               -- 元 / m³ / 小时
);

CREATE TABLE IF NOT EXISTS cells (
    id          TEXT PRIMARY KEY,           -- 例 F-01
    zone_code   TEXT NOT NULL REFERENCES zones(code),
    volume_m3   REAL NOT NULL,
    seq         INTEGER NOT NULL,           -- 库区内纵向排序
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    pw_hash     TEXT NOT NULL,
    role        TEXT NOT NULL CHECK(role IN ('admin','finance','tenant')),
    tenant_id   TEXT                        -- role=tenant 时指向 tenants.id
);

CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    contact     TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id),
    zone_code   TEXT NOT NULL REFERENCES zones(code),
    cell_id     TEXT REFERENCES cells(id),  -- 确认时锁定具体货格
    need_volume_m3 REAL NOT NULL,
    target_temp_min REAL NOT NULL,          -- 目标温区（确认时从温区拷贝快照）
    target_temp_max REAL NOT NULL,
    start_ts    TEXT NOT NULL,              -- ISO，租用时段起
    end_ts      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','confirmed','checked_in','checked_out','settled','cancelled')),
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_res_cell_time ON reservations(cell_id, start_ts, end_ts);
CREATE INDEX IF NOT EXISTS idx_res_tenant ON reservations(tenant_id);

-- 货品批次：一次预约可含多个批次，入库复核记录实称重量
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    name        TEXT NOT NULL,              -- 虚构货品名
    declared_weight_kg REAL,                -- 申报重量
    actual_weight_kg REAL,                  -- 入库复核实称
    volume_m3   REAL,                       -- 实测体积
    photo_path  TEXT,                       -- 本地文件
    inbound_note TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

-- 租期（lease）：入库复核通过后创建，计费只认 lease
CREATE TABLE IF NOT EXISTS leases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id  INTEGER NOT NULL UNIQUE REFERENCES reservations(id),
    cell_id         TEXT NOT NULL REFERENCES cells(id),
    zone_code       TEXT NOT NULL,
    volume_m3       REAL NOT NULL,          -- 锁定容量
    weight_kg       REAL NOT NULL DEFAULT 0,
    temp_min        REAL NOT NULL,
    temp_max        REAL NOT NULL,
    started_at      TEXT NOT NULL,          -- 计费起点（复核通过时刻）
    ended_at        TEXT,                   -- 退库清点时刻
    status          TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','closed')),
    overcapacity_m3 REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_lease_cell ON leases(cell_id);

-- 移格申请。同温区空格可直接执行；跨温区必须管理员重新确认。
CREATE TABLE IF NOT EXISTS moves (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lease_id    INTEGER NOT NULL REFERENCES leases(id),
    from_cell   TEXT NOT NULL REFERENCES cells(id),
    to_cell     TEXT NOT NULL REFERENCES cells(id),
    same_zone   INTEGER NOT NULL,
    reason      TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'requested'
        CHECK(status IN ('requested','approved','rejected','done','cancelled')),
    decided_by  TEXT,
    decided_at  TEXT,
    created_at  TEXT NOT NULL,
    done_at     TEXT
);

-- 开门记录（模拟门磁适配器产生）
CREATE TABLE IF NOT EXISTS door_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cell_id     TEXT NOT NULL REFERENCES cells(id),
    opened_at   TEXT NOT NULL,
    closed_at   TEXT,
    source      TEXT NOT NULL DEFAULT 'sim',
    note        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_door_cell ON door_events(cell_id, opened_at);

-- 温度/断电采样（模拟温度适配器产生）
CREATE TABLE IF NOT EXISTS temp_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cell_id     TEXT NOT NULL REFERENCES cells(id),
    ts          TEXT NOT NULL,
    temp_c      REAL,                       -- NULL 表示断电（无读数）
    power_on    INTEGER NOT NULL DEFAULT 1,
    source      TEXT NOT NULL DEFAULT 'sim'
);
CREATE INDEX IF NOT EXISTS idx_temp_cell_ts ON temp_samples(cell_id, ts);

-- 管理员手工改温（必须填写原因）
CREATE TABLE IF NOT EXISTS temp_overrides (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cell_id     TEXT NOT NULL REFERENCES cells(id),
    old_min     REAL NOT NULL,
    old_max     REAL NOT NULL,
    new_min     REAL NOT NULL,
    new_max     REAL NOT NULL,
    reason      TEXT NOT NULL,
    by_user     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
-- 生效中的手工温区覆盖（每格至多一条）；退租或取消时删除
CREATE TABLE IF NOT EXISTS cell_temp_overrides (
    cell_id     TEXT PRIMARY KEY REFERENCES cells(id),
    temp_min    REAL NOT NULL,
    temp_max    REAL NOT NULL,
    override_id INTEGER NOT NULL REFERENCES temp_overrides(id)
);

-- 异常/操作留痕：超容、临时降温、越界、断电、门未关、渗漏、提前退租
CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lease_id    INTEGER REFERENCES leases(id),
    cell_id     TEXT REFERENCES cells(id),
    kind        TEXT NOT NULL CHECK(kind IN (
                    'overcapacity','temp_pull','excursion','outage',
                    'door_open','leak','early_exit')),
    ts          TEXT NOT NULL,
    start_ts    TEXT,
    end_ts      TEXT,
    detail      TEXT NOT NULL DEFAULT '',
    amount      REAL NOT NULL DEFAULT 0,     -- 正=加收，负=减免/补偿
    billing_tag TEXT,                       -- 结算时写入快照项 id，避免重复入账
    reviewed    INTEGER NOT NULL DEFAULT 0, -- 负数减免需财务复核
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inc_lease ON incidents(lease_id);

-- 费用快照：退库清点结束生成；已结账不可重开
CREATE TABLE IF NOT EXISTS bills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lease_id    INTEGER NOT NULL UNIQUE REFERENCES leases(id),
    tenant_id   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    period_from TEXT NOT NULL,
    period_to   TEXT NOT NULL,
    subtotal    REAL NOT NULL,
    discount_total REAL NOT NULL,
    surcharge_total REAL NOT NULL,
    total       REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'issued'
        CHECK(status IN ('issued','reviewed','paid')),
    detail_json TEXT NOT NULL
);

-- 减免复核记录
CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id     INTEGER REFERENCES bills(id),
    incident_id INTEGER REFERENCES incidents(id),
    action      TEXT NOT NULL,              -- approve / reject
    by_user     TEXT NOT NULL,
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
"""

_lock = threading.Lock()
_initialized: set[Path] = set()


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None：事务由服务层显式 BEGIN IMMEDIATE 控制（容量锁需要）
    conn = sqlite3.connect(str(path), timeout=15, check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    with _lock:
        if path not in _initialized:
            conn.executescript(_SCHEMA)
            _initialized.add(path)
    return conn


def init_db(db_path: Optional[Path] = None) -> None:
    conn = connect(db_path)
    conn.close()
