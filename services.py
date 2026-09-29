"""业务服务层：预约锁定、护理流程、事件、照片权限、取宠核验、通知重试。

所有"先查后写"的操作都在 BEGIN IMMEDIATE 事务中完成，配合 SQLite 的
WAL 与 busy_timeout，保证并发下时段竞争只有一个请求成功。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from db import UPLOAD_DIR, connect

ACTIVE_APPT_STATUSES = ("booked", "reception_confirmed", "servicing", "completed")

# 通知重试退避（秒）。测试可 monkeypatch。
RETRY_BACKOFF = [5, 15, 60]
# 结单后超过宽限期仍未取宠 → 超时未取事件
OVERDUE_GRACE_MIN = 30


class AppError(Exception):
    """业务错误，http_status 供 API 层映射。"""

    def __init__(self, message: str, http_status: int = 400):
        super().__init__(message)
        self.http_status = http_status


class NotFound(AppError):
    def __init__(self, message: str = "记录不存在"):
        super().__init__(message, 404)


class Conflict(AppError):
    def __init__(self, message: str):
        super().__init__(message, 409)


class PermissionDenied(AppError):
    def __init__(self, message: str = "无权操作"):
        super().__init__(message, 403)


@contextmanager
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def row_to_dict(row: sqlite3.Row | None):
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# 基础资料查询
# ---------------------------------------------------------------------------

def list_contacts(conn=None) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM contacts ORDER BY id")]
    finally:
        if own:
            conn.close()


def list_pets(conn=None) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    try:
        rows = conn.execute(
            """SELECT p.*, c.name AS contact_name, c.phone AS contact_phone
               FROM pets p JOIN contacts c ON c.id = p.contact_id
               WHERE p.archived=0 ORDER BY p.id"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def pet_detail(pet_id: int, conn=None) -> dict:
    own = conn is None
    conn = conn or connect()
    try:
        pet = conn.execute(
            """SELECT p.*, c.name contact_name, c.phone contact_phone, c.address contact_address,
                      c.note contact_note
               FROM pets p JOIN contacts c ON c.id=p.contact_id WHERE p.id=?""",
            (pet_id,),
        ).fetchone()
        if not pet:
            raise NotFound("宠物不存在")
        pet = dict(pet)
        pet["appointments"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM appointments WHERE pet_id=? ORDER BY appt_date DESC, start_time DESC LIMIT 10",
                (pet_id,),
            )
        ]
        return pet
    finally:
        if own:
            conn.close()


def list_services(conn=None) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM services WHERE active=1 ORDER BY category, id")]
    finally:
        if own:
            conn.close()


def list_staff(conn=None, include_inactive=False) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    try:
        sql = "SELECT * FROM staff"
        if not include_inactive:
            sql += " WHERE active=1"
        return [dict(r) for r in conn.execute(sql + " ORDER BY id")]
    finally:
        if own:
            conn.close()


def list_stations(conn=None) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM stations WHERE active=1 ORDER BY id")]
    finally:
        if own:
            conn.close()


def list_supplies(conn=None) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM supplies ORDER BY id")]
    finally:
        if own:
            conn.close()


def shifts_on(date: str, conn=None) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    try:
        return [dict(r) for r in conn.execute(
            """SELECT sh.*, st.name station_name, s.name staff_name, s.color staff_color,
                      s.can_view_sensitive
               FROM shifts sh
               JOIN stations st ON st.id=sh.station_id
               JOIN staff s ON s.id=sh.staff_id
               WHERE sh.work_date=? ORDER BY sh.start_time""", (date,))]
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 预约：冲突检测 / 工位容量锁定 / 排班校验
# ---------------------------------------------------------------------------

def _overlapping(conn, table_col, value, date, start, end, exclude_id=None):
    """统计某日某资源（宠物/工位/美容师）时间区间重叠的有效预约数。

    [start, end) 区间相交条件：b.start < a.end AND a.start < b.end
    """
    sql = f"""SELECT COUNT(*) c FROM appointments
              WHERE {table_col}=? AND appt_date=?
                AND start_time < ? AND end_time > ?
                AND status IN ({','.join('?' * len(ACTIVE_APPT_STATUSES))})"""
    params = [value, date, end, start, *ACTIVE_APPT_STATUSES]
    if exclude_id:
        sql += " AND id<>?"
        params.append(exclude_id)
    return conn.execute(sql, params).fetchone()["c"]


def _station_overlap_count(conn, station_id, date, start, end, exclude_id=None):
    return _overlapping(conn, "station_id", station_id, date, start, end, exclude_id)


