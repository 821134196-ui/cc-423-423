"""演示数据：宠物与联系人、服务/美容师，并预置预约、护理和异常样例。

幂等：仅在库内无宠物档案时执行；``run.py --reset`` 会删库重建。
异常样例照片由 :func:`_solid_png` 现造的小尺寸 PNG 充当，无需外部素材。
"""
import struct
import zlib
from datetime import datetime, timedelta

from . import auth, config, operations, photos as photos_mod
from .db import connect

# 柔和配色，供造图与档案区分。
_SWATCHES = {
    "peach": (246, 198, 168),
    "green": (143, 191, 159),
    "navy": (43, 58, 85),
    "rose": (224, 143, 143),
    "sand": (233, 214, 178),
}


def _solid_png(rgb, size=96):
    """生成一张纯色带圆角色块观感的 PNG（标准库 zlib 压缩，无第三方依赖）。"""
    r, g, b = rgb
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type 0
        for x in range(size):
            # 对角微纹理，避免大片纯色在缩略图里看不出边界。
            shade = 18 if (x // 12 + y // 12) % 2 == 0 else 0
            raw.extend((min(r + shade, 255), min(g + shade, 255), min(b + shade, 255)))

    def chunk(tag, data):
        out = struct.pack(">I", len(data)) + tag + data
        return out + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def already_seeded(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM pets").fetchone()["n"] > 0


def seed(force=False):
    config.ensure_dirs()
    conn = connect()
    try:
        if already_seeded(conn) and not force:
            return False
        auth.ensure_users(conn)
        _seed_core(conn)
        return True
    finally:
        conn.close()


def _seed_core(conn):
    # ---- 联系人与宠物（演示数据）----
    contacts = [
        ("王秀兰", "13800000001", "老顾客，介意排队等太久"),
        ("陈昊", "13800000002", "周末取宠，常迟到"),
        ("李棠", "13800000003", "对香氛类用品敏感"),
        ("赵鹏", "13800000004", "要求修剪后拍照确认"),
    ]
    contact_ids = []
    for name, phone, note in contacts:
        cur = conn.execute(
            "INSERT INTO contacts(name, phone, note) VALUES (?,?,?)", (name, phone, note))
        contact_ids.append(cur.lastrowid)

    pets_def = [
        # contact_idx, 名字, 品种, 体型, 标签, 皮肤毛发备注, 禁忌, 色块
        (0, "奶茶", "比熊犬", "小", "怕生,白色长毛,易泪痕",
         "腹部毛发稀疏，皮肤偏干；耳后有轻微皮屑", "禁用含香精浴液；吹风温度不可过高", "peach"),
        (1, "豆豆", "彭布罗克柯基", "中", "活泼,掉毛多,护食",
         "被毛健康，换毛期脱毛明显", "修剪指甲时后脚敏感，需两人配合", "green"),
        (2, "布丁", "英国短毛猫", "小", "胆小,怕水声,蓝灰短毛",
         "尾根有一小块旧癣痕，已长毛", "禁用犬用精油；严禁强制按入水中", "navy"),
        (3, "拿铁", "金毛寻回犬", "大", "温顺,长毛,耳朵下垂",
         "耳内偏潮湿，需重点吹干", "耳道有过炎症，耳道护理只用低敏清洁液", "sand"),
    ]
    pet_ids = []
    for ci, name, breed, size, tags, skin, contra, color in pets_def:
        avatar = photos_mod.save_photo(
            _solid_png(_SWATCHES[color]), kind="pet", sensitivity="normal",
            caption=f"{name}档案照", uploaded_by="前台", conn=conn)
        cur = conn.execute(
            """INSERT INTO pets(contact_id, name, species, breed, size, gender,
                 birth_year, photo_file, tab_tags, skin_coat_note, contraindications)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (contact_ids[ci], name, "猫" if "猫" in breed else "犬", breed, size,
             "母" if name in ("奶茶", "布丁") else "公",
             2020 if name in ("奶茶",) else 2021,
             f"pet_{avatar}.png", tags, skin, contra))
        pid = cur.lastrowid
        conn.execute("UPDATE photos SET pet_id=? WHERE id=?", (pid, avatar))
        pet_ids.append(pid)

    # ---- 美容师 ----
    groomer_ids = []
    for name, title in [("阿梅", "高级美容师"), ("小周", "美容师"), ("安娜", "猫咪专属美容师")]:
        cur = conn.execute("INSERT INTO groomers(name, title) VALUES (?,?)", (name, title))
        groomer_ids.append(cur.lastrowid)

    # ---- 服务项目（预计时长/单价）----
    svc_ids = {}
    for name, minutes, price in [
        ("基础洗浴", 40, 88),
        ("精细修剪", 90, 168),
        ("耳道护理", 20, 40),
        ("肛门腺护理", 15, 30),
        ("猫咪舒缓SPA", 60, 128),
        ("牙齿清洁", 25, 60),
    ]:
        cur = conn.execute(
            "INSERT INTO services(name, duration_minutes, price) VALUES (?,?,?)",
            (name, minutes, price))
        svc_ids[name] = cur.lastrowid

    # ---- 工位容量与时段锚点 ----
    # 演示样例的时段相对「当前整点」锚定，保证任何时刻启动都处于合理状态：
    #   t0-1h 已结单待取宠；t0 护理中 + 待接待；t0+1h 空闲；t0+3h 已预约。
    now_hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    t_picked = now_hour - timedelta(hours=1)
    t_live = now_hour
    t_free = now_hour + timedelta(hours=1)
    t_next = now_hour + timedelta(hours=3)

    for hour in range(9, 19):
        operations.set_slot_capacity(
            conn, now_hour.replace(hour=hour).strftime("%Y-%m-%d %H:%M"), 3)
    for t in (t_picked, t_live, t_free, t_next):
        operations.set_slot_capacity(conn, t.strftime("%Y-%m-%d %H:%M"), 3)

    # ======================================================================
    # 样例 1：奶茶，护理中（1/2 项目完成），含皮肤异常/抗拒事件 + 用品消耗
    # ======================================================================
    a1 = operations.create_appointment(
        conn, pet_ids[0], groomer_ids[0], t_live.strftime("%Y-%m-%d %H:%M"),
        [svc_ids["基础洗浴"], svc_ids["精细修剪"]], note="主人提醒腹部少推毛")
    operations.confirm_reception(conn, a1, actor="小千")
    care1 = conn.execute("SELECT id FROM care_records WHERE appointment_id=?", (a1,)).fetchone()["id"]
    item1 = conn.execute(
        "SELECT id FROM care_items WHERE care_record_id=? ORDER BY id", (care1,)).fetchall()
    operations.complete_care_item(conn, care1, item1[0]["id"], actor="阿梅")  # 洗浴完成
    operations.use_supply(conn, care1, "燕麦低敏浴液", 1, "份", actor="阿梅")
    e_skin = operations.add_event(
        conn, a1, "skin", "腹部发现一处约指甲盖大红斑，已暂停推毛并告知主人", actor="阿梅")
    operations.attach_incident_photo(
        conn, e_skin, _solid_png(_SWATCHES["rose"]),
        "腹部红斑特写（异常照片，授权可见）", authorized=True, actor="阿梅")
    e_res = operations.add_event(conn, a1, "resistance", "吹风时抗拒明显，已改为低温小风慢吹", actor="阿梅")
    operations.attach_incident_photo(
        conn, e_res, _solid_png(_SWATCHES["sand"]),
        "抗拒记录照（异常照片，授权可见）", authorized=True, actor="阿梅")

    # ======================================================================
    # 样例 2：豆豆，与样例 1 同时段已预约待接待（演示工位容量 >1）
    # ======================================================================
    operations.create_appointment(
        conn, pet_ids[1], groomer_ids[1], t_live.strftime("%Y-%m-%d %H:%M"),
        [svc_ids["基础洗浴"], svc_ids["耳道护理"]], note="到店前先遛 10 分钟")

    # ======================================================================
    # 样例 3：布丁，上一时段已结单待取宠，含剪伤事件（尚未超时）
    # ======================================================================
    a3 = operations.create_appointment(
        conn, pet_ids[2], groomer_ids[2], t_picked.strftime("%Y-%m-%d %H:%M"),
        [svc_ids["猫咪舒缓SPA"], svc_ids["肛门腺护理"]], note="主人会晚到")
    operations.confirm_reception(conn, a3, actor="小千")
    care3 = conn.execute("SELECT id FROM care_records WHERE appointment_id=?", (a3,)).fetchone()["id"]
    for row in conn.execute("SELECT id FROM care_items WHERE care_record_id=? ORDER BY id", (care3,)):
        operations.complete_care_item(conn, care3, row["id"], actor="安娜")
    e_cut = operations.add_event(conn, a3, "cut", "剪指甲时指尖轻微渗血，已止血处理并电话告知主人", actor="安娜")
    operations.attach_incident_photo(
        conn, e_cut, _solid_png(_SWATCHES["rose"]),
        "指甲指尖轻微剪伤（异常照片，授权可见）", authorized=True, actor="安娜")
    operations.use_supply(conn, care3, "止血粉", 1, "小包", actor="安娜")
    operations.close_care_record(conn, care3, actor="安娜")

    # ======================================================================
    # 样例 4：拿铁，昨天的单超时未取 -> 超时事件 + 取宠提醒（含一条注定失败的重试）
    # ======================================================================
    yesterday = (now_hour - timedelta(days=1)).replace(hour=10)
    a4 = operations.create_appointment(
        conn, pet_ids[3], groomer_ids[0], yesterday.strftime("%Y-%m-%d %H:%M"),
        [svc_ids["基础洗浴"], svc_ids["耳道护理"]])
    operations.confirm_reception(conn, a4, actor="小千")
    care4 = conn.execute("SELECT id FROM care_records WHERE appointment_id=?", (a4,)).fetchone()["id"]
    for row in conn.execute("SELECT id FROM care_items WHERE care_record_id=? ORDER BY id", (care4,)):
        operations.complete_care_item(conn, care4, row["id"], actor="阿梅")
    operations.close_care_record(conn, care4, actor="阿梅")
    overdue_end = (datetime.now() - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M")
    conn.execute("UPDATE appointments SET end_time=? WHERE id=?", (overdue_end, a4))
    operations.sweep_overdue(conn)  # 生成超时未取事件并入队提醒
    # 一条注定失败的通知，演示后台指数退避重试。
    operations.enqueue_notification(conn, a4, "重试样例：网关超时的取宠提醒", channel="fail-sms")

    # 未来时段还有一笔预约（复洗观察皮肤）。
    operations.create_appointment(
        conn, pet_ids[0], groomer_ids[1], t_next.strftime("%Y-%m-%d %H:%M"),
        [svc_ids["基础洗浴"], svc_ids["牙齿清洁"]], note="复洗，观察腹部红斑")
