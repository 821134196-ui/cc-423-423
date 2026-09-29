"""演示数据预置：联系人、宠物、服务、美容师、工位排班、预约、护理单、
六类事件、普通/敏感照片、用品消耗、取宠码与通知样例。

所有预置照片生成为本地 SVG 文件（uploads/seed_*.svg），无需联网下载。
执行：python seed.py            # 重建库并预置
      python seed.py --keep     # 仅在空库时预置
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import services
from db import DB_PATH, UPLOAD_DIR, connect, init_db

log = logging.getLogger("grooming.seed")

TODAY = datetime.now().date()
D0 = TODAY.strftime("%Y-%m-%d")
DY = (TODAY - timedelta(days=1)).strftime("%Y-%m-%d")
DT = (TODAY + timedelta(days=1)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 本地 SVG 照片生成
# ---------------------------------------------------------------------------

def _svg(filename: str, bg: str, emoji: str, label: str, sub: str = "",
         ribbon: str = "") -> Path:
    ribbon_svg = ""
    if ribbon:
        ribbon_svg = (
            '<rect x="0" y="0" width="640" height="56" fill="#C75146"/>'
            f'<text x="320" y="37" font-size="26" fill="#fff" '
            'text-anchor="middle" font-family="sans-serif" '
            f'font-weight="bold">{ribbon}</text>'
        )
    content = f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480" viewBox="0 0 640 480">
  <rect width="640" height="480" fill="{bg}"/>
  <circle cx="320" cy="210" r="120" fill="#ffffff" opacity="0.55"/>
  {ribbon_svg}
  <text x="320" y="262" font-size="150" text-anchor="middle">{emoji}</text>
  <text x="320" y="392" font-size="40" fill="#22384f" text-anchor="middle"
        font-family="sans-serif" font-weight="bold">{label}</text>
  <text x="320" y="432" font-size="24" fill="#22384f" text-anchor="middle"
        font-family="sans-serif" opacity="0.75">{sub}</text>
</svg>"""
    path = UPLOAD_DIR / filename
    path.write_text(content, encoding="utf-8")
    return path


def _pet_photo(filename, bg, emoji, name, breed):
    return _svg(filename, bg, emoji, name, breed)


def reset_storage() -> None:
    """删除旧库与预置照片（保留用户后来上传的非 seed_ 文件）。"""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for f in UPLOAD_DIR.glob("seed_*"):
        f.unlink()


# ---------------------------------------------------------------------------
# 预置主流程
# ---------------------------------------------------------------------------

def seed() -> dict:
    reset_storage()
    init_db()
    conn = connect()
    conn.close()

    summary: dict = {"pets": 0, "appointments": 0, "orders": 0, "events": 0,
                     "photos": 0, "notifications": 0}

    # ---- 联系人与宠物（演示数据）----------------------------------------
    contacts = [
        ("林晓雨", "13800010001", "春熙路 88 号", "偏好香波无香型"),
        ("陈默", "13800010002", "锦江畔 2 栋", "猫容易应激"),
        ("苏婉清", "13800010003", "桐梓林 17 号", "金毛每月一次精致洗护"),
        ("高远山", "13800010004", "宽窄巷子旁", "周末取宠，可接受短信提醒"),
    ]
    conn = connect()
    contact_ids = []
    for name, phone, addr, note in contacts:
        cur = conn.execute("INSERT INTO contacts (name,phone,address,note) VALUES (?,?,?,?)",
                           (name, phone, addr, note))
        contact_ids.append(cur.lastrowid)
    conn.commit()

    photos = {
        "doudou": _pet_photo("seed_pet_doudou.svg", "#D9EFE6", "🐩", "豆豆", "比熊犬"),
        "naitang": _pet_photo("seed_pet_naitang.svg", "#FDE7D7", "🐱", "奶糖", "布偶猫"),
        "buding": _pet_photo("seed_pet_buding.svg", "#DDE8F2", "🦮", "布丁", "金毛寻回犬"),
        "tuanzi": _pet_photo("seed_pet_tuanzi.svg", "#E7F0DA", "🐕", "团子", "柯基"),
        "migao": _pet_photo("seed_pet_migao.svg", "#F6E2DD", "🐩", "米糕", "贵宾"),
        "xueqiu": _pet_photo("seed_pet_xueqiu.svg", "#E3EAF3", "🐕‍🦺", "雪球", "萨摩耶"),
    }
    pets = [
        # contact_id, name, species, breed, size, gender, year, weight, temper, taboos, skin, coat, tags, photo
        (contact_ids[0], "豆豆", "犬", "比熊", "小型", "公", 2021, 5.2,
         "活泼,亲人", "禁用香精护毛素,怕吹风筒大声", "眼周轻微泪痕", "卷毛，需每日梳理",
         "泪痕护理,白色卷毛", photos["doudou"].name),
        (contact_ids[1], "奶糖", "猫", "布偶", "中型", "母", 2022, 4.1,
         "胆小,怕生", "禁止烘干箱,禁止强行保定", "腹部近期有红疹", "中长毛，易打结",
         "易应激,长毛猫,皮肤敏感", photos["naitang"].name),
        (contact_ids[2], "布丁", "犬", "金毛寻回犬", "大型", "公", 2019, 31.0,
         "温顺,贪吃", "零食奖励需低糖", "肘部皮肤偏干", "长毛双层被毛，换季掉毛",
         "大型犬,换季掉毛,老客", photos["buding"].name),
        (contact_ids[3], "团子", "犬", "柯基", "中型", "母", 2020, 12.4,
         "倔强,护食", "清理肛门腺需两人配合", "无异常", "短毛双层，臀部毛厚",
         "掉毛季,短毛", photos["tuanzi"].name),
        (contact_ids[0], "米糕", "犬", "贵宾", "小型", "母", 2023, 3.6,
         "黏人,敏感", "禁用含燕麦成分用品", "耳道偏油", "卷毛，做造型",
         "造型常客,耳道护理", photos["migao"].name),
        (contact_ids[2], "雪球", "犬", "萨摩耶", "大型", "母", 2018, 24.8,
         "热情,精力旺", "剪指甲抗拒", "无异常", "白色长毛，嘴角易黄",
         "白色被毛,大型犬", photos["xueqiu"].name),
    ]
    pet_ids = {}
    names = ["doudou", "naitang", "buding", "tuanzi", "migao", "xueqiu"]
    for i, p in enumerate(pets):
        cur = conn.execute(
            """INSERT INTO pets (contact_id,name,species,breed,size,gender,birth_year,weight,
                                 temperament,taboos,skin_note,coat_note,tags,photo)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", p)
        pid = cur.lastrowid
        pet_ids[names[i]] = pid
        # 档案照片同时登记进照片表（普通照片）
        conn.execute(
            """INSERT INTO photos (filename,pet_id,kind,caption,uploaded_by)
               VALUES (?,?,'normal','档案照片','前台')""", (p[13], pid))
    conn.commit()
    summary["pets"] = len(pets)

    # ---- 服务项目 --------------------------------------------------------
    service_rows = [
        ("基础洗澡", "清洁", 30, 68, "温水清洗 + 吹毛"),
        ("精致洗护", "清洁", 90, 168, "去底绒 + 两次香波 + 护毛"),
        ("快速洗护", "清洁", 45, 88, "短毛犬快洗快吹"),
        ("局部清洁", "清洁", 20, 35, "脚毛 / 嘴周 / 私处"),
        ("基础造型修剪", "造型", 60, 128, "按品种标准造型"),
        ("指甲修剪", "护理", 20, 25, "含打磨"),
        ("耳道护理", "护理", 15, 30, "清洁耳道 + 拔耳毛"),
        ("肛门腺护理", "护理", 10, 20, "清理肛门腺"),
        ("洁牙护理", "护理", 30, 98, "无需麻醉的牙齿清洁"),
        ("毛发护理SPA", "护理", 40, 120, "护毛素焗油 + 按摩"),
    ]
    svc_ids = {}
    for name, cat, dur, price, desc in service_rows:
        cur = conn.execute(
            "INSERT INTO services (name,category,duration_min,price,description) VALUES (?,?,?,?,?)",
            (name, cat, dur, price, desc))
        svc_ids[name] = cur.lastrowid
    conn.commit()

    # ---- 用品 ------------------------------------------------------------
    supply_rows = [
        ("温和香波（无香）", "瓶", 12, "敏感肌适用"),
        ("燕麦护毛素", "瓶", 8, "含燕麦成分，过敏宠物禁用"),
        ("耳道清洁液", "瓶", 15, ""),
        ("指甲止血粉", "盒", 6, "剪伤应急"),
        ("一次性吸水毛巾", "条", 80, ""),
        ("保湿皮肤喷雾", "瓶", 9, "肘部干燥适用"),
    ]
    supply_ids = {}
    for name, unit, stock, note in supply_rows:
        cur = conn.execute("INSERT INTO supplies (name,unit,stock,note) VALUES (?,?,?,?)",
                           (name, unit, stock, note))
        supply_ids[name] = cur.lastrowid
    conn.commit()

    # ---- 美容师（含异常照片授权）----------------------------------------
    staff_rows = [
        # name, title, phone, can_view_sensitive, color
        ("周海", "店长 / 高级美容师", "13910000001", 1, "#2E4A62"),  # 授权
        ("夏栀", "高级美容师", "13910000002", 1, "#7FB7A6"),          # 授权
        ("阿淼", "助理美容师", "13910000003", 0, "#F3B39A"),          # 未授权
    ]
    staff_ids = []
    for r in staff_rows:
        cur = conn.execute(
            "INSERT INTO staff (name,title,phone,can_view_sensitive,color) VALUES (?,?,?,?,?)",
            r)
        staff_ids.append(cur.lastrowid)
    haihai, xiazhi, amiao = staff_ids

    # ---- 工位 ------------------------------------------------------------
    station_rows = [("A 美容工位", 1), ("B 美容工位", 1), ("C 美容工位", 1),
                    ("烘干位", 2)]
    station_ids = []
    for name, cap in station_rows:
        cur = conn.execute("INSERT INTO stations (name,capacity) VALUES (?,?)", (name, cap))
        station_ids.append(cur.lastrowid)
    stA, stB, stC, stDry = station_ids

    # ---- 排班：昨天起 8 天，按工位容量锁定 --------------------------------
    for d in range(-1, 7):
        day = (TODAY + timedelta(days=d)).strftime("%Y-%m-%d")
        for staff_id, station_id, start, end in [
            (haihai, stA, "09:00", "18:00"),
            (xiazhi, stB, "09:00", "18:00"),
            (amiao, stC, "10:00", "17:00"),
            (xiazhi, stDry, "09:00", "12:00"),
            (amiao, stDry, "13:00", "18:00"),
        ]:
            conn.execute(
                """INSERT OR IGNORE INTO shifts (staff_id,station_id,work_date,start_time,end_time)
                   VALUES (?,?,?,?,?)""", (staff_id, station_id, day, start, end))
    conn.commit()
    conn.close()

    # ---- 预约 + 护理流程样例（走业务接口，保证状态一致）------------------
    def sid(*names):
        return [svc_ids[n] for n in names]

    # 1) 今天 09:00 豆豆 · 已预约（未接待）
    a1 = services.create_appointment(
        pet_ids["doudou"], haihai, stA, D0, "09:00",
        sid("基础洗澡", "耳道护理"), note="老客人，眼周泪痕轻柔处理")

    # 2) 今天 10:30 奶糖 · 已接待，护理单已生成（待开始）
    a2 = services.create_appointment(
        pet_ids["naitang"], xiazhi, stB, D0, "10:30",
        sid("基础洗澡", "毛发护理SPA"), note="易应激，禁止烘干箱")
    o2 = services.confirm_reception(a2, by="前台-小周",
                                    skin_note="腹部有约 2cm 红疹，已拍照留档",
                                    coat_note="腋下轻度打结")
    services.add_event("skin", "腹部红疹",
                       "接待检查发现腹部有约 2cm 红疹，触诊宠物无明显疼痛反应，已告知主人",
                       by="夏栀", appointment_id=a2, order_id=o2)
    skin_photo = services.save_photo(
        _svg("seed_skin_naitang.svg", "#F8E1DD", "🔴", "腹部红疹（特写）",
             "奶糖 · 接待检查", ribbon="异常照片 · 仅限授权").read_bytes(),
        "seed_skin_naitang.svg", kind="sensitive", pet_id=pet_ids["naitang"],
        appointment_id=a2, caption="腹部红疹特写，护理前留档", uploaded_by="夏栀")
    conn = connect()
    conn.execute("UPDATE photos SET event_id=(SELECT id FROM events WHERE order_id=? ORDER BY id LIMIT 1) WHERE id=?",
                 (o2, skin_photo["id"]))
    conn.commit()
    conn.close()

    # 3) 今天 13:00 布丁 · 护理中：部分完成，含抗拒/剪伤/改项/过敏事件
    a3 = services.create_appointment(
        pet_ids["buding"], haihai, stA, D0, "13:00",
        sid("精致洗护", "指甲修剪", "肛门腺护理"), note="大型犬，安排 A 工位")
    o3 = services.confirm_reception(a3, by="前台-小周", skin_note="肘部偏干，无外伤")
    o3d = services.order_detail(o3)
    items3 = {i["name"]: i["id"] for i in o3d["items"]}
    services.set_item_status(items3["精致洗护"], "done", by="周海",
                             remark="两次香波，肘部加保湿喷雾")
    services.set_item_status(items3["指甲修剪"], "done", by="周海",
                             remark="右后脚指甲轻微剪伤，已止血粉处理")
    # 宠物抗拒事件
    services.add_event("resistance", "吹毛时抗拒",
                       "吹腹毛时布丁多次躲闪低吼，暂停 5 分钟安抚后继续，改用低风速",
                       by="周海", appointment_id=a3, order_id=o3)
    # 剪伤事件 + 敏感照片
    services.add_event("cut", "右后脚指甲剪伤",
                       "修剪右后脚指甲时剪到血线约 1mm，已使用止血粉，观察 10 分钟无持续出血，已电话告知主人",
                       by="周海", appointment_id=a3, order_id=o3)
    cut_photo = services.save_photo(
        _svg("seed_cut_buding.svg", "#F6D9D2", "🩹", "右后脚指甲剪伤",
             "布丁 · 止血粉处理后", ribbon="异常照片 · 仅限授权").read_bytes(),
        "seed_cut_buding.svg", kind="sensitive", pet_id=pet_ids["buding"],
        appointment_id=a3, caption="剪伤处理后留档", uploaded_by="周海")
    # 主人临时改项：新增洁牙护理（尚未完成）
    services.owner_change_services(
        o3, sid("精致洗护", "指甲修剪", "肛门腺护理", "洁牙护理"),
        by="前台-小周", reason="主人到店等候时临时追加洁牙")
    # 用品消耗：一条正常，一条导致过敏
    services.record_supply_usage(o3, supply_ids["一次性吸水毛巾"], 2, by="周海")
    services.record_supply_usage(o3, supply_ids["燕麦护毛素"], 1, by="周海",
                                 caused_allergy=True,
                                 allergy_detail="腹部出现局部红斑、频繁舔舐，已清水冲净并停药，建议复诊")
    # 普通照片：护理后
    services.save_photo(
        _svg("seed_after_buding.svg", "#DDE8F2", "✨", "洗护后状态",
             "布丁 · 毛发蓬松").read_bytes(),
        "seed_after_buding.svg", kind="normal", pet_id=pet_ids["buding"],
        appointment_id=a3, caption="洗护完成后毛发状态", uploaded_by="周海")
    conn = connect()
    conn.execute("UPDATE photos SET event_id=(SELECT id FROM events WHERE order_id=? AND type='cut') WHERE id=?",
                 (o3, cut_photo["id"]))
    conn.commit()
    conn.close()

    # 4) 今天 16:00 团子 · 已结单待取宠（取宠码已生成，演示取宠核验）
    a4 = services.create_appointment(
        pet_ids["tuanzi"], xiazhi, stB, D0, "16:00",
        sid("快速洗护", "指甲修剪"), note="护食，修剪时注意")
    o4 = services.confirm_reception(a4, by="前台-小周")
    o4d = services.order_detail(o4)
    for it in o4d["items"]:
        services.set_item_status(it["id"], "done", by="夏栀", remark="顺利完成")
    services.save_photo(
        _svg("seed_after_tuanzi.svg", "#E7F0DA", "🛁", "洗护完成",
             "团子 · 等待主人").read_bytes(),
        "seed_after_tuanzi.svg", kind="normal", pet_id=pet_ids["tuanzi"],
        appointment_id=a4, caption="结单照片", uploaded_by="夏栀")
    close4 = services.close_order(o4, by="夏栀", summary="短毛快洗，臀部底绒已梳理，状态良好")
    # 护理记录已确认（不可删除样例）
    services.confirm_record(o4, by="店长-周海")

    # 5) 今天 15:30 米糕 · 已预约（局部清洁，烘干位下午由阿淼值守）
    a5 = services.create_appointment(
        pet_ids["migao"], amiao, stDry, D0, "15:30",
        sid("局部清洁", "耳道护理"), note="禁用含燕麦用品")

    # 6) 昨天 14:00 米糕 · 已结单未取 → 超时未取事件样例
    a6 = services.create_appointment(
        pet_ids["migao"], haihai, stA, DY, "14:00",
        sid("基础造型修剪", "耳道护理"))
    o6 = services.confirm_reception(a6, by="前台-小周")
    for it in services.order_detail(o6)["items"]:
        services.set_item_status(it["id"], "done", by="周海")
    services.close_order(o6, by="周海", summary="泰迪造型，耳道已清洁")
    services.add_event("overdue", "超时未取",
                       f"已超过约定取宠时间，多次拨打主人电话未接通，宠物留院过夜",
                       by="系统", pet_id=pet_ids["migao"], appointment_id=a6, order_id=o6)

    # 7) 昨天 10:00 雪球 · 已取宠（完整闭环样例）
    a7 = services.create_appointment(
        pet_ids["xueqiu"], xiazhi, stB, DY, "10:00",
        sid("精致洗护", "指甲修剪"))
    o7 = services.confirm_reception(a7, by="前台-小周")
    for it in services.order_detail(o7)["items"]:
        services.set_item_status(it["id"], "done", by="夏栀")
    close7 = services.close_order(o7, by="夏栀", summary="白色被毛去黄护理，指甲已打磨")
    services.verify_pickup(a7, close7["pickup_code"], verifier="前台-小周")

    # 8) 明天 11:00 布丁 · 已预约（未来提醒）
    a8 = services.create_appointment(
        pet_ids["buding"], haihai, stA, DT, "11:00",
        sid("精致洗护", "毛发护理SPA"), note="老客月度精致洗护",
        reminder_minutes=60)

    summary["appointments"] = 8
    summary["orders"] = 5

    # ---- 通知重试演示数据 -----------------------------------------------
    conn = connect()
    # 前两次发送失败、第三次成功：用于观察进程内重试退避
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO notifications (appointment_id,pet_id,kind,channel,payload,status,
                                      attempts,max_attempts,next_run_at,last_error)
           VALUES (?,?, 'reminder','短信',?, 'pending', 0, 3, ?, '')""",
        (a2, pet_ids["naitang"],
         "fail:2:重试演示：奶糖护理提醒（前两次通道失败，第三次自动成功）", now))
    # 已彻底失败样例（达到最大重试）
    conn.execute(
        """INSERT INTO notifications (appointment_id,pet_id,kind,channel,payload,status,
                                      attempts,max_attempts,next_run_at,last_error,sent_at)
           VALUES (?,?, 'reminder','短信',?, 'failed', 3, 3, ?, '号码为空，三次重试均失败', NULL)""",
        (a6, pet_ids["migao"], "米糕超时未取提醒（演示失败记录）", now))
    # 已发送成功样例
    conn.execute(
        """INSERT INTO notifications (appointment_id,pet_id,kind,channel,payload,status,
                                      attempts,max_attempts,next_run_at,sent_at)
           VALUES (?,?, 'pickup','站内',?, 'sent', 1, 3, ?, ?)""",
        (a7, pet_ids["xueqiu"], "雪球已由主人凭取宠码接走", now, now))
    conn.commit()
    summary["notifications"] = conn.execute("SELECT COUNT(*) c FROM notifications").fetchone()["c"]
    summary["events"] = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    summary["photos"] = conn.execute("SELECT COUNT(*) c FROM photos").fetchone()["c"]
    conn.close()
    return summary


def ensure_seeded() -> bool:
    """空库时自动预置；已有数据则跳过。返回是否执行了预置。"""
    init_db()
    conn = connect()
    try:
        count = conn.execute("SELECT COUNT(*) c FROM pets").fetchone()["c"]
        if count > 0:
            return False
    finally:
        conn.close()
    seed()
    return True


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if "--keep" in argv:
        did = ensure_seeded()
        print("已存在演示数据，跳过" if not did else "空库，已完成预置")
    else:
        summary = seed()
        print("演示数据预置完成：")
        for k, v in summary.items():
            print(f"  - {k}: {v}")


if __name__ == "__main__":
    main(sys.argv[1:])