def check_availability(pet_id, staff_id, station_id, date, start, end,
                       exclude_id=None, conn=None) -> dict:
    """返回可用性信息；不抛错，供前端预检测。"""
    own = conn is None
    conn = conn or connect()
    try:
        problems = []
        if _overlapping(conn, "pet_id", pet_id, date, start, end, exclude_id):
            problems.append("pet_busy")
        station = conn.execute("SELECT capacity FROM stations WHERE id=?",
                               (station_id,)).fetchone()
        if not station:
            problems.append("no_station")
        elif _station_overlap_count(conn, station_id, date, start, end, exclude_id) >= station["capacity"]:
            problems.append("station_full")
        if staff_id and not _staff_available(conn, staff_id, date, start, end, exclude_id):
            problems.append("staff_busy")
        if staff_id and not _shift_covers(conn, staff_id, date, start, end):
            problems.append("no_shift")
        return {"available": not problems, "problems": problems}
    finally:
        if own:
            conn.close()


def _staff_available(conn, staff_id, date, start, end, exclude_id=None):
    return _overlapping(conn, "staff_id", staff_id, date, start, end, exclude_id) == 0


def _shift_covers(conn, staff_id, date, start, end):
    row = conn.execute(
        """SELECT 1 FROM shifts WHERE staff_id=? AND work_date=?
           AND start_time<=? AND end_time>=? LIMIT 1""",
        (staff_id, date, start, end),
    ).fetchone()
    return row is not None


def _compute_window(service_ids, conn):
    if not service_ids:
        raise AppError("请至少选择一个服务项目")
    placeholders = ",".join("?" * len(service_ids))
    rows = conn.execute(
        f"SELECT * FROM services WHERE id IN ({placeholders}) AND active=1", service_ids
    ).fetchall()
    if len(rows) != len(set(service_ids)):
        raise AppError("服务项目不存在或已停用")
    total = sum(r["duration_min"] for r in rows)
    snap = [{"id": r["id"], "name": r["name"], "duration_min": r["duration_min"],
             "price": r["price"], "category": r["category"]} for r in rows]
    return total, snap


def create_appointment(pet_id, staff_id, station_id, date, start, service_ids,
                       note="", reminder_minutes=30, conn=None) -> int:
    """创建预约。并发安全：冲突 / 工位满员 / 无排班时抛 Conflict。"""
    own = conn is None
    conn = conn or connect()
    try:
        with tx(conn):
            pet = conn.execute("SELECT * FROM pets WHERE id=?", (pet_id,)).fetchone()
            if not pet:
                raise NotFound("宠物不存在")
            total_min, snap = _compute_window(service_ids, conn)
            end = (datetime.strptime(f"{date} {start}", "%Y-%m-%d %H:%M")
                   + timedelta(minutes=total_min)).strftime("%H:%M")

            # 1) 同一宠物同一时段不得重复预约
            if _overlapping(conn, "pet_id", pet_id, date, start, end):
                raise Conflict(f"宠物「{pet['name']}」在 {date} {start}-{end} 已有重叠预约")
            # 2) 工位容量锁定
            station = conn.execute("SELECT * FROM stations WHERE id=?",
                                   (station_id,)).fetchone()
            if not station:
                raise NotFound("工位不存在")
            if _station_overlap_count(conn, station_id, date, start, end) >= station["capacity"]:
                raise Conflict(f"工位「{station['name']}」该时段已满（容量 {station['capacity']}）")
            # 3) 美容师不重叠且排班覆盖
            if staff_id:
                staff = conn.execute("SELECT * FROM staff WHERE id=?", (staff_id,)).fetchone()
                if not staff:
                    raise NotFound("美容师不存在")
                if _overlapping(conn, "staff_id", staff_id, date, start, end):
                    raise Conflict(f"美容师「{staff['name']}」该时段已有预约")
                if not _shift_covers(conn, staff_id, date, start, end):
                    raise Conflict(
                        f"美容师「{staff['name']}」在 {date} 没有覆盖 {start}-{end} 的排班")

            cur = conn.execute(
                """INSERT INTO appointments
                   (pet_id, contact_id, staff_id, station_id, appt_date, start_time, end_time,
                    estimated_min, breed_snapshot, size_snapshot, taboos_snapshot,
                    services_snapshot, status, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'booked', ?)""",
                (pet_id, pet["contact_id"], staff_id, station_id, date, start, end,
                 total_min, pet["breed"], pet["size"], pet["taboos"],
                 json.dumps(snap, ensure_ascii=False), note),
            )
            appt_id = cur.lastrowid
            # 到点提醒（进程内任务调度）
            remind_at = datetime.strptime(f"{date} {start}", "%Y-%m-%d %H:%M") \
                        - timedelta(minutes=reminder_minutes)
            conn.execute(
                """INSERT INTO notifications
                   (appointment_id, pet_id, kind, channel, payload, status, next_run_at)
                   VALUES (?,?, 'reminder','短信',?, 'pending', ?)""",
                (appt_id, pet_id,
                 f"提醒：{pet['name']} 将于 {start} 到店美容，请提前准备工位",
                 remind_at.strftime("%Y-%m-%d %H:%M:%S")),
            )
        return appt_id
    finally:
        if own:
            conn.close()


def appointments_between(date_from, date_to, conn=None) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    try:
        rows = conn.execute(
            """SELECT a.*, p.name pet_name, p.photo pet_photo, p.breed, p.size,
                      c.name contact_name, c.phone contact_phone,
                      s.name staff_name, s.color staff_color, st.name station_name
               FROM appointments a
               JOIN pets p ON p.id=a.pet_id
               JOIN contacts c ON c.id=a.contact_id
               LEFT JOIN staff s ON s.id=a.staff_id
               LEFT JOIN stations st ON st.id=a.station_id
               WHERE a.appt_date BETWEEN ? AND ?
               ORDER BY a.appt_date, a.start_time""", (date_from, date_to),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["services"] = json.loads(d["services_snapshot"] or "[]")
            result.append(d)
        return result
    finally:
        if own:
            conn.close()


def appointment_detail(appt_id, conn=None) -> dict:
    own = conn is None
    conn = conn or connect()
    try:
        r = conn.execute(
            """SELECT a.*, p.name pet_name, p.photo pet_photo, p.temperament,
                      c.name contact_name, c.phone contact_phone,
                      s.name staff_name, st.name station_name
               FROM appointments a
               JOIN pets p ON p.id=a.pet_id
               JOIN contacts c ON c.id=a.contact_id
               LEFT JOIN staff s ON s.id=a.staff_id
               LEFT JOIN stations st ON st.id=a.station_id
               WHERE a.id=?""", (appt_id,)).fetchone()
        if not r:
            raise NotFound("预约不存在")
        d = dict(r)
        d["services"] = json.loads(d["services_snapshot"] or "[]")
        d["order"] = row_to_dict(conn.execute(
            "SELECT * FROM care_orders WHERE appointment_id=?", (appt_id,)).fetchone())
        d["pickup"] = row_to_dict(conn.execute(
            "SELECT * FROM pickups WHERE appointment_id=? ORDER BY id DESC LIMIT 1",
            (appt_id,)).fetchone())
        return d
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 护理单流程：接待确认 → 逐项完成 → 结单
# ---------------------------------------------------------------------------

def confirm_reception(appt_id, by="前台", skin_note="", coat_note="", conn=None) -> int:
    """接待确认：预约 → 已接待，并生成护理单与护理项目。"""
    own = conn is None
    conn = conn or connect()
    try:
        with tx(conn):
            appt = conn.execute("SELECT * FROM appointments WHERE id=?", (appt_id,)).fetchone()
            if not appt:
                raise NotFound("预约不存在")
            if appt["status"] not in ("booked",):
                raise Conflict(f"当前状态（{appt['status']}）不能接待确认")
            services = json.loads(appt["services_snapshot"] or "[]")
            cur = conn.execute(
                """INSERT INTO care_orders
                   (appointment_id, pet_id, staff_id, status, skin_note, coat_note)
                   VALUES (?,?,?, 'open', ?, ?)""",
                (appt_id, appt["pet_id"], appt["staff_id"], skin_note, coat_note),
            )
            order_id = cur.lastrowid
            for i, sv in enumerate(services):
                conn.execute(
                    """INSERT INTO care_items
                       (order_id, service_id, name, duration_min, status, sort_no)
                       VALUES (?,?,?,?,'pending',?)""",
                    (order_id, sv["id"], sv["name"], sv["duration_min"], i),
                )
            conn.execute(
                "UPDATE appointments SET status='reception_confirmed', updated_at=? WHERE id=?",
                (now_str(), appt_id))
        return order_id
    finally:
        if own:
            conn.close()


def _get_order_for_update(conn, order_id):
    order = conn.execute("SELECT * FROM care_orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise NotFound("护理单不存在")
    return order


def set_item_status(item_id, status, by="美容师", remark="", conn=None) -> dict:
    """完成 / 挂起一项护理。完成一项记录一项（带时间戳）。"""
    if status not in ("done", "pending"):
        raise AppError("状态只支持 done / pending")
    own = conn is None
    conn = conn or connect()
    try:
        with tx(conn):
            item = conn.execute("SELECT * FROM care_items WHERE id=?", (item_id,)).fetchone()
            if not item:
                raise NotFound("护理项目不存在")
            order = _get_order_for_update(conn, item["order_id"])
            if order["status"] == "closed":
                raise Conflict("护理单已结单，不能再修改项目")
            ts = now_str() if status == "done" else None
            conn.execute(
                """UPDATE care_items SET status=?, completed_at=?, completed_by=?, remark=?
                   WHERE id=?""",
                (status, ts, by if status == "done" else "", remark, item_id))
            # 有任意项目完成 → 预约进入护理中
            if status == "done":
                conn.execute(
                    """UPDATE appointments SET status='servicing', updated_at=?
                       WHERE id=? AND status='reception_confirmed'""",
                    (ts, order["appointment_id"]))
        return order_item_status(item_id, conn=conn)
    finally:
        if own:
            conn.close()


def order_item_status(item_id, conn=None) -> dict:
    own = conn is None
    conn = conn or connect()
    try:
        return dict(conn.execute("SELECT * FROM care_items WHERE id=?", (item_id,)).fetchone())
    finally:
        if own:
            conn.close()


def order_detail(order_id, viewer_staff_id=None, conn=None) -> dict:
    own = conn is None
    conn = conn or connect()
    try:
        order = conn.execute(
            """SELECT o.*, p.name pet_name, p.photo pet_photo, p.breed, p.size,
                      p.taboos, p.skin_note pet_skin_note, p.coat_note pet_coat_note,
                      s.name staff_name, a.appt_date, a.start_time, a.end_time,
                      a.services_snapshot, a.id appointment_id,
                      c.name contact_name, c.phone contact_phone
               FROM care_orders o
               JOIN pets p ON p.id=o.pet_id
               JOIN appointments a ON a.id=o.appointment_id
               JOIN contacts c ON c.id=a.contact_id
               LEFT JOIN staff s ON s.id=o.staff_id
               WHERE o.id=?""", (order_id,)).fetchone()
        if not order:
            raise NotFound("护理单不存在")
        d = dict(order)
        d["items"] = [dict(r) for r in conn.execute(
            "SELECT * FROM care_items WHERE order_id=? ORDER BY sort_no, id", (order_id,))]
        d["events"] = [dict(r) for r in conn.execute(
            "SELECT * FROM events WHERE order_id=? ORDER BY id", (order_id,))]
        d["usages"] = [dict(r) for r in conn.execute(
            "SELECT * FROM supply_usages WHERE order_id=? ORDER BY id", (order_id,))]
        d["photos"] = visible_photos(conn, order_id=order_id, viewer_staff_id=viewer_staff_id)
        d["done_count"] = sum(1 for i in d["items"] if i["status"] == "done")
        d["pending_count"] = sum(1 for i in d["items"] if i["status"] == "pending")
        return d
    finally:
        if own:
            conn.close()


def close_order(order_id, by="美容师", summary="", conn=None) -> dict:
    """结单：存在未完成项目时禁止；不可重复结单。结单后生成取宠码。"""
    own = conn is None
    conn = conn or connect()
    try:
        with tx(conn):
            order = _get_order_for_update(conn, order_id)
            if order["status"] == "closed":
                raise Conflict("护理单已结单，不能重复结单")
            pending = conn.execute(
                "SELECT COUNT(*) c FROM care_items WHERE order_id=? AND status='pending'",
                (order_id,)).fetchone()["c"]
            if pending:
                names = [r["name"] for r in conn.execute(
                    "SELECT name FROM care_items WHERE order_id=? AND status='pending' ORDER BY sort_no",
                    (order_id,))]
                raise Conflict(f"还有 {pending} 项未完成，不能结单：{('、'.join(names))}")
            ts = now_str()
            conn.execute(
                """UPDATE care_orders SET status='closed', closed_at=?, summary=? WHERE id=?""",
                (ts, summary, order_id))
            conn.execute(
                "UPDATE appointments SET status='completed', updated_at=? WHERE id=?",
                (ts, order["appointment_id"]))
            code = uuid.uuid4().hex[:6].upper()
            conn.execute(
                "INSERT INTO pickups (appointment_id, code, method) VALUES (?,?,'取宠码')",
                (order["appointment_id"], code))
            # 取宠通知（到点可取）
            conn.execute(
                """INSERT INTO notifications
                   (appointment_id, pet_id, kind, channel, payload, status, next_run_at)
                   VALUES (?,?, 'pickup','短信',?, 'pending', ?)""",
                (order["appointment_id"], order["pet_id"],
                 f"宠物护理完成，取宠码 {code}，请凭码到店取宠", ts))
        return {"order_id": order_id, "pickup_code": code}
    finally:
        if own:
            conn.close()


def confirm_record(order_id, by="店长", conn=None) -> None:
    """护理记录确认：确认后不可删除。"""
    own = conn is None
    conn = conn or connect()
    try:
        with tx(conn):
            order = _get_order_for_update(conn, order_id)
            if order["confirmed"]:
                raise Conflict("护理记录已确认，请勿重复确认")
            conn.execute(
                "UPDATE care_orders SET confirmed=1, confirmed_at=?, confirmed_by=? WHERE id=?",
                (now_str(), by, order_id))
    finally:
        if own:
            conn.close()


def delete_order(order_id, conn=None) -> None:
    """删除护理单：仅未确认记录可删（确认后不能删除）。"""
    own = conn is None
    conn = conn or connect()
    try:
        with tx(conn):
            order = _get_order_for_update(conn, order_id)
            if order["confirmed"]:
                raise PermissionDenied("护理记录已确认，不能删除")
            if order["status"] == "closed":
                raise Conflict("已结单记录不能删除，请先联系店长")
            conn.execute("UPDATE appointments SET status='booked', updated_at=? WHERE id=?",
                         (now_str(), order["appointment_id"]))
            conn.execute("DELETE FROM care_items WHERE order_id=?", (order_id,))
            conn.execute("DELETE FROM events WHERE order_id=?", (order_id,))
            conn.execute("DELETE FROM care_orders WHERE id=?", (order_id,))
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 主人临时改项
# ---------------------------------------------------------------------------

def owner_change_services(order_id, new_service_ids, by="前台", reason="", conn=None) -> dict:
    """主人临时改项：更新护理项目与预约快照，生成 change 事件。

    - 新增项目：追加 pending 护理项；
    - 被去掉且尚未做：删除；已完成的保留；
    - 只能在结单前修改。
    """
    own = conn is None
    conn = conn or connect()
    try:
        with tx(conn):
            order = _get_order_for_update(conn, order_id)
            if order["status"] == "closed":
                raise Conflict("护理单已结单，不能再改项")
            _, snap = _compute_window(new_service_ids, conn)
            want_ids = set(new_service_ids)

            existing = conn.execute(
                "SELECT * FROM care_items WHERE order_id=?", (order_id,)).fetchall()
            existing_by_sid = {i["service_id"]: i for i in existing if i["service_id"]}
            added, removed, kept_done = [], [], []
            max_sort = max([i["sort_no"] for i in existing], default=-1)
            for sid in want_ids - set(existing_by_sid):
                sv = next(s for s in snap if s["id"] == sid)
                max_sort += 1
                conn.execute(
                    """INSERT INTO care_items
                       (order_id, service_id, name, duration_min, status, sort_no)
                       VALUES (?,?,?,?,'pending',?)""",
                    (order_id, sid, sv["name"], sv["duration_min"], max_sort))
                added.append(sv["name"])
            for sid, item in existing_by_sid.items():
                if sid not in want_ids:
                    if item["status"] == "done":
                        kept_done.append(item["name"])  # 已完成保留
                    else:
                        conn.execute("DELETE FROM care_items WHERE id=?", (item["id"],))
                        removed.append(item["name"])

            appt = conn.execute("SELECT * FROM appointments WHERE id=?",
                                (order["appointment_id"],)).fetchone()
            total = sum(s["duration_min"] for s in snap)
            end = (datetime.strptime(f"{appt['appt_date']} {appt['start_time']}",
                                     "%Y-%m-%d %H:%M")
                   + timedelta(minutes=total)).strftime("%H:%M")
            conn.execute(
                """UPDATE appointments SET services_snapshot=?, estimated_min=?, end_time=?,
                   updated_at=? WHERE id=?""",
                (json.dumps(snap, ensure_ascii=False), total, end, now_str(), appt["id"]))

            detail_parts = []
            if added:
                detail_parts.append("新增：" + "、".join(added))
            if removed:
                detail_parts.append("取消：" + "、".join(removed))
            if kept_done:
                detail_parts.append("已完成保留：" + "、".join(kept_done))
            if not detail_parts:
                detail_parts.append("服务组合无变化")
            detail = f"主人临时改项（{by}登记）。{'；'.join(detail_parts)}。"
            if reason:
                detail += f"原因：{reason}。"
            conn.execute(
                """INSERT INTO events (appointment_id, order_id, pet_id, type, title, detail, created_by)
                   VALUES (?,?,?, 'change', '主人临时改项', ?, ?)""",
                (appt["id"], order_id, order["pet_id"], detail, by))
        return order_detail(order_id, conn=conn)
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 事件（抗拒 / 皮肤异常 / 剪伤 / 超时未取 / 用品过敏）
# ---------------------------------------------------------------------------

def add_event(event_type, title="", detail="", by="前台", pet_id=None,
              appointment_id=None, order_id=None, conn=None) -> int:
    valid = {"resistance", "skin", "cut", "change", "overdue", "allergy"}
    if event_type not in valid:
        raise AppError("未知事件类型")
    own = conn is None
    conn = conn or connect()
    try:
        with tx(conn):
            if appointment_id and not pet_id:
                row = conn.execute("SELECT pet_id FROM appointments WHERE id=?",
                                   (appointment_id,)).fetchone()
                if row:
                    pet_id = row["pet_id"]
            if order_id and not appointment_id:
                row = conn.execute("SELECT appointment_id FROM care_orders WHERE id=?",
                                   (order_id,)).fetchone()
                if row:
                    appointment_id = row["appointment_id"]
            cur = conn.execute(
                """INSERT INTO events (appointment_id, order_id, pet_id, type, title, detail, created_by)
                   VALUES (?,?,?,?,?,?,?)""",
                (appointment_id, order_id, pet_id, event_type, title, detail, by))
            event_id = cur.lastrowid
            # 异常类事件通知店长（进程内任务）
            if event_type in ("cut", "skin", "allergy", "overdue"):
                conn.execute(
                    """INSERT INTO notifications
                       (appointment_id, pet_id, kind, channel, payload, status, next_run_at)
                       VALUES (?,?, 'event','站内',?, 'pending', ?)""",
                    (appointment_id, pet_id, f"[{event_type}] {title}：{detail}", now_str()))
        return event_id
    finally:
        if own:
            conn.close()


def list_events(event_type=None, conn=None) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    try:
        sql = """SELECT e.*, p.name pet_name, a.appt_date, a.start_time
                 FROM events e LEFT JOIN pets p ON p.id=e.pet_id
                 LEFT JOIN appointments a ON a.id=e.appointment_id"""
        params = ()
        if event_type:
            sql += " WHERE e.type=?"
            params = (event_type,)
        sql += " ORDER BY e.id DESC"
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 照片：本地目录 + 敏感照片授权
# ---------------------------------------------------------------------------

def is_authorized_sensitive(staff_id, conn=None) -> bool:
    if not staff_id:
        return False
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("SELECT can_view_sensitive FROM staff WHERE id=?",
                           (staff_id,)).fetchone()
        return bool(row and row["can_view_sensitive"])
    finally:
        if own:
            conn.close()


def save_photo(data: bytes, original_name: str, *, kind="normal", pet_id=None,
               appointment_id=None, event_id=None, caption="", uploaded_by="前台") -> dict:
    """把照片写入本地 uploads 目录并登记。"""
    ext = Path(original_name).suffix.lower() or ".img"
    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".img"):
        ext = ".img"
    stored = f"{uuid.uuid4().hex}{ext}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / stored).write_bytes(data)
    conn = connect()
    try:
        with tx(conn):
            cur = conn.execute(
                """INSERT INTO photos (filename, pet_id, appointment_id, event_id, kind,
                                       caption, uploaded_by)
                   VALUES (?,?,?,?,?,?,?)""",
                (stored, pet_id, appointment_id, event_id, kind, caption, uploaded_by))
            photo_id = cur.lastrowid
        return {"id": photo_id, "filename": stored, "kind": kind}
    finally:
        conn.close()


def get_photo_meta(photo_id, viewer_staff_id=None, conn=None) -> dict:
    """读取照片元数据；sensitive 照片必须由授权人员查看，否则 403。"""
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
        if not row:
            raise NotFound("照片不存在")
        photo = dict(row)
        if photo["kind"] == "sensitive" and not is_authorized_sensitive(viewer_staff_id, conn):
            raise PermissionDenied("异常照片仅限授权人员查看")
        return photo
    finally:
        if own:
            conn.close()


def visible_photos(conn, order_id=None, viewer_staff_id=None) -> list[dict]:
    """护理单可见照片：敏感照片对非授权人员只返回占位信息（不返回文件名）。"""
    authorized = is_authorized_sensitive(viewer_staff_id, conn)
    if order_id:
        rows = conn.execute(
            "SELECT * FROM photos WHERE "
            "(appointment_id=(SELECT appointment_id FROM care_orders WHERE id=?) "
            "OR event_id IN (SELECT id FROM events WHERE order_id=?)) ORDER BY id",
            (order_id, order_id)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM photos ORDER BY id DESC LIMIT 200").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if d["kind"] == "sensitive" and not authorized:
            d["filename"] = ""  # 不暴露真实文件路径
            d["locked"] = True
        else:
            d["locked"] = False
        result.append(d)
    return result


def list_all_photos(viewer_staff_id=None, conn=None) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    try:
        photos = visible_photos(conn, viewer_staff_id=viewer_staff_id)
        for p in photos:
            r = conn.execute("SELECT name FROM pets WHERE id=?", (p["pet_id"],)).fetchone()
            p["pet_name"] = r["name"] if r else ""
        return photos
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 用品消耗 / 用品过敏
# ---------------------------------------------------------------------------

def record_supply_usage(order_id, supply_id, qty=1, by="美容师",
                        caused_allergy=False, allergy_detail="", conn=None) -> int:
    own = conn is None
    conn = conn or connect()
    try:
        with tx(conn):
            order = _get_order_for_update(conn, order_id)
            supply = conn.execute("SELECT * FROM supplies WHERE id=?", (supply_id,)).fetchone()
            if not supply:
                raise NotFound("用品不存在")
            cur = conn.execute(
                """INSERT INTO supply_usages
                   (order_id, appointment_id, supply_id, supply_name, qty, caused_allergy, used_by)
                   VALUES (?,?,?,?,?,?,?)""",
                (order_id, order["appointment_id"], supply_id, supply["name"], qty,
                 1 if caused_allergy else 0, by))
            usage_id = cur.lastrowid
            conn.execute("UPDATE supplies SET stock=stock-? WHERE id=?", (qty, supply_id))
            if caused_allergy:
                detail = f"使用用品「{supply['name']}」x{qty} 后出现过敏反应。{allergy_detail}".strip()
                conn.execute(
                    """INSERT INTO events (appointment_id, order_id, pet_id, type, title, detail, created_by)
                       VALUES (?,?,?, 'allergy', '用品过敏', ?, ?)""",
                    (order["appointment_id"], order_id, order["pet_id"], detail, by))
                conn.execute(
                    """INSERT INTO notifications
                       (appointment_id, pet_id, kind, channel, payload, status, next_run_at)
                       VALUES (?,?, 'event','站内',?, 'pending', ?)""",
                    (order["appointment_id"], order["pet_id"],
                     f"[allergy] 用品过敏：{detail}", now_str()))
        return usage_id
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 取宠核验
# ---------------------------------------------------------------------------

def verify_pickup(appt_id, code, verifier="前台", conn=None) -> dict:
    own = conn is None
    conn = conn or connect()
    try:
        with tx(conn):
            appt = conn.execute("SELECT * FROM appointments WHERE id=?", (appt_id,)).fetchone()
            if not appt:
                raise NotFound("预约不存在")
            pickup = conn.execute(
                "SELECT * FROM pickups WHERE appointment_id=? ORDER BY id DESC LIMIT 1",
                (appt_id,)).fetchone()
            if not pickup:
                raise Conflict("该预约尚未结单，没有取宠码")
            if pickup["verified"]:
                raise Conflict("该宠物已完成取宠核验")
            if appt["status"] != "completed":
                raise Conflict("护理尚未完成，不能取宠")
            if pickup["code"].strip().upper() != code.strip().upper():
                raise PermissionDenied("取宠码不符，核验失败")
            ts = now_str()
            conn.execute("UPDATE pickups SET verified=1, verified_at=?, verifier=? WHERE id=?",
                         (ts, verifier, pickup["id"]))
            conn.execute("UPDATE appointments SET status='picked_up', updated_at=? WHERE id=?",
                         (ts, appt_id))
        return {"verified": True, "verified_at": ts}
    finally:
        if own:
            conn.close()


def pickup_status(appt_id, conn=None) -> dict:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            "SELECT * FROM pickups WHERE appointment_id=? ORDER BY id DESC LIMIT 1",
            (appt_id,)).fetchone()
        if not row:
            raise NotFound("取宠记录不存在")
        return dict(row)
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 工位排程时间轴
# ---------------------------------------------------------------------------

def station_timeline(date: str, conn=None) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    try:
        stations = [dict(r) for r in conn.execute(
            "SELECT * FROM stations WHERE active=1 ORDER BY id")]
        for st in stations:
            st["shifts"] = [dict(r) for r in conn.execute(
                """SELECT sh.*, s.name staff_name, s.color staff_color
                   FROM shifts sh JOIN staff s ON s.id=sh.staff_id
                   WHERE sh.station_id=? AND sh.work_date=? ORDER BY sh.start_time""",
                (st["id"], date))]
            appts = conn.execute(
                """SELECT a.id, a.start_time, a.end_time, a.status, a.estimated_min,
                          p.name pet_name, p.breed, p.size, s.name staff_name, s.color
                   FROM appointments a
                   JOIN pets p ON p.id=a.pet_id
                   LEFT JOIN staff s ON s.id=a.staff_id
                   WHERE a.station_id=? AND a.appt_date=?
                     AND a.status IN ('booked','reception_confirmed','servicing','completed','picked_up')
                   ORDER BY a.start_time""", (st["id"], date)).fetchall()
            st["appointments"] = [dict(r) for r in appts]
        return stations
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 通知：发送模拟 + 重试退避
# ---------------------------------------------------------------------------

def _simulate_send(payload: str) -> tuple[bool, str]:
    """模拟短信/站内发送。

    payload 以 "fail:always:" 开头表示一直失败；"fail:N:" 前缀本身不导致失败
    （前 N 次失败的判定在 attempt_send 中按 attempts 计数处理）。
    """
    if payload.startswith("fail:always:"):
        return False, "模拟发送失败（演示用强制失败标记）"
    return True, ""


def attempt_send(notif_id, conn=None) -> dict:
    """尝试发送一条通知，失败按指数退避安排重试，超过最大次数标记 failed。"""
    own = conn is None
    conn = conn or connect()
    try:
        with tx(conn):
            n = conn.execute("SELECT * FROM notifications WHERE id=?", (notif_id,)).fetchone()
            if not n:
                raise NotFound("通知不存在")
            if n["status"] in ("sent", "failed"):
                return dict(n)
            # fail:N 标记：当前尝试次数 < N 时失败
            payload = n["payload"]
            force_fail = False
            if payload.startswith("fail:"):
                parts = payload.split(":", 2)
                rule = parts[1]
                if rule == "always":
                    force_fail = True
                else:
                    force_fail = n["attempts"] < int(rule)
            attempts = n["attempts"] + 1
            if not force_fail:
                ok, err = _simulate_send(payload)
            else:
                ok, err = False, "模拟发送失败（演示用强制失败标记）"
            if ok:
                conn.execute(
                    """UPDATE notifications SET status='sent', attempts=?, sent_at=?,
                       last_error='' WHERE id=?""",
                    (attempts, now_str(), notif_id))
                return {"id": notif_id, "status": "sent", "attempts": attempts}
            if attempts >= n["max_attempts"]:
                conn.execute(
                    "UPDATE notifications SET status='failed', attempts=?, last_error=? WHERE id=?",
                    (attempts, err, notif_id))
                return {"id": notif_id, "status": "failed", "attempts": attempts,
                        "last_error": err}
            delay = RETRY_BACKOFF[min(attempts - 1, len(RETRY_BACKOFF) - 1)]
            nxt = (datetime.now() + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """UPDATE notifications SET status='pending', attempts=?, next_run_at=?,
                   last_error=? WHERE id=?""",
                (attempts, nxt, err, notif_id))
            return {"id": notif_id, "status": "pending", "attempts": attempts,
                    "next_run_at": nxt, "last_error": err}
    finally:
        if own:
            conn.close()


def list_notifications(limit=50, conn=None) -> list[dict]:
    own = conn is None
    conn = conn or connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))]
    finally:
        if own:
            conn.close()


def due_notifications(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM notifications WHERE status='pending' AND next_run_at<=? ORDER BY id",
        (now_str(),))]


def scan_overdue(conn=None) -> list[int]:
    """扫描超时未取：已结单超过宽限期仍未核验取宠 → 生成 overdue 事件（每单一次）。"""
    own = conn is None
    conn = conn or connect()
    created = []
    try:
        rows = conn.execute(
            """SELECT a.id, a.appt_date, a.end_time, a.pet_id, p.name pet_name,
                      (SELECT MIN(verified_at) FROM pickups pu WHERE pu.appointment_id=a.id) verified_at
               FROM appointments a JOIN pets p ON p.id=a.pet_id
               WHERE a.status='completed'
                 AND NOT EXISTS (SELECT 1 FROM events e
                                 WHERE e.appointment_id=a.id AND e.type='overdue')"""
        ).fetchall()
        for r in rows:
            try:
                due = datetime.strptime(f"{r['appt_date']} {r['end_time']}",
                                        "%Y-%m-%d %H:%M") + timedelta(minutes=OVERDUE_GRACE_MIN)
            except ValueError:
                continue
            if datetime.now() >= due:
                with tx(conn):
                    conn.execute(
                        """INSERT INTO events (appointment_id, pet_id, type, title, detail, created_by)
                           VALUES (?,?, 'overdue', '超时未取',
                                   ?, '系统')""",
                        (r["id"], r["pet_id"],
                         f"宠物「{r['pet_name']}」已超过约定取宠时间 {r['end_time']} "
                         f"{OVERDUE_GRACE_MIN} 分钟仍未取走，请尽快联系主人"))
                    conn.execute(
                        """INSERT INTO notifications
                           (appointment_id, pet_id, kind, channel, payload, status, next_run_at)
                           VALUES (?,?, 'overdue','短信',?, 'pending', ?)""",
                        (r["id"], r["pet_id"],
                         f"超时未取提醒：{r['pet_name']} 等待取宠已超时", now_str()))
                created.append(r["id"])
        return created
    finally:
        if own:
            conn.close()
