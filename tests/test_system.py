"""系统测试：时段竞争、服务变更、照片权限、重复结单、通知重试、超时未取等。

直接运行：python3 -m unittest -v
（每个用例使用独立临时数据库与 uploads 目录，不污染演示库）
"""
from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import db
import seed as seed_mod
import server as server_mod
import services
from db import connect, init_db
from notify import NotificationWorker

TODAY = datetime.now().date()
D0 = TODAY.strftime("%Y-%m-%d")
DY = (TODAY - timedelta(days=1)).strftime("%Y-%m-%d")


class Fixture:
    """构造最小可用数据集。"""

    def __init__(self):
        conn = connect()
        cids = []
        for name, phone in [("测试主人甲", "13800000001"), ("测试主人乙", "13800000002")]:
            cids.append(conn.execute(
                "INSERT INTO contacts (name,phone) VALUES (?,?)", (name, phone)).lastrowid)
        self.pets = []
        for cid, name, breed, size, taboos in [
            (cids[0], "测试狗", "比熊", "小型", "禁用香精护毛素"),
            (cids[1], "测试猫", "布偶", "中型", "禁止烘干箱"),
        ]:
            self.pets.append(conn.execute(
                """INSERT INTO pets (contact_id,name,species,breed,size,taboos,skin_note)
                   VALUES (?,?, '犬',?,?,?, '测试皮肤备注')""",
                (cid, name, breed, size, taboos)).lastrowid)
        self.services = {}
        for sname, dur, price in [("洗澡", 60, 80), ("剪指甲", 20, 25),
                                  ("造型", 60, 130), ("洁牙", 30, 98),
                                  ("耳道护理", 15, 30)]:
            self.services[sname] = conn.execute(
                "INSERT INTO services (name,duration_min,price) VALUES (?,?,?)",
                (sname, dur, price)).lastrowid
        self.staff_authorized = conn.execute(
            "INSERT INTO staff (name,can_view_sensitive,color) VALUES ('店长周',1,'#2E4A62')",
        ).lastrowid
        self.staff_plain = conn.execute(
            "INSERT INTO staff (name,can_view_sensitive,color) VALUES ('助理淼',0,'#F3B39A')",
        ).lastrowid
        self.station_a = conn.execute(
            "INSERT INTO stations (name,capacity) VALUES ('A工位',1)").lastrowid
        self.station_dryer = conn.execute(
            "INSERT INTO stations (name,capacity) VALUES ('烘干位',2)").lastrowid
        # 两位美容师在两个工位都有 08:00-20:00 排班，覆盖测试时段
        for d in (-2, -1, 0, 1, 2):
            day = (TODAY + timedelta(days=d)).strftime("%Y-%m-%d")
            for staff_id in (self.staff_authorized, self.staff_plain):
                for station_id in (self.station_a, self.station_dryer):
                    conn.execute(
                        """INSERT INTO shifts (staff_id,station_id,work_date,start_time,end_time)
                           VALUES (?,?,?, '08:00','20:00')""",
                        (staff_id, station_id, day))
        for sname, stock in [("香波", 10), ("止血粉", 5), ("燕麦护毛素", 8)]:
            conn.execute("INSERT INTO supplies (name,stock) VALUES (?,?)", (sname, stock))
        conn.commit()
        conn.close()

    def sid(self, *names):
        return [self.services[n] for n in names]


class BaseCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._patches = []
        # 重定向数据库与上传目录到临时路径
        self._orig = {
            "db_path": db.DB_PATH, "db_upload": db.UPLOAD_DIR,
            "svc_upload": services.UPLOAD_DIR, "srv_upload": server_mod.UPLOAD_DIR,
            "seed_path": seed_mod.DB_PATH, "seed_upload": seed_mod.UPLOAD_DIR,
        }
        db.DB_PATH = tmp / "test.db"
        db.UPLOAD_DIR = tmp / "uploads"
        services.UPLOAD_DIR = db.UPLOAD_DIR
        server_mod.UPLOAD_DIR = db.UPLOAD_DIR
        seed_mod.DB_PATH = db.DB_PATH
        seed_mod.UPLOAD_DIR = db.UPLOAD_DIR
        init_db()
        self.fx = Fixture()

    def tearDown(self):
        db.DB_PATH = self._orig["db_path"]
        db.UPLOAD_DIR = self._orig["db_upload"]
        services.UPLOAD_DIR = self._orig["svc_upload"]
        server_mod.UPLOAD_DIR = self._orig["srv_upload"]
        seed_mod.DB_PATH = self._orig["seed_path"]
        seed_mod.UPLOAD_DIR = self._orig["seed_upload"]
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# 1. 时段竞争：同一宠物 / 工位容量并发锁定
# ---------------------------------------------------------------------------
class TestSlotRace(BaseCase):
    def test_same_pet_concurrent_only_one_wins(self):
        barrier = threading.Barrier(2)
        results = []

        def book():
            barrier.wait()
            try:
                aid = services.create_appointment(
                    self.fx.pets[0], self.fx.staff_authorized, self.fx.station_a,
                    D0, "09:00", self.fx.sid("洗澡"))
                results.append(("ok", aid))
            except services.Conflict as e:
                results.append(("conflict", str(e)))
            except Exception as e:  # noqa: BLE001
                results.append(("error", str(e)))

        threads = [threading.Thread(target=book) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        self.assertEqual(len(results), 2)
        oks = [r for r in results if r[0] == "ok"]
        conflicts = [r for r in results if r[0] == "conflict"]
        self.assertEqual(len(oks), 1, f"应有且仅有一个预约成功，实际：{results}")
        self.assertEqual(len(conflicts), 1)
        conn = connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) c FROM appointments WHERE pet_id=? AND appt_date=?",
                (self.fx.pets[0], D0)).fetchone()["c"]
            self.assertEqual(count, 1)
        finally:
            conn.close()

    def test_station_capacity_concurrent(self):
        """容量 1 的工位，不同宠物、不同美容师同时抢，也只能成一单。"""
        barrier = threading.Barrier(2)
        results = []

        def book(pet_id, staff_id):
            barrier.wait()
            try:
                results.append(("ok", services.create_appointment(
                    pet_id, staff_id, self.fx.station_a,
                    D0, "11:00", self.fx.sid("剪指甲"))))
            except services.Conflict as e:
                results.append(("conflict", str(e)))

        ts = [
            threading.Thread(target=book, args=(self.fx.pets[0], self.fx.staff_authorized)),
            threading.Thread(target=book, args=(self.fx.pets[1], self.fx.staff_plain)),
        ]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        self.assertEqual(sum(1 for r in results if r[0] == "ok"), 1, results)

    def test_capacity_two_allows_two_parallel(self):
        """容量 2 的烘干位允许两个预约并行（不同美容师）。"""
        a1 = services.create_appointment(
            self.fx.pets[0], self.fx.staff_authorized, self.fx.station_dryer,
            D0, "14:00", self.fx.sid("剪指甲"))
        a2 = services.create_appointment(
            self.fx.pets[1], self.fx.staff_plain, self.fx.station_dryer,
            D0, "14:05", self.fx.sid("剪指甲"))
        self.assertTrue(a1 and a2)
        # 第三个必须被容量锁定拒绝
        with self.assertRaises(services.Conflict):
            # 容量已满；美容师同时段也会冲突，这里新增第三位美容师验证工位容量
            conn = connect()
            s3 = conn.execute(
                "INSERT INTO staff (name,can_view_sensitive) VALUES ('临时工',0)").lastrowid
            p3 = conn.execute(
                "INSERT INTO pets (contact_id,name) VALUES (1,'第三只')").lastrowid
            conn.execute(
                """INSERT INTO shifts (staff_id,station_id,work_date,start_time,end_time)
                   VALUES (?,?,?, '08:00','20:00')""",
                (s3, self.fx.station_dryer, D0))
            conn.commit()
            conn.close()
            services.create_appointment(
                p3, s3, self.fx.station_dryer,
                D0, "14:10", self.fx.sid("剪指甲"))

    def test_sequential_duplicate_rejected(self):
        services.create_appointment(
            self.fx.pets[0], self.fx.staff_authorized, self.fx.station_a,
            D0, "09:00", self.fx.sid("洗澡"))  # 09:00-10:00
        with self.assertRaises(services.Conflict):
            services.create_appointment(
                self.fx.pets[0], self.fx.staff_plain, self.fx.station_dryer,
                D0, "09:30", self.fx.sid("剪指甲"))  # 09:30-09:50 重叠
        # 首尾相接不视为重叠：10:00 开始可以
        aid = services.create_appointment(
            self.fx.pets[0], self.fx.staff_plain, self.fx.station_dryer,
            D0, "10:00", self.fx.sid("剪指甲"))
        self.assertTrue(aid)

    def test_shift_cover_required(self):
        """美容师排班不覆盖时段时拒绝预约。"""
        with self.assertRaises(services.Conflict):
            services.create_appointment(
                self.fx.pets[0], self.fx.staff_authorized, self.fx.station_dryer,
                D0, "06:30", self.fx.sid("剪指甲"))  # 排班 08:00 才开始

    def test_snapshot_saved(self):
        aid = services.create_appointment(
            self.fx.pets[0], self.fx.staff_authorized, self.fx.station_a,
            D0, "09:00", self.fx.sid("洗澡", "剪指甲"), note="轻一点")
        d = services.appointment_detail(aid)
        self.assertEqual(d["breed_snapshot"], "比熊")
        self.assertEqual(d["size_snapshot"], "小型")
        self.assertIn("禁用香精", d["taboos_snapshot"])
        self.assertEqual(d["estimated_min"], 80)
        self.assertEqual(d["end_time"], "10:20")
        self.assertEqual({s["name"] for s in d["services"]}, {"洗澡", "剪指甲"})


# ---------------------------------------------------------------------------
# 2. 主人临时改项
# ---------------------------------------------------------------------------
class TestServiceChange(BaseCase):
    def _open_order(self):
        aid = services.create_appointment(
            self.fx.pets[0], self.fx.staff_authorized, self.fx.station_a,
            D0, "09:00", self.fx.sid("洗澡", "剪指甲"))
        oid = services.confirm_reception(aid)
        return aid, oid

    def test_change_add_remove_keeps_done(self):
        aid, oid = self._open_order()
        detail = services.order_detail(oid)
        nails = next(i for i in detail["items"] if i["name"] == "剪指甲")
        services.set_item_status(nails["id"], "done", by="店长周")

        # 去掉「剪指甲」（已完成→保留）、去掉「洗澡」（未做→删除）、新增「造型」
        updated = services.owner_change_services(
            oid, self.fx.sid("剪指甲", "造型"), by="前台", reason="主人要求做造型")

        names = {i["name"]: i for i in updated["items"]}
        self.assertIn("造型", names)
        self.assertIn("剪指甲", names)
        self.assertEqual(names["剪指甲"]["status"], "done")  # 已完成保留
        self.assertEqual(names["造型"]["status"], "pending")
        self.assertNotIn("洗澡", names)

        # 预计时长与结束时间重算：剪指甲20 + 造型60 = 80
        appt = services.appointment_detail(aid)
        self.assertEqual(appt["estimated_min"], 80)
        self.assertEqual(appt["end_time"], "10:20")

        # 生成 change 事件
        events = services.list_events("change")
        self.assertTrue(any("主人临时改项" in e["title"] and "造型" in e["detail"]
                            for e in events))

    def test_change_after_close_rejected(self):
        _aid, oid = self._open_order()
        for item in services.order_detail(oid)["items"]:
            services.set_item_status(item["id"], "done", by="店长周")
        services.close_order(oid)
        with self.assertRaises(services.Conflict):
            services.owner_change_services(oid, self.fx.sid("洗澡", "造型"))


# ---------------------------------------------------------------------------
# 3. 异常照片权限（服务层 + HTTP 文件接口）
# ---------------------------------------------------------------------------
class TestPhotoPermission(BaseCase):
    def _photos(self):
        normal = services.save_photo(
            b"normal-bytes", "n.svg", kind="normal", pet_id=self.fx.pets[0],
            caption="普通照片")
        sensitive = services.save_photo(
            b"<svg>sensitive</svg>", "s.svg", kind="sensitive",
            pet_id=self.fx.pets[0], caption="剪伤特写")
        return normal, sensitive

    def test_service_layer_permission(self):
        normal, sensitive = self._photos()
        # 未授权：元数据拒绝
        with self.assertRaises(services.PermissionDenied):
            services.get_photo_meta(sensitive["id"], self.fx.staff_plain)
        with self.assertRaises(services.PermissionDenied):
            services.get_photo_meta(sensitive["id"], None)
        # 授权：可以查看
        meta = services.get_photo_meta(sensitive["id"], self.fx.staff_authorized)
        self.assertEqual(meta["kind"], "sensitive")
        # 普通照片任何人可看
        self.assertEqual(
            services.get_photo_meta(normal["id"], self.fx.staff_plain)["kind"], "normal")
        # 列表对未授权人员隐藏真实文件名
        hidden = [p for p in services.list_all_photos(self.fx.staff_plain)
                  if p["id"] == sensitive["id"]][0]
        self.assertTrue(hidden["locked"])
        self.assertEqual(hidden["filename"], "")
        shown = [p for p in services.list_all_photos(self.fx.staff_authorized)
                 if p["id"] == sensitive["id"]][0]
        self.assertFalse(shown["locked"])
        self.assertTrue(shown["filename"])

    def test_http_file_endpoint_permission(self):
        _normal, sensitive = self._photos()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            url = f"{base}/api/photo_file?filename={sensitive['filename']}"

            def get(viewer=None):
                req = urllib.request.Request(url)
                if viewer:
                    req.add_header("X-Staff-Id", str(viewer))
                return urllib.request.urlopen(req, timeout=5)

            # 未登录 → 403
            with self.assertRaises(urllib.error.HTTPError) as cm:
                get(None)
            self.assertEqual(cm.exception.code, 403)
            # 无权限员工 → 403
            with self.assertRaises(urllib.error.HTTPError) as cm:
                get(self.fx.staff_plain)
            self.assertEqual(cm.exception.code, 403)
            # 授权员工 → 200 且拿到真实字节
            resp = get(self.fx.staff_authorized)
            self.assertEqual(resp.status, 200)
            self.assertIn(b"sensitive", resp.read())
            # 文件名穿越 → 404
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(
                    f"{base}/api/photo_file?filename=../etc/passwd", timeout=5)
            self.assertEqual(cm.exception.code, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    def test_upload_sensitive_still_protected(self):
        """上传者是未授权人员也不影响后续访问控制（服务端按查看者鉴权）。"""
        photo = services.save_photo(
            b"x", "x.png", kind="sensitive", pet_id=self.fx.pets[1],
            uploaded_by="助理淼")
        with self.assertRaises(services.PermissionDenied):
            services.get_photo_meta(photo["id"], self.fx.staff_plain)


# ---------------------------------------------------------------------------
# 4. 护理流程：未完成不能结单 / 重复结单 / 确认后不可删除
# ---------------------------------------------------------------------------
class TestCareFlow(BaseCase):
    def _order(self):
        aid = services.create_appointment(
            self.fx.pets[0], self.fx.staff_authorized, self.fx.station_a,
            D0, "09:00", self.fx.sid("洗澡", "剪指甲"))
        oid = services.confirm_reception(aid)
        return aid, oid

    def test_cannot_close_with_pending(self):
        _aid, oid = self._open_order_unwrapped()
        with self.assertRaises(services.Conflict) as cm:
            services.close_order(oid)
        self.assertIn("未完成", str(cm.exception))

    def _open_order_unwrapped(self):
        return self._order()

    def test_close_once_and_pickup(self):
        aid, oid = self._order()
        for item in services.order_detail(oid)["items"]:
            services.set_item_status(item["id"], "done", by="店长周")
        result = services.close_order(oid, summary="完成")
        self.assertRegex(result["pickup_code"], r"^[0-9A-F]{6}$")

        # 重复结单被拒绝
        with self.assertRaises(services.Conflict):
            services.close_order(oid)

        # 结单后项目不能再改
        item = services.order_detail(oid)["items"][0]
        with self.assertRaises(services.Conflict):
            services.set_item_status(item["id"], "pending")

        # 取宠码错误 → 403；正确 → 放行；重复核验 → 冲突
        with self.assertRaises(services.PermissionDenied):
            services.verify_pickup(aid, "WRONG1")
        ok = services.verify_pickup(aid, result["pickup_code"])
        self.assertTrue(ok["verified"])
        with self.assertRaises(services.Conflict):
            services.verify_pickup(aid, result["pickup_code"])
        self.assertEqual(services.appointment_detail(aid)["status"], "picked_up")

    def test_confirmed_record_cannot_delete(self):
        _aid, oid = self._order()
        # 护理中（未结单未确认）可删
        services.delete_order(oid)
        with self.assertRaises(services.NotFound):
            services.order_detail(oid)

        # 重建一单：结单 → 未确认不可删（closed），确认后删除被权限拒绝
        aid2 = services.create_appointment(
            self.fx.pets[1], self.fx.staff_plain, self.fx.station_dryer,
            D0, "13:00", self.fx.sid("洗澡"))
        oid2 = services.confirm_reception(aid2)
        for item in services.order_detail(oid2)["items"]:
            services.set_item_status(item["id"], "done")
        services.close_order(oid2)
        with self.assertRaises(services.Conflict):
            services.delete_order(oid2)  # 已结单
        services.confirm_record(oid2)
        with self.assertRaises(services.PermissionDenied):
            services.delete_order(oid2)  # 确认后不能删除
        # 重复确认也被拒绝
        with self.assertRaises(services.Conflict):
            services.confirm_record(oid2)

    def test_reception_idempotent_guard(self):
        aid, _oid = self._order()
        with self.assertRaises(services.Conflict):
            services.confirm_reception(aid)


# ---------------------------------------------------------------------------
# 5. 通知重试（进程内任务）
# ---------------------------------------------------------------------------
class TestNotificationRetry(BaseCase):
    def _add(self, payload, attempts=0, status="pending"):
        conn = connect()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        nid = conn.execute(
            """INSERT INTO notifications (pet_id,kind,channel,payload,status,attempts,
                                          max_attempts,next_run_at)
               VALUES (?, 'reminder','短信',?,?,0,3,?)""",
            (self.fx.pets[0], payload, status, now)).lastrowid
        conn.commit()
        conn.close()
        return nid

    def test_retry_then_success(self):
        old = services.RETRY_BACKOFF
        services.RETRY_BACKOFF = [0, 0, 0]
        try:
            nid = self._add("fail:2:前两次失败第三次成功")
            r1 = services.attempt_send(nid)
            self.assertEqual(r1["status"], "pending")
            self.assertEqual(r1["attempts"], 1)
            r2 = services.attempt_send(nid)
            self.assertEqual(r2["status"], "pending")
            self.assertEqual(r2["attempts"], 2)
            r3 = services.attempt_send(nid)
            self.assertEqual(r3["status"], "sent")
            self.assertEqual(r3["attempts"], 3)
        finally:
            services.RETRY_BACKOFF = old

    def test_retry_exhausted_marks_failed(self):
        nid = self._add("fail:always:始终失败")
        statuses = [services.attempt_send(nid)["status"] for _ in range(3)]
        self.assertEqual(statuses, ["pending", "pending", "failed"])
        # 终态后再调用不再发送
        again = services.attempt_send(nid)
        self.assertEqual(again["status"], "failed")

    def test_backoff_schedule(self):
        old = services.RETRY_BACKOFF
        services.RETRY_BACKOFF = [5, 15, 60]
        try:
            nid = self._add("fail:always:x")
            before = datetime.now()
            r = services.attempt_send(nid)
            nxt = datetime.strptime(r["next_run_at"], "%Y-%m-%d %H:%M:%S")
            # next_run_at 按秒存储，存在亚秒截断，下界放宽到 4 秒
            self.assertGreaterEqual((nxt - before).total_seconds(), 4.0)
        finally:
            services.RETRY_BACKOFF = old

    def test_worker_picks_due_notification(self):
        """进程内后台线程自动到期发送并重试。"""
        old = services.RETRY_BACKOFF
        services.RETRY_BACKOFF = [0, 0, 0]
        worker = NotificationWorker(tick_seconds=0.05)
        nid = self._add("fail:1:后台线程重试演示")
        worker.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                conn = connect()
                row = conn.execute(
                    "SELECT status, attempts FROM notifications WHERE id=?",
                    (nid,)).fetchone()
                conn.close()
                if row["status"] == "sent":
                    break
                time.sleep(0.05)
            conn = connect()
            row = conn.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
            conn.close()
            self.assertEqual(row["status"], "sent")
            self.assertGreaterEqual(row["attempts"], 2)
            self.assertGreaterEqual(worker.sent_count, 1)
        finally:
            worker.stop()
            worker.join(timeout=2)
            services.RETRY_BACKOFF = old


# ---------------------------------------------------------------------------
# 6. 超时未取 & 过敏事件
# ---------------------------------------------------------------------------
class TestOverdueAndAllergy(BaseCase):
    def _completed_yesterday(self):
        aid = services.create_appointment(
            self.fx.pets[0], self.fx.staff_authorized, self.fx.station_a,
            DY, "09:00", self.fx.sid("洗澡", "剪指甲"))
        oid = services.confirm_reception(aid)
        for item in services.order_detail(oid)["items"]:
            services.set_item_status(item["id"], "done")
        services.close_order(oid)
        return aid, oid

    def test_overdue_scan_once(self):
        aid, _oid = self._completed_yesterday()
        created = services.scan_overdue()
        self.assertIn(aid, created)
        # 同一单不重复生成事件
        again = services.scan_overdue()
        self.assertEqual(again, [])
        events = [e for e in services.list_events("overdue") if e["appointment_id"] == aid]
        self.assertEqual(len(events), 1)
        conn = connect()
        notifs = conn.execute(
            "SELECT COUNT(*) c FROM notifications WHERE appointment_id=? AND kind='overdue'",
            (aid,)).fetchone()["c"]
        conn.close()
        self.assertEqual(notifs, 1)

    def test_allergy_event_and_stock(self):
        _aid, oid = self._completed_yesterday()
        conn = connect()
        supply_id = conn.execute(
            "SELECT id, stock FROM supplies WHERE name='燕麦护毛素'").fetchone()["id"]
        before = conn.execute("SELECT stock FROM supplies WHERE id=?", (supply_id,)).fetchone()["stock"]
        conn.close()
        services.record_supply_usage(
            oid, supply_id, qty=1, caused_allergy=True,
            allergy_detail="腹部红斑")
        events = services.list_events("allergy")
        self.assertTrue(any("燕麦护毛素" in e["detail"] for e in events))
        conn = connect()
        after = conn.execute("SELECT stock FROM supplies WHERE id=?", (supply_id,)).fetchone()["stock"]
        flagged = conn.execute(
            "SELECT COUNT(*) c FROM supply_usages WHERE order_id=? AND caused_allergy=1",
            (oid,)).fetchone()["c"]
        conn.close()
        self.assertEqual(after, before - 1)
        self.assertEqual(flagged, 1)


# ---------------------------------------------------------------------------
# 7. 预置数据完整性（seed 冒烟）
# ---------------------------------------------------------------------------
class TestSeedSmoke(BaseCase):
    def test_seed_creates_all_event_types(self):
        summary = seed_mod.seed()
        self.assertGreaterEqual(summary["pets"], 6)
        self.assertGreaterEqual(summary["appointments"], 8)
        types = {e["type"] for e in services.list_events()}
        self.assertEqual(
            types,
            {"resistance", "skin", "cut", "change", "overdue", "allergy"})
        conn = connect()
        sensitive = conn.execute(
            "SELECT COUNT(*) c FROM photos WHERE kind='sensitive'").fetchone()["c"]
        pickups = conn.execute("SELECT COUNT(*) c FROM pickups WHERE verified=1").fetchone()["c"]
        conn.close()
        self.assertGreaterEqual(sensitive, 2)
        self.assertGreaterEqual(pickups, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
