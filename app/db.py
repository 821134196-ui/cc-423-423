"""SQLite 连接管理与建表脚本。

约束要点：
- ``appointments`` 上的部分唯一索引保证同一宠物同一时段不重复预约；
- 护理记录确认后 ``confirmed_at`` 非空，业务层据此拒绝删除；
- 照片 ``sensitivity`` 区分普通照片与异常照片，异常照片需授权查看。
"""
import sqlite3
import threading

from . import config

_lock = threading.RLock()


def connect(db_path=None):
    conn = sqlite3.connect(
        str(db_path or config.DB_PATH),
        timeout=10,
        isolation_level=None,  # 自动提交，事务由业务层显式 BEGIN/COMMIT
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pets (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    name TEXT NOT NULL,
    species TEXT NOT NULL,            -- 犬/猫...
    breed TEXT NOT NULL,             -- 品种
    size TEXT NOT NULL,              -- 体型：小/中/大
    gender TEXT DEFAULT '',
    birth_year INTEGER,
    photo_file TEXT DEFAULT '',      -- 档案头像（普通照片）
    tab_tags TEXT DEFAULT '',        -- 逗号分隔标签：怕生/护食/长毛...
    skin_coat_note TEXT DEFAULT '',  -- 皮肤毛发备注
    contraindications TEXT DEFAULT '', -- 禁忌
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS groomers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    title TEXT DEFAULT '',
    active INTEGER DEFAULT 1
);

-- 工位容量：每个时段（YYYY-MM-DD HH:MM 粒度，粒度见 SLOT_MINUTES）全店可容纳的在做美容数。
CREATE TABLE IF NOT EXISTS station_slots (
    id INTEGER PRIMARY KEY,
    slot_time TEXT NOT NULL UNIQUE,  -- 预约开始时间
    capacity INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL, -- 预计时长
    price REAL NOT NULL,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY,
    pet_id INTEGER NOT NULL REFERENCES pets(id),
    groomer_id INTEGER NOT NULL REFERENCES groomers(id),
    start_time TEXT NOT NULL,        -- 'YYYY-MM-DD HH:MM'
    end_time TEXT NOT NULL,
    breed_snapshot TEXT DEFAULT '',  -- 预约时保存的品种
    size_snapshot TEXT DEFAULT '',   -- 预约时保存的体型
    contraindication_snapshot TEXT DEFAULT '', -- 预约时保存的禁忌
    estimated_minutes INTEGER NOT NULL,        -- 服务组合预计时长
    status TEXT NOT NULL DEFAULT 'booked',
    -- booked 已预约 / reception_confirmed 接待确认 / in_care 护理中 /
    -- done 已完成 / picked_up 已取宠 / canceled 已取消 / no_show
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 同一宠物同一开始时段不得重复预约（排除取消单）。
CREATE UNIQUE INDEX IF NOT EXISTS ux_appt_pet_slot
    ON appointments(pet_id, start_time)
    WHERE status NOT IN ('canceled');

-- 工位排程按时间轴查询用。
CREATE INDEX IF NOT EXISTS ix_appt_slot ON appointments(start_time);

CREATE TABLE IF NOT EXISTS appointment_services (
    id INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    service_id INTEGER NOT NULL REFERENCES services(id),
    price_snapshot REAL NOT NULL,
    UNIQUE(appointment_id, service_id)
);

-- 护理单：接待确认后生成，一单对应一预约。
CREATE TABLE IF NOT EXISTS care_records (
    id INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL UNIQUE REFERENCES appointments(id),
    status TEXT NOT NULL DEFAULT 'open',  -- open 护理中 / closed 已结单
    confirmed_at TEXT,               -- 结单（护理记录确认）时间，非空后不可删除
    picked_up_at TEXT,
    pickup_verified_by TEXT DEFAULT '',
    pickup_code TEXT DEFAULT '',
    total_price REAL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 护理项目：服务项目完成一项记录一项。
CREATE TABLE IF NOT EXISTS care_items (
    id INTEGER PRIMARY KEY,
    care_record_id INTEGER NOT NULL REFERENCES care_records(id) ON DELETE CASCADE,
    service_id INTEGER NOT NULL REFERENCES services(id),
    name_snapshot TEXT NOT NULL,
    price_snapshot REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', -- pending / done
    completed_at TEXT,
    completed_by TEXT DEFAULT '',
    UNIQUE(care_record_id, service_id)
);

-- 事件：宠物抗拒 / 皮肤异常 / 剪伤 / 主人临时改项 / 超时未取 / 用品过敏。
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    appointment_id INTEGER REFERENCES appointments(id),
    care_record_id INTEGER REFERENCES care_records(id),
    pet_id INTEGER REFERENCES pets(id),
    kind TEXT NOT NULL,
    -- resistance 抗拒 / skin 皮肤异常 / cut 剪伤 / owner_change 主人临时改项
    -- / overdue_pickup 超时未取 / supply_allergy 用品过敏
    detail TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    created_by TEXT DEFAULT '接待'
);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    file_name TEXT NOT NULL,          -- PHOTO_DIR 下的文件名
    kind TEXT NOT NULL DEFAULT 'pet', -- pet 档案 / incident 异常
    sensitivity TEXT NOT NULL DEFAULT 'normal', -- normal / restricted
    pet_id INTEGER REFERENCES pets(id),
    appointment_id INTEGER REFERENCES appointments(id),
    event_id INTEGER REFERENCES events(id),
    caption TEXT DEFAULT '',
    uploaded_by TEXT DEFAULT '前台',
    uploaded_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 用品消耗记录。
CREATE TABLE IF NOT EXISTS supply_usages (
    id INTEGER PRIMARY KEY,
    care_record_id INTEGER NOT NULL REFERENCES care_records(id) ON DELETE CASCADE,
    supply_name TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 1,
    unit TEXT DEFAULT '份',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- 进程内通知（取宠提醒/超时提醒等），后台任务扫描并重试。
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    appointment_id INTEGER REFERENCES appointments(id),
    channel TEXT DEFAULT 'sms',
    payload TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending', -- pending / sent / failed
    attempts INTEGER NOT NULL DEFAULT 0,
    next_run_at TEXT NOT NULL,           -- 'YYYY-MM-DD HH:MM:SS'
    last_error TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS auth_users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,          -- front 前台 / groomer 美容师 / manager 店长
    can_view_incident INTEGER NOT NULL DEFAULT 0, -- 是否可查看异常照片
    password TEXT NOT NULL
);
"""


def init_db(db_path=None):
    """建表并写入默认工位容量（幂等）。"""
    with _lock:
        conn = connect(db_path)
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()


if __name__ == "__main__":
    config.ensure_dirs()
    init_db()
    print(f"数据库已初始化：{config.DB_PATH}")
