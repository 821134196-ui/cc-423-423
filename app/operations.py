"""核心业务逻辑：预约锁定、护理单状态机、事件、照片权限、用品消耗、取宠核验。

所有写操作在 :func:`_transaction` 中以 ``BEGIN IMMEDIATE`` 开启，
SQLite 写锁在整段事务内持有，因此并发抢同一时段时只有一个事务能成功。
"""
import threading
from datetime import datetime, timedelta

from . import config
from .db import connect

EVENT_KINDS = {
    "resistance": "宠物抗拒",
    "skin": "皮肤异常",
    "cut": "剪伤",
    "owner_change": "主人临时改项",
    "overdue_pickup": "超时未取",
    "supply_allergy": "用品过敏",
}

# 护理单/预约状态机。
APPT_STATUS = {
    "booked": "已预约",
    "reception_confirmed": "接待确认",
    "in_care": "护理中",
    "done": "待取宠",
    "picked_up": "已取宠",
    "canceled": "已取消",
    "no_show": "未到店",
}

_TLS = threading.local()


class AppError(Exception):
    """业务规则错误，HTTP 层映射为 400/403/409。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def now():
    return datetime.now()


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def parse_slot(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


def transaction(conn):
    """BEGIN IMMEDIATE 上下文：立即获取 SQLite 写锁（RESERVED）。"""
    conn.execute("BEGIN IMMEDIATE")
    return conn


def commit(conn):
    conn.execute("COMMIT")


def rollback(conn):
    conn.execute("ROLLBACK")


def _slot_capacity(conn, slot_time):
    row = conn.execute(
        "SELECT capacity FROM station_slots WHERE slot_time=?", (slot_time,)
    ).fetchone()
    return row["capacity"] if row else config.DEFAULT_STATION_CAPACITY


def set_slot_capacity(conn, slot_time, capacity):
    conn.execute(
        """INSERT INTO station_slots(slot_time, capacity) VALUES(?,?)
           ON CONFLICT(slot_time) DO UPDATE SET capacity=excluded.capacity""",
        (slot_time, capacity),
    )


def _tags(text):
    return [t.strip() for t in (text or "").split(",") if t.strip()]


# ---------------------------------------------------------------------------
# 档案 / 基础数据查询
# ---------------------------------------------------------------------------

def list_pets(conn):
    rows = conn.execute(
        """SELECT p.*, c.name AS contact_name, c.phone AS contact_phone, c.note AS contact_note
           FROM pets p JOIN contacts c ON c.id = p.contact_id
           ORDER BY p.id"""
    ).fetchall()
    return [_pet_dict(conn, r) for r in rows]


def get_pet(conn, pet_id):
    r = conn.execute(
        """SELECT p.*, c.name AS contact_name, c.phone AS contact_phone, c.note AS contact_note
           FROM pets p JOIN contacts c ON c.id = p.contact_id WHERE p.id=?""",
        (pet_id,),
    ).fetchone()
    if not r:
        raise AppError("宠物不存在", 404)
    return _pet_dict(conn, r)


def _pet_dict(conn, r):
    d = dict(r)
    d["tags"] = _tags(r["tab_tags"])
    d["photos"] = [dict(x) for x in conn.execute(
        "SELECT id, file_name, caption, uploaded_at FROM photos WHERE pet_id=? ORDER BY id",
        (r["id"],)).fetchall()]
    d["recent_events"] = [dict(x) for x in conn.execute(
        """SELECT id, kind, detail, created_at FROM events
           WHERE pet_id=? ORDER BY id DESC LIMIT 8""", (r["id"],)).fetchall()]
    return d


def list_services(conn, active_only=True):
    sql = "SELECT * FROM services"
    if active_only:
        sql += " WHERE active=1"
    return [dict(r) for r in conn.execute(sql + " ORDER BY id").fetchall()]


def list_groomers(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM groomers WHERE active=1 ORDER BY id").fetchall()]


# ---------------------------------------------------------------------------
# 预约日历 / 工位排程
# ---------------------------------------------------------------------------

def appointments_between(conn, day_start, day_end):
    """取某一天 00:00~次日 00:00 的预约（含取消，用于日历展示）。"""
    rows = conn.execute(
        """SELECT a.*, p.name AS pet_name, p.species, g.name AS groomer_name,
                  c.name AS contact_name, c.phone AS contact_phone
           FROM appointments a
           JOIN pets p ON p.id = a.pet_id
           JOIN groomers g ON g.id = a.groomer_id
           JOIN contacts c ON c.id = p.contact_id
           WHERE a.start_time >= ? AND a.start_time < ?
           ORDER BY a.start_time, a.id""",
        (day_start, day_end),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["services"] = [dict(x) for x in conn.execute(
            """SELECT s.id, s.name, asv.price_snapshot AS price
               FROM appointment_services asv JOIN services s ON s.id = asv.service_id
               WHERE asv.appointment_id=? ORDER BY s.id""", (r["id"],)).fetchall()]
        d["status_label"] = APPT_STATUS.get(r["status"], r["status"])
        out.append(d)
    return out


def station_schedule(conn, date_text):
    """工位时间轴：每个开店时段一行，列出容量/已锁定数与该时段预约卡。"""
    slots = conn.execute(
        "SELECT slot_time, capacity FROM station_slots WHERE slot_time LIKE ? ORDER BY slot_time",
        (f"{date_text}%",),
    ).fetchall()
    cap_map = {r["slot_time"]: r["capacity"] for r in slots}

    rows = conn.execute(
        """SELECT a.*, p.name AS pet_name, p.size, g.name AS groomer_name
           FROM appointments a
           JOIN pets p ON p.id = a.pet_id
           JOIN groomers g ON g.id = a.groomer_id
           WHERE a.start_time LIKE ? AND a.status NOT IN ('canceled','no_show')
           ORDER BY a.start_time, g.id""",
        (f"{date_text}%",),
    ).fetchall()
    axis = {}
    for r in rows:
        axis.setdefault(r["start_time"], []).append(dict(r))

    timeline = []
    for slot_time in sorted(set(list(cap_map) + list(axis))):
        cards = axis.get(slot_time, [])
        capacity = cap_map.get(slot_time, config.DEFAULT_STATION_CAPACITY)
        # 同一美容师同一时段只能做一单。
        groomer_ids = [c["groomer_id"] for c in cards]
        timeline.append({
            "slot_time": slot_time,
            "capacity": capacity,
            "locked": len(cards),
            "full": len(cards) >= capacity,
            "groomer_conflict": len(groomer_ids) != len(set(groomer_ids)),
            "cards": cards,
        })
    return timeline


# ---------------------------------------------------------------------------
# 创建预约：保存品种/体型/禁忌快照 + 服务组合与预计时长，按工位容量锁定
# ---------------------------------------------------------------------------

def create_appointment(conn, pet_id, groomer_id, start_time, service_ids,
                       note=""):
    if isinstance(start_time, datetime):
        start_time = fmt(start_time)
    else:
        parse_slot(start_time)  # 校验格式
    service_ids = list(dict.fromkeys(int(s) for s in service_ids))
    if not service_ids:
        raise AppError("请至少选择一个服务项目")

    transaction(conn)
    try:
        pet = conn.execute("SELECT * FROM pets WHERE id=?", (pet_id,)).fetchone()
        if not pet:
            raise AppError("宠物不存在", 404)
        groomer = conn.execute(
            "SELECT id FROM groomers WHERE id=? AND active=1", (groomer_id,)).fetchone()
        if not groomer:
            raise AppError("美容师不存在或已停用", 404)

        # 1) 同一宠物同一时段不得重复预约（DB 还有唯一索引兜底）。
        dup = conn.execute(
            """SELECT id FROM appointments
               WHERE pet_id=? AND start_time=? AND status NOT IN ('canceled','no_show')""",
            (pet_id, start_time),
        ).fetchone()
        if dup:
            raise AppError("同一宠物该时段已有预约，不能重复预约", 409)

        # 2) 美容师同一时段只能服务一只。
        g_busy = conn.execute(
            """SELECT COUNT(*) AS n FROM appointments
               WHERE groomer_id=? AND start_time=?
                 AND status NOT IN ('canceled','no_show')""",
            (groomer_id, start_time),
        ).fetchone()["n"]
        if g_busy:
            raise AppError("该美容师此时段已有排班，请换一个时段", 409)

        # 3) 工位容量锁定（跨美容师合计）。
        capacity = _slot_capacity(conn, start_time)
        used = conn.execute(
            """SELECT COUNT(*) AS n FROM appointments
               WHERE start_time=? AND status NOT IN ('canceled','no_show')""",
            (start_time,),
        ).fetchone()["n"]
        if used >= capacity:
            raise AppError(f"该时段工位已满（容量 {capacity}），无法锁定工位", 409)

        # 服务组合与预计时长。
        total_minutes, price = 0, 0.0
        found = []
        for sid in service_ids:
            s = conn.execute("SELECT * FROM services WHERE id=? AND active=1", (sid,)).fetchone()
            if not s:
                raise AppError(f"服务项目 {sid} 不存在或已停用")
            total_minutes += s["duration_minutes"]
            price += s["price"]
            found.append(s)

        start_dt = parse_slot(start_time)
        end_time = fmt(start_dt + timedelta(minutes=total_minutes))

        cur = conn.execute(
            """INSERT INTO appointments
               (pet_id, groomer_id, start_time, end_time, breed_snapshot,
                size_snapshot, contraindication_snapshot, estimated_minutes,
                status, note)
               VALUES (?,?,?,?,?,?,?,?, 'booked', ?)""",
            (pet_id, groomer_id, start_time, end_time,
             pet["breed"], pet["size"], pet["contraindications"],
             total_minutes, note),
        )
        appt_id = cur.lastrowid
        for s in found:
            conn.execute(
                """INSERT INTO appointment_services(appointment_id, service_id, price_snapshot)
                   VALUES (?,?,?)""",
                (appt_id, s["id"], s["price"]),
            )
        commit(conn)
        return appt_id
    except Exception:
        rollback(conn)
        raise


def cancel_appointment(conn, appointment_id, reason=""):
    transaction(conn)
    try:
        r = conn.execute("SELECT status FROM appointments WHERE id=?",
                         (appointment_id,)).fetchone()
        if not r:
            raise AppError("预约不存在", 404)
        if r["status"] in ("in_care", "done", "picked_up"):
            raise AppError("护理已开始，不能取消预约")
        conn.execute("UPDATE appointments SET status='canceled', "
                     "note=note || ? WHERE id=?",
                     (f"\n[取消] {reason}", appointment_id))
        commit(conn)
    except Exception:
        rollback(conn)
        raise


# ---------------------------------------------------------------------------
# 主人临时改项（服务变更）
# ---------------------------------------------------------------------------

def change_services(conn, appointment_id, service_ids, actor="前台", reason=""):
    """变更服务组合。

    接待前：直接改预约明细并重算预计时长；
    接待后：同步护理单条目——新增项目生成待完成条目，已完成的项目不允许删除。
    每次变更都登记 ``owner_change`` 事件。
    """
    service_ids = list(dict.fromkeys(int(s) for s in service_ids))
    if not service_ids:
        raise AppError("服务组合不能为空")

    transaction(conn)
    try:
        appt = conn.execute("SELECT * FROM appointments WHERE id=?",
                            (appointment_id,)).fetchone()
        if not appt:
            raise AppError("预约不存在", 404)
        if appt["status"] in ("canceled", "picked_up"):
            raise AppError("该预约已结束，不能变更项目")

        wanted = {}
        total_minutes = 0
        for sid in service_ids:
            s = conn.execute("SELECT * FROM services WHERE id=? AND active=1", (sid,)).fetchone()
            if not s:
                raise AppError(f"服务项目 {sid} 不存在或已停用")
            wanted[sid] = s
            total_minutes += s["duration_minutes"]

        old_rows = conn.execute(
            """SELECT asv.service_id, s.name FROM appointment_services asv
               JOIN services s ON s.id=asv.service_id
               WHERE asv.appointment_id=?""", (appointment_id,)).fetchall()
        old_ids = {r["service_id"] for r in old_rows}
        new_ids = set(wanted)
        added = [wanted[i]["name"] for i in new_ids - old_ids]
        removed = [r["name"] for r in old_rows if r["service_id"] in (old_ids - new_ids)]

        care = conn.execute(
            "SELECT * FROM care_records WHERE appointment_id=?", (appointment_id,)).fetchone()
        if care:
            if care["confirmed_at"]:
                raise AppError("护理记录已确认，项目不可变更")
            # 已完成的条目不能被删掉。
            placeholders = ",".join("?" * len(wanted))
            done_removed = conn.execute(
                f"""SELECT name_snapshot FROM care_items
                    WHERE care_record_id=? AND status='done'
                      AND service_id NOT IN ({placeholders})""",
                [care["id"], *wanted.keys()],
            ).fetchall()
            if done_removed:
                names = "、".join(r["name_snapshot"] for r in done_removed)
                raise AppError(f"以下项目已完成记录，不能删除：{names}")

        # 重写预约明细。
        conn.execute("DELETE FROM appointment_services WHERE appointment_id=?", (appointment_id,))
        for s in wanted.values():
            conn.execute(
                """INSERT INTO appointment_services(appointment_id, service_id, price_snapshot)
                   VALUES (?,?,?)""", (appointment_id, s["id"], s["price"]))
        end_time = fmt(parse_slot(appt["start_time"]) + timedelta(minutes=total_minutes))
        conn.execute(
            "UPDATE appointments SET estimated_minutes=?, end_time=? WHERE id=?",
            (total_minutes, end_time, appointment_id))

        if care:
            # 同步护理条目：删除仍处于 pending 的移除项，追加新增项。
            conn.execute(
                """DELETE FROM care_items WHERE care_record_id=? AND status='pending'
                   AND service_id NOT IN (%s)""" % ",".join("?" * len(wanted)),
                [care["id"], *wanted.keys()])
            exist = {r["service_id"] for r in conn.execute(
                "SELECT service_id FROM care_items WHERE care_record_id=?", (care["id"],))}
            for sid, s in wanted.items():
                if sid not in exist:
                    conn.execute(
                        """INSERT INTO care_items
                           (care_record_id, service_id, name_snapshot, price_snapshot, status)
                           VALUES (?,?,?,?, 'pending')""",
                        (care["id"], sid, s["name"], s["price"]))

        detail = f"新增：{('、'.join(added) or '无')}；移除：{('、'.join(removed) or '无')}"
        if reason:
            detail += f"；原因：{reason}"
        conn.execute(
            """INSERT INTO events(appointment_id, care_record_id, pet_id, kind, detail, created_by)
               VALUES (?,?,?, 'owner_change', ?, ?)""",
            (appointment_id, care["id"] if care else None, appt["pet_id"], detail, actor))
        commit(conn)
    except Exception:
        rollback(conn)
        raise


# ---------------------------------------------------------------------------
# 接待确认 -> 护理单；项目逐项完成；结单约束
# ---------------------------------------------------------------------------

def confirm_reception(conn, appointment_id, actor="前台"):
    transaction(conn)
    try:
        appt = conn.execute("SELECT * FROM appointments WHERE id=?",
                            (appointment_id,)).fetchone()
        if not appt:
            raise AppError("预约不存在", 404)
        if appt["status"] != "booked":
            raise AppError(f"当前状态（{APPT_STATUS.get(appt['status'])}）不能接待确认", 409)
        exists = conn.execute(
            "SELECT id FROM care_records WHERE appointment_id=?", (appointment_id,)).fetchone()
        if exists:
            raise AppError("护理单已生成，不能重复接待确认", 409)

        cur = conn.execute(
            "INSERT INTO care_records(appointment_id, status) VALUES (?, 'open')",
            (appointment_id,))
        care_id = cur.lastrowid
        rows = conn.execute(
            """SELECT asv.service_id, s.name, asv.price_snapshot
               FROM appointment_services asv JOIN services s ON s.id=asv.service_id
               WHERE asv.appointment_id=? ORDER BY s.id""", (appointment_id,)).fetchall()
        for r in rows:
            conn.execute(
                """INSERT INTO care_items(care_record_id, service_id, name_snapshot,
                   price_snapshot, status) VALUES (?,?,?,?, 'pending')""",
                (care_id, r["service_id"], r["name"], r["price_snapshot"]))
        conn.execute("UPDATE appointments SET status='reception_confirmed' WHERE id=?",
                     (appointment_id,))
        commit(conn)
        return care_id
    except Exception:
        rollback(conn)
        raise


def get_care_record(conn, appointment_id):
    care = conn.execute(
        "SELECT * FROM care_records WHERE appointment_id=?", (appointment_id,)).fetchone()
    if not care:
        raise AppError("护理单尚未生成", 404)
    appt = conn.execute(
        """SELECT a.*, p.name AS pet_name, p.breed, p.skin_coat_note,
                  p.contraindications, g.name AS groomer_name
           FROM appointments a JOIN pets p ON p.id=a.pet_id
           JOIN groomers g ON g.id=a.groomer_id WHERE a.id=?""",
        (appointment_id,)).fetchone()
    items = conn.execute(
        "SELECT * FROM care_items WHERE care_record_id=? ORDER BY id", (care["id"],)).fetchall()
    supplies = conn.execute(
        "SELECT * FROM supply_usages WHERE care_record_id=? ORDER BY id", (care["id"],)).fetchall()
    events = conn.execute(
        "SELECT * FROM events WHERE care_record_id=? ORDER BY id", (care["id"],)).fetchall()
    event_list = []
    for ev in events:
        d = dict(ev)
        d["photos"] = [photo_meta(x) for x in conn.execute(
            "SELECT * FROM photos WHERE event_id=? ORDER BY id", (ev["id"],)).fetchall()]
        event_list.append(d)
    return {
        "record": dict(care),
        "appointment": dict(appt),
        "items": [dict(x) for x in items],
        "supplies": [dict(x) for x in supplies],
        "events": event_list,
        "event_labels": EVENT_KINDS,
    }


def complete_care_item(conn, care_record_id, item_id, actor="美容师"):
    """完成一项记录一项；首次有项目完成时预约进入护理中。"""
    transaction(conn)
    try:
        care = conn.execute("SELECT * FROM care_records WHERE id=?",
                            (care_record_id,)).fetchone()
        if not care:
            raise AppError("护理单不存在", 404)
        if care["confirmed_at"]:
            raise AppError("护理记录已确认，不能再登记项目")
        item = conn.execute(
            "SELECT * FROM care_items WHERE id=? AND care_record_id=?",
            (item_id, care_record_id)).fetchone()
        if not item:
            raise AppError("护理项目不存在", 404)
        if item["status"] == "done":
            raise AppError("该项目已完成")
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE care_items SET status='done', completed_at=?, completed_by=? WHERE id=?",
            (stamp, actor, item_id))
        conn.execute(
            "UPDATE appointments SET status='in_care' WHERE id=(SELECT appointment_id FROM care_records WHERE id=?) AND status='reception_confirmed'",
            (care_record_id,))
        commit(conn)
    except Exception:
        rollback(conn)
        raise


def undo_care_item(conn, care_record_id, item_id, actor="美容师"):
    """误点可撤销，但结单后不允许。"""
    transaction(conn)
    try:
        care = conn.execute("SELECT * FROM care_records WHERE id=?", (care_record_id,)).fetchone()
        if not care:
            raise AppError("护理单不存在", 404)
        if care["confirmed_at"]:
            raise AppError("护理记录已确认，不能修改")
        conn.execute(
            """UPDATE care_items SET status='pending', completed_at=NULL, completed_by=''
               WHERE id=? AND care_record_id=? AND status='done'""",
            (item_id, care_record_id))
        commit(conn)
    except Exception:
        rollback(conn)
        raise


def close_care_record(conn, care_record_id, actor="美容师"):
    """结单：存在未完成项目直接拒绝；已确认的单子不能重复结单。"""
    transaction(conn)
    try:
        care = conn.execute("SELECT * FROM care_records WHERE id=?",
                            (care_record_id,)).fetchone()
        if not care:
            raise AppError("护理单不存在", 404)
        if care["confirmed_at"]:
            raise AppError("护理记录已确认，不能重复结单", 409)
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM care_items WHERE care_record_id=? AND status!='done'",
            (care_record_id,)).fetchone()["n"]
        if pending:
            raise AppError(f"还有 {pending} 个项目未完成，未完成项目不能直接结单", 409)
        total = conn.execute(
            "SELECT COALESCE(SUM(price_snapshot),0) AS t FROM care_items WHERE care_record_id=?",
            (care_record_id,)).fetchone()["t"]
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        code = f"{100000 + (care_id_int(care_record_id) * 7919) % 900000}"
        conn.execute(
            """UPDATE care_records SET status='closed', confirmed_at=?, total_price=?,
               pickup_code=? WHERE id=?""",
            (stamp, total, code, care_record_id))
        appt = conn.execute(
            "SELECT appointment_id FROM care_records WHERE id=?", (care_record_id,)).fetchone()
        conn.execute("UPDATE appointments SET status='done' WHERE id=?",
                     (appt["appointment_id"],))
        enqueue_notification(conn, appt["appointment_id"],
                             f"宠物护理已完成，请凭取宠码 {code} 到店取宠",
                             channel="sms")
        commit(conn)
        return {"confirmed_at": stamp, "total_price": total, "pickup_code": code}
    except Exception:
        rollback(conn)
        raise


def care_id_int(care_record_id):
    return int(care_record_id)


def delete_care_record(conn, care_record_id):
    """护理记录确认后不能删除。"""
    care = conn.execute("SELECT id, confirmed_at, appointment_id FROM care_records WHERE id=?",
                        (care_record_id,)).fetchone()
    if not care:
        raise AppError("护理单不存在", 404)
    if care["confirmed_at"]:
        raise AppError("护理记录已确认，不能删除", 403)
    transaction(conn)
    try:
        conn.execute("DELETE FROM care_records WHERE id=?", (care_record_id,))
        conn.execute("UPDATE appointments SET status='booked' WHERE id=? AND status IN ('reception_confirmed','in_care')",
                     (care["appointment_id"],))
        commit(conn)
    except Exception:
        rollback(conn)
        raise


# ---------------------------------------------------------------------------
# 事件：抗拒/皮肤异常/剪伤/改项/超时/过敏
# ---------------------------------------------------------------------------

def add_event(conn, appointment_id, kind, detail, actor="美容师", pet_id=None):
    if kind not in EVENT_KINDS:
        raise AppError(f"未知事件类型：{kind}")
    transaction(conn)
    try:
        care_id = None
        real_pet_id = pet_id
        if appointment_id:
            appt = conn.execute("SELECT * FROM appointments WHERE id=?",
                                (appointment_id,)).fetchone()
            if not appt:
                raise AppError("预约不存在", 404)
            real_pet_id = appt["pet_id"]
            care = conn.execute(
                "SELECT id FROM care_records WHERE appointment_id=?", (appointment_id,)).fetchone()
            care_id = care["id"] if care else None
        cur = conn.execute(
            """INSERT INTO events(appointment_id, care_record_id, pet_id, kind, detail, created_by)
               VALUES (?,?,?,?,?,?)""",
            (appointment_id or None, care_id, real_pet_id, kind, detail, actor))
        commit(conn)
        return cur.lastrowid
    except Exception:
        rollback(conn)
        raise


def list_events(conn, kind=None, limit=50):
    sql = ("""SELECT e.*, p.name AS pet_name, a.start_time FROM events e
              LEFT JOIN pets p ON p.id=e.pet_id
              LEFT JOIN appointments a ON a.id=e.appointment_id""")
    args = []
    if kind:
        sql += " WHERE e.kind=?"
        args.append(kind)
    sql += " ORDER BY e.id DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["kind_label"] = EVENT_KINDS.get(r["kind"], r["kind"])
        d["photos"] = [photo_meta(x) for x in conn.execute(
            "SELECT * FROM photos WHERE event_id=? ORDER BY id", (r["id"],)).fetchall()]
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# 用品消耗（用品过敏同时生成事件）
# ---------------------------------------------------------------------------

def use_supply(conn, care_record_id, supply_name, quantity=1, unit="份",
               allergy=False, actor="美容师", reaction=""):
    transaction(conn)
    try:
        care = conn.execute(
            "SELECT cr.*, a.pet_id FROM care_records cr JOIN appointments a ON a.id=cr.appointment_id WHERE cr.id=?",
            (care_record_id,)).fetchone()
        if not care:
            raise AppError("护理单不存在", 404)
        if care["confirmed_at"]:
            raise AppError("护理记录已确认，不能再登记用品消耗")
        conn.execute(
            """INSERT INTO supply_usages(care_record_id, supply_name, quantity, unit)
               VALUES (?,?,?,?)""",
            (care_record_id, supply_name, quantity, unit))
        if allergy:
            detail = f"用品「{supply_name}」疑似过敏" + (f"：{reaction}" if reaction else "")
            conn.execute(
                """INSERT INTO events(appointment_id, care_record_id, pet_id, kind, detail, created_by)
                   VALUES (?,?,?, 'supply_allergy', ?, ?)""",
                (care["appointment_id"], care_record_id, care["pet_id"], detail, actor))
        commit(conn)
    except Exception:
        rollback(conn)
        raise


# ---------------------------------------------------------------------------
# 照片与权限
# ---------------------------------------------------------------------------

def photo_meta(row):
    d = dict(row)
    d["restricted"] = row["sensitivity"] == "restricted"
    return d


def list_incident_photos(conn):
    """异常照片列表：元数据始终可列，内容是否可看由 ``get_photo_path`` 决定。"""
    rows = conn.execute(
        """SELECT ph.*, p.name AS pet_name, e.kind AS event_kind
           FROM photos ph LEFT JOIN pets p ON p.id=ph.pet_id
           LEFT JOIN events e ON e.id=ph.event_id
           WHERE ph.kind='incident' ORDER BY ph.id DESC""").fetchall()
    return [photo_meta(r) for r in rows]


def attach_incident_photo(conn, event_id, raw, caption, authorized, actor):
    """异常照片登记：只有授权人员能上传（restricted 内容）。"""
    if not authorized:
        raise AppError("只有授权人员可以登记异常照片", 403)
    event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not event:
        raise AppError("事件不存在", 404)
    from . import photos as photos_mod
    return photos_mod.save_photo(
        raw, kind="incident", sensitivity="restricted", caption=caption,
        uploaded_by=actor, pet_id=event["pet_id"],
        appointment_id=event["appointment_id"], event_id=event["id"], conn=conn)


def get_photo_path(conn, photo_id, authorized):
    """取照片文件路径。异常（restricted）照片只允许授权人员查看。"""
    row = conn.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    if not row:
        raise AppError("照片不存在", 404)
    if row["sensitivity"] == "restricted" and not authorized:
        raise AppError("该异常照片仅限授权人员查看", 403)
    from . import config as cfg
    photo_root = cfg.PHOTO_DIR.resolve()
    target = (photo_root / row["file_name"]).resolve()
    if photo_root not in target.parents:
        raise AppError("非法照片路径", 403)
    if not target.exists():
        raise AppError("照片文件丢失", 410)
    return target, dict(row)


# ---------------------------------------------------------------------------
# 取宠核验
# ---------------------------------------------------------------------------

def verify_pickup(conn, appointment_id, code, actor="前台"):
    """凭取宠码或联系人手机号核验，核验通过后标记取宠。"""
    transaction(conn)
    try:
        appt = conn.execute("SELECT * FROM appointments WHERE id=?",
                            (appointment_id,)).fetchone()
        if not appt:
            raise AppError("预约不存在", 404)
        care = conn.execute(
            "SELECT * FROM care_records WHERE appointment_id=?", (appointment_id,)).fetchone()
        if not care or not care["confirmed_at"]:
            raise AppError("护理尚未结单，不能核验取宠", 409)
        if care["picked_up_at"]:
            raise AppError("该宠物已完成取宠核验，请勿重复操作", 409)

        contact = conn.execute(
            """SELECT c.phone FROM contacts c JOIN pets p ON p.contact_id=c.id
               WHERE p.id=?""", (appt["pet_id"],)).fetchone()
        code = (code or "").strip()
        if code != care["pickup_code"] and code != contact["phone"]:
            raise AppError("取宠码或预留手机号核验失败", 403)

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE care_records SET picked_up_at=?, pickup_verified_by=? WHERE id=?",
            (stamp, actor, care["id"]))
        conn.execute("UPDATE appointments SET status='picked_up' WHERE id=?", (appointment_id,))
        commit(conn)
        return {"picked_up_at": stamp, "verified_by": actor}
    except Exception:
        rollback(conn)
        raise


def pickup_queue(conn):
    """待取宠列表（已结单未取）。"""
    rows = conn.execute(
        """SELECT a.id AS appointment_id, a.start_time, a.end_time, p.name AS pet_name,
                  c.name AS contact_name, c.phone, cr.pickup_code, cr.confirmed_at,
                  cr.picked_up_at, a.status,
                  EXISTS(SELECT 1 FROM events e
                         WHERE e.appointment_id=a.id AND e.kind='overdue_pickup') AS overdue
           FROM appointments a
           JOIN pets p ON p.id=a.pet_id
           JOIN contacts c ON c.id=p.contact_id
           JOIN care_records cr ON cr.appointment_id=a.id
           WHERE a.status IN ('done','picked_up')
           ORDER BY a.end_time""").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 超时未取扫描 + 通知（进程内任务，失败重试）
# ---------------------------------------------------------------------------

def sweep_overdue(conn, at=None):
    """超过约定结束时间指定分钟仍未取 => 生成一次超时未取事件并提醒。"""
    at = at or datetime.now()
    limit = (at - timedelta(minutes=config.OVERDUE_PICKUP_MINUTES)).strftime("%Y-%m-%d %H:%M")
    rows = conn.execute(
        """SELECT a.id, a.pet_id, cr.id AS care_id FROM appointments a
           JOIN care_records cr ON cr.appointment_id=a.id
           WHERE a.status='done' AND cr.picked_up_at IS NULL AND a.end_time <= ?
             AND NOT EXISTS (SELECT 1 FROM events e
                             WHERE e.appointment_id=a.id AND e.kind='overdue_pickup')""",
        (limit,)).fetchall()
    for r in rows:
        conn.execute(
            """INSERT INTO events(appointment_id, care_record_id, pet_id, kind, detail, created_by)
               VALUES (?,?,?, 'overdue_pickup', ?, '系统')""",
            (r["id"], r["care_id"], r["pet_id"],
             f"超过预计结束时间 {config.OVERDUE_PICKUP_MINUTES} 分钟仍未取宠"))
        enqueue_notification(conn, r["id"], "您的宠物已超时未取，请尽快到店接回", channel="sms")
    return len(rows)


def enqueue_notification(conn, appointment_id, payload, channel="sms",
                         run_at=None, status="pending"):
    run_at = run_at or datetime.now()
    cur = conn.execute(
        """INSERT INTO notifications(appointment_id, channel, payload, status, attempts, next_run_at)
           VALUES (?,?,?, ?, 0, ?)""",
        (appointment_id, channel, payload, status,
         run_at.strftime("%Y-%m-%d %H:%M:%S")))
    return cur.lastrowid


def simulated_sender(row):
    """模拟短信网关。

    测试用：channel 以 ``fail`` 开头时网关返回失败，驱动重试逻辑。
    返回 (是否成功, 错误信息)。
    """
    if (row["channel"] or "").startswith("fail"):
        return False, "模拟网关超时"
    return True, ""


def process_notifications(conn, at=None, sender=simulated_sender):
    """处理到期通知；失败按指数退避重试，超过上限标记 failed。

    返回本次处理的 id 列表。纯函数式、可在测试中直接调用。
    """
    at = at or datetime.now()
    stamp = at.strftime("%Y-%m-%d %H:%M:%S")
    due = conn.execute(
        "SELECT * FROM notifications WHERE status='pending' AND next_run_at <= ? ORDER BY id",
        (stamp,)).fetchall()
    processed = []
    for row in due:
        processed.append(row["id"])
        ok, err = sender(row)
        attempts = row["attempts"] + 1
        if ok:
            conn.execute(
                "UPDATE notifications SET status='sent', attempts=?, last_error='' WHERE id=?",
                (attempts, row["id"]))
        elif attempts >= config.NOTIFY_MAX_ATTEMPTS:
            conn.execute(
                "UPDATE notifications SET status='failed', attempts=?, last_error=? WHERE id=?",
                (attempts, f"{err}（已达最大重试次数）", row["id"]))
        else:
            delay = config.NOTIFY_INTERVAL_SECONDS * (
                config.NOTIFY_BACKOFF_BASE ** (attempts - 1))
            nxt = (at + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """UPDATE notifications SET status='pending', attempts=?, last_error=?,
                   next_run_at=? WHERE id=?""",
                (attempts, err, nxt, row["id"]))
    return processed
