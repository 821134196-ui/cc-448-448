"""SQLite 连接层与表结构。

保存内容：库区/货格/租户/预约/批次/开门记录/温度采样/移格/异常/账单区间/
费用快照/管理员改温留痕。采样值与租期均落盘，进程重启后仍可结算。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 库区（虚拟）
CREATE TABLE IF NOT EXISTS zones (
    id           INTEGER PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,          -- 如 A
    name         TEXT NOT NULL,                 -- 如 深冷库A
    zone_type    TEXT NOT NULL,                 -- frozen / chilled
    temp_min     REAL NOT NULL,                 -- 该区允许温度下限 ℃
    temp_max     REAL NOT NULL,                 -- 该区允许温度上限 ℃
    hourly_rate  REAL NOT NULL DEFAULT 0,       -- 占格基础费率 元/小时
    kg_hour_rate REAL NOT NULL DEFAULT 0        -- 重量附加 元/(千克·小时)
);

-- 货格
CREATE TABLE IF NOT EXISTS cells (
    id           INTEGER PRIMARY KEY,
    zone_id      INTEGER NOT NULL REFERENCES zones(id),
    code         TEXT NOT NULL UNIQUE,          -- 如 A-01
    capacity_m3  REAL NOT NULL,
    occupied_m3  REAL NOT NULL DEFAULT 0,       -- 当前已锁定容积（预约确认即占用）
    active_reservation_id INTEGER REFERENCES reservations(id)
);

-- 租户
CREATE TABLE IF NOT EXISTS tenants (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    login    TEXT NOT NULL UNIQUE,              -- 演示用登录名
    token    TEXT NOT NULL UNIQUE               -- 演示用会话令牌
);

-- 管理员/财务（演示：单角色表，role 区分）
CREATE TABLE IF NOT EXISTS staff (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    login TEXT NOT NULL UNIQUE,
    token TEXT NOT NULL UNIQUE,
    role  TEXT NOT NULL                         -- admin / finance
);

-- 预约（也是租约主单）
CREATE TABLE IF NOT EXISTS reservations (
    id                INTEGER PRIMARY KEY,
    tenant_id         INTEGER NOT NULL REFERENCES tenants(id),
    cell_id           INTEGER REFERENCES cells(id),   -- 确认时锁定
    batch_id          INTEGER REFERENCES batches(id),
    status            TEXT NOT NULL DEFAULT 'pending',
        -- pending 已申请待确认 / confirmed 已锁定货格 / checked_in 已入库计费中
        -- move_pending 跨温区移格待确认 / checked_out 已退库待结账 / settled 已结账（不可重开）
        -- rejected 拒绝 / cancelled 取消
    target_temp_min   REAL NOT NULL,
    target_temp_max   REAL NOT NULL,
    planned_start     TEXT NOT NULL,            -- ISO，租用时段起
    planned_end       TEXT NOT NULL,
    actual_start      TEXT,                     -- 入库复核通过时刻：计费起点
    actual_end        TEXT,                     -- 退库清点结束时刻：计费终点
    declared_volume_m3 REAL NOT NULL DEFAULT 0,
    declared_weight_kg REAL NOT NULL DEFAULT 0,
    checked_weight_kg  REAL,                    -- 入库复核重量
    note              TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- 货品批次
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY,
    tenant_id   INTEGER NOT NULL REFERENCES tenants(id),
    name        TEXT NOT NULL,                  -- 如 冻三文鱼块
    sku         TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batches_tenant ON batches(tenant_id);

-- 批次照片（文件存本地，库里只存路径）
CREATE TABLE IF NOT EXISTS batch_photos (
    id        INTEGER PRIMARY KEY,
    batch_id  INTEGER NOT NULL REFERENCES batches(id),
    path      TEXT NOT NULL,
    label     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- 开门记录（门磁适配器产生）
CREATE TABLE IF NOT EXISTS door_events (
    id          INTEGER PRIMARY KEY,
    cell_id     INTEGER NOT NULL REFERENCES cells(id),
    kind        TEXT NOT NULL,                  -- open / close
    event_at    TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'sim',    -- sim 模拟适配器
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_door_cell_time ON door_events(cell_id, event_at);

-- 温度采样（温度适配器产生）
CREATE TABLE IF NOT EXISTS temp_samples (
    id        INTEGER PRIMARY KEY,
    zone_id   INTEGER NOT NULL REFERENCES zones(id),
    celsius   REAL NOT NULL,
    sampled_at TEXT NOT NULL,
    source    TEXT NOT NULL DEFAULT 'sim'
);
CREATE INDEX IF NOT EXISTS idx_samples_zone_time ON temp_samples(zone_id, sampled_at);

-- 移格申请
CREATE TABLE IF NOT EXISTS move_requests (
    id             INTEGER PRIMARY KEY,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    from_cell_id   INTEGER NOT NULL REFERENCES cells(id),
    to_cell_id     INTEGER NOT NULL REFERENCES cells(id),
    cross_zone     INTEGER NOT NULL,            -- 1 = 跨温区，必须重新确认
    status         TEXT NOT NULL DEFAULT 'requested',
        -- requested / approved / rejected
    reason         TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    decided_at     TEXT,
    decided_by     INTEGER REFERENCES staff(id)
);

-- 异常留痕：超容/临时降温/温度越界/断电补偿/门未关/渗漏/提前退租
CREATE TABLE IF NOT EXISTS incidents (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,
        -- overload 超容 / temp_pull_down 临时降温 / temp_breach 温度越界
        -- power_outage 断电补偿 / door_left_open 门未关 / leakage 货品渗漏
        -- early_checkout 提前退租 / manual_temp_change 管理员手工改温
    zone_id      INTEGER REFERENCES zones(id),
    cell_id      INTEGER REFERENCES cells(id),
    reservation_id INTEGER REFERENCES reservations(id),
    severity     TEXT NOT NULL DEFAULT 'warning', -- info / warning / critical
    title        TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    metric_value REAL,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    resolved     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_incidents_resv ON incidents(reservation_id);
CREATE INDEX IF NOT EXISTS idx_incidents_open ON incidents(kind, resolved);

-- 账单：计费区间（沿时间轴拆开，每段独立计费）
CREATE TABLE IF NOT EXISTS billing_segments (
    id             INTEGER PRIMARY KEY,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    seg_type       TEXT NOT NULL,   -- normal 正常 / overload 超容 / pull_down 临时降温 / breach 越界 / outage 断电
    start_at       TEXT NOT NULL,
    end_at         TEXT NOT NULL,
    hours          REAL NOT NULL,
    weight_kg      REAL NOT NULL,
    base_amount    REAL NOT NULL,   -- 占格时长费
    weight_amount  REAL NOT NULL,   -- 重量附加费
    surcharge      REAL NOT NULL DEFAULT 0,  -- 附加（如临时降温）
    gross_amount   REAL NOT NULL,   -- 段内应收（减免前）
    incident_id    INTEGER REFERENCES incidents(id)
);
CREATE INDEX IF NOT EXISTS idx_seg_resv ON billing_segments(reservation_id);

-- 减免（财务复核）
CREATE TABLE IF NOT EXISTS adjustments (
    id             INTEGER PRIMARY KEY,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    segment_id     INTEGER REFERENCES billing_segments(id), -- 可空=整单减免
    amount         REAL NOT NULL,                  -- 正数=减免金额
    kind           TEXT NOT NULL,                  -- outage 断电补偿 / goodwill / early_refund
    reason         TEXT NOT NULL,
    dedupe_key     TEXT NOT NULL DEFAULT '',       -- 自动生成减免按 类型:区间起:区间止 去重
    status         TEXT NOT NULL DEFAULT 'pending', -- pending / approved / rejected
    reviewer_id    INTEGER REFERENCES staff(id),
    reviewed_at    TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adj_resv ON adjustments(reservation_id);

-- 费用快照：退库清点后生成；已结账不可重开
CREATE TABLE IF NOT EXISTS billing_snapshots (
    id             INTEGER PRIMARY KEY,
    reservation_id INTEGER NOT NULL UNIQUE,
    occupancy_hours REAL NOT NULL,
    base_total     REAL NOT NULL,
    weight_total   REAL NOT NULL,
    surcharge_total REAL NOT NULL,
    discount_total REAL NOT NULL,
    final_amount   REAL NOT NULL,
    early_hours    REAL NOT NULL DEFAULT 0,
    detail_json    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',   -- open / closed
    created_at     TEXT NOT NULL,
    closed_at      TEXT
);

-- 简易审计日志（手工改温原因、复核动作等）
CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    target    TEXT NOT NULL DEFAULT '',
    detail    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    # isolation_level=None：autocommit 模式，事务一律由 tx() 用 BEGIN IMMEDIATE 显式开启
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def init_db(db_path: str | Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection):
    """串行化事务（BEGIN IMMEDIATE），用于货格争抢等关键段。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise
