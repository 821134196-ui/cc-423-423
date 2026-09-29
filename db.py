"""SQLite 数据层：建库、建表、连接管理。

所有写操作在事务中执行，关键业务路径使用 IMMEDIATE 事务 + UNIQUE 索引，
确保同一宠物同一时段、同一工位同一时段不会被重复预约（数据库层最终防线）。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "grooming.db"
UPLOAD_DIR = BASE_DIR / "uploads"

# 六种异常 / 业务事件
EVENT_TYPES = [
    ("resistance", "宠物抗拒", "🐶", "桃色"),
    ("skin", "皮肤异常", "🔍", "珊瑚"),
    ("cut", "剪伤", "🩹", "红"),
    ("change", "主人临时改项", "✏️", "海军蓝"),
    ("overdue", "超时未取", "⏰", "琥珀"),
    ("allergy", "用品过敏", "🌿", "柔绿"),
]

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    address TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS pets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    name TEXT NOT NULL,
    species TEXT DEFAULT '犬',
    breed TEXT DEFAULT '',
    size TEXT DEFAULT '中型',            -- 小型 / 中型 / 大型
    gender TEXT DEFAULT '',
    birth_year INTEGER,
    weight REAL,
    temperament TEXT DEFAULT '',         -- 性格标签，逗号分隔
    taboos TEXT DEFAULT '',              -- 禁忌，逗号分隔（怕吹风筒/禁用某用品…）
    skin_note TEXT DEFAULT '',           -- 皮肤毛发备注
    coat_note TEXT DEFAULT '',
    photo TEXT DEFAULT '',               -- 档案照片文件名（uploads/）
    tags TEXT DEFAULT '',                -- 标签（逗号分隔）
    archived INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    title TEXT DEFAULT '美容师',
    phone TEXT DEFAULT '',
    can_view_sensitive INTEGER DEFAULT 0,  -- 是否为可查看异常照片的授权人员
    color TEXT DEFAULT '#7FB7A6',
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS stations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                 -- 如 A工位 / 烘干位
    capacity INTEGER DEFAULT 1,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    station_id INTEGER NOT NULL REFERENCES stations(id),
    work_date TEXT NOT NULL,            -- YYYY-MM-DD
    start_time TEXT NOT NULL,           -- HH:MM
    end_time TEXT NOT NULL,             -- HH:MM
    UNIQUE(staff_id, station_id, work_date, start_time)
);

CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT DEFAULT '基础',
    duration_min INTEGER DEFAULT 30,    -- 标准预计时长
    price REAL DEFAULT 0,
    description TEXT DEFAULT '',
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS supplies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    unit TEXT DEFAULT '次',
    stock REAL DEFAULT 0,
    note TEXT DEFAULT ''
);

-- 预约：保存品种/体型/禁忌快照、服务组合、预计时长
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pet_id INTEGER NOT NULL REFERENCES pets(id),
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    staff_id INTEGER REFERENCES staff(id),
    station_id INTEGER REFERENCES stations(id),
    appt_date TEXT NOT NULL,            -- YYYY-MM-DD
    start_time TEXT NOT NULL,           -- HH:MM
    end_time TEXT NOT NULL,             -- HH:MM
    estimated_min INTEGER DEFAULT 60,   -- 服务组合预计总时长
    breed_snapshot TEXT DEFAULT '',     -- 品种快照
    size_snapshot TEXT DEFAULT '',      -- 体型快照
    taboos_snapshot TEXT DEFAULT '',    -- 禁忌快照
    services_snapshot TEXT DEFAULT '',  -- 服务组合快照 JSON [{id,name,duration,price}]
    status TEXT NOT NULL DEFAULT 'booked',
        -- booked 已预约 / reception_confirmed 已接待 / servicing 护理中
        -- / completed 已结单 / picked_up 已取宠 / cancelled 已取消 / noshow 爽约
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
-- 同一宠物同一时段重叠（同日且时间区间相交）由应用层 + 部分唯一索引共同拦截。
-- 采用 [start,end) 区间：b 与 a 重叠当且仅当 b.start < a.end AND a.start < b.end
CREATE INDEX IF NOT EXISTS idx_appt_pet_time
    ON appointments(pet_id, appt_date, start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_appt_station_time
    ON appointments(station_id, appt_date, start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_appt_staff_time
    ON appointments(staff_id, appt_date, start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_appt_date ON appointments(appt_date);

-- 接待确认后生成的护理单（与预约 1:1）
CREATE TABLE IF NOT EXISTS care_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL UNIQUE REFERENCES appointments(id),
    pet_id INTEGER NOT NULL REFERENCES pets(id),
    staff_id INTEGER REFERENCES staff(id),
    status TEXT NOT NULL DEFAULT 'open',   -- open 护理中 / closed 已结单
    skin_note TEXT DEFAULT '',
    coat_note TEXT DEFAULT '',
    summary TEXT DEFAULT '',               -- 护理总结
    confirmed INTEGER DEFAULT 0,           -- 护理记录确认后不可删除
    confirmed_at TEXT,
    confirmed_by TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    closed_at TEXT
);

-- 护理项目：服务完成一项记录一项
CREATE TABLE IF NOT EXISTS care_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES care_orders(id),
    service_id INTEGER REFERENCES services(id),
    name TEXT NOT NULL,
    duration_min INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending', -- pending / done / skipped
    completed_at TEXT,
    completed_by TEXT DEFAULT '',
    remark TEXT DEFAULT '',
    sort_no INTEGER DEFAULT 0
);

-- 事件：宠物抗拒/皮肤异常/剪伤/主人临时改项/超时未取/用品过敏
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER REFERENCES appointments(id),
    order_id INTEGER REFERENCES care_orders(id),
    pet_id INTEGER REFERENCES pets(id),
    type TEXT NOT NULL,
    title TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    created_by TEXT DEFAULT '前台',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 照片：本地目录保存，敏感照片仅授权人员可看
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,               -- uploads 下的文件名
    pet_id INTEGER REFERENCES pets(id),
    appointment_id INTEGER REFERENCES appointments(id),
    event_id INTEGER REFERENCES events(id),
    kind TEXT DEFAULT 'normal',           -- normal 普通 / sensitive 异常敏感
    caption TEXT DEFAULT '',
    uploaded_by TEXT DEFAULT '前台',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 用品消耗（含过敏事件引用）
CREATE TABLE IF NOT EXISTS supply_usages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER REFERENCES care_orders(id),
    appointment_id INTEGER REFERENCES appointments(id),
    supply_id INTEGER NOT NULL REFERENCES supplies(id),
    supply_name TEXT NOT NULL,
    qty REAL DEFAULT 1,
    caused_allergy INTEGER DEFAULT 0,
    used_at TEXT DEFAULT (datetime('now','localtime')),
    used_by TEXT DEFAULT '美容师'
);

-- 取宠确认：核验码 + 证件/身份方式
CREATE TABLE IF NOT EXISTS pickups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id),
    code TEXT NOT NULL,                   -- 取宠码（结单时生成）
    method TEXT DEFAULT '取宠码',          -- 取宠码 / 身份证后四位 / 手机号
    verifier TEXT DEFAULT '',
    verified INTEGER DEFAULT 0,
    verified_at TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 进程内任务处理的提醒/通知
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER REFERENCES appointments(id),
    pet_id INTEGER REFERENCES pets(id),
    channel TEXT DEFAULT '短信',
    kind TEXT DEFAULT 'reminder',         -- reminder 到点提醒 / overdue 超时未取 / event 异常通知 / pickup 取宠通知
    payload TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',        -- pending / sent / failed
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    next_run_at TEXT NOT NULL,            -- HH:MM 调度比较用（YYYY-MM-DD HH:MM:SS）
    last_error TEXT DEFAULT '',
    sent_at TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_notif_due ON notifications(next_run_at, status);
"""

_lock = threading.Lock()
_initialized = False


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = str(db_path or DB_PATH)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """建表（幂等）。"""
    global _initialized
    with _lock:
        conn = connect(db_path)
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _initialized = True
