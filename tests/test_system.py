"""系统测试：时段竞争、服务变更、照片权限、重复结单、通知重试。

直接运行：

    python3 -m unittest -v   （或 python3 -m pytest，若已安装）

测试在临时目录中建库并预置演示数据；HTTP 用例在真实端口上起服务，
用标准库 urllib + Cookie 走完整登录/鉴权链路。
"""
import base64
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.cookiejar import CookieJar
from urllib.parse import urlencode

# 必须在 import app 之前指定临时数据目录。
_TMP = tempfile.mkdtemp(prefix="grooming-test-")
os.environ["GROOMING_DATA_DIR"] = _TMP
os.environ["GROOMING_DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["GROOMING_PHOTO_DIR"] = os.path.join(_TMP, "photos")
os.environ["GROOMING_NOTIFY_INTERVAL"] = "1"

from app import auth, config, operations, photos as photos_mod, seed  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.notifier import NotificationScheduler  # noqa: E402
from app.server import build_server  # noqa: E402

init_db()
seed.seed(force=True)


def ids(conn, table, col="name"):
    return {r[col]: r["id"] for r in conn.execute(f"SELECT id,{col} FROM {table}")}


class OpsTestBase(unittest.TestCase):
    def setUp(self):
        self.conn = connect()

    def tearDown(self):
        self.conn.close()

    def fresh_slot(self, hour=16, minute=30, capacity=2):
        t = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        slot = t.strftime("%Y-%m-%d %H:%M")
        operations.set_slot_capacity(self.conn, slot, capacity)
        return slot

    def make_appt(self, pet, groomer, slot, services=("基础洗浴",)):
        sv = ids(self.conn, "services")
        return operations.create_appointment(
            self.conn, pet, groomer, slot, [sv[s] for s in services])


# ---------------------------------------------------------------------------
# 1) 时段竞争：同宠重复预约 / 美容师冲突 / 工位容量，多线程并发抢锁
# ---------------------------------------------------------------------------
class SlotContentionTests(OpsTestBase):
    def test_concurrent_station_capacity(self):
        """容量 2 的时段，3 个并发不同宠物+不同美容师，恰好 2 单成功。"""
        slot = self.fresh_slot(capacity=2)
        pets = list(ids(self.conn, "pets").values())[:3]
        groomers = list(ids(self.conn, "groomers").values())[:3]

        barrier = threading.Barrier(len(pets))
        results, errors = [], []
        lock = threading.Lock()

        def worker(pet_id, groomer_id):
            conn = connect()
            try:
                barrier.wait()
                appt_id = operations.create_appointment(
                    conn, pet_id, groomer_id, slot,
                    [list(ids(conn, "services").values())[0]])
                with lock:
                    results.append(appt_id)
            except operations.AppError as e:
                with lock:
                    errors.append(e.status)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(p, g))
                   for p, g in zip(pets, groomers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 2, f"应有 2 单成功，实际 {len(results)}")
        self.assertEqual(errors, [409], "落败的一单必须是 409 容量冲突")
        locked = self.conn.execute(
            "SELECT COUNT(*) n FROM appointments WHERE start_time=? AND status!='canceled'",
            (slot,)).fetchone()["n"]
        self.assertEqual(locked, 2)

    def test_concurrent_same_pet_same_slot(self):
        """同一宠物同一时段并发预约两次：唯一索引 + 前置校验，最多 1 单。"""
        slot = self.fresh_slot(hour=17, minute=10, capacity=5)
        pet = list(ids(self.conn, "pets").values())[0]
        groomers = list(ids(self.conn, "groomers").values())[:2]
        barrier = threading.Barrier(2)
        wins, statuses = [], []
        lock = threading.Lock()

        def worker(g):
            conn = connect()
            try:
                barrier.wait()
                aid = operations.create_appointment(
                    conn, pet, g, slot,
                    [list(ids(conn, "services").values())[0]])
                with lock:
                    wins.append(aid)
            except operations.AppError as e:
                with lock:
                    statuses.append(e.status)
            finally:
                conn.close()

        ts = [threading.Thread(target=worker, args=(g,)) for g in groomers]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(wins), 1)
        self.assertIn(409, statuses)

    def test_groomer_double_booking(self):
        """同一美容师同一时段不能排两只。"""
        slot = self.fresh_slot(hour=17, minute=40, capacity=5)
        pets = list(ids(self.conn, "pets").values())
        g = list(ids(self.conn, "groomers").values())[0]
        self.make_appt(pets[0], g, slot)
        with self.assertRaises(operations.AppError) as ctx:
            self.make_appt(pets[1], g, slot)
        self.assertEqual(ctx.exception.status, 409)

    def test_appointment_snapshots_and_duration(self):
        """预约保存品种/体型/禁忌快照，预计时长为服务组合之和。"""
        slot = self.fresh_slot(hour=8, minute=0, capacity=5)
        pet_id = list(ids(self.conn, "pets").values())[0]
        g_id = list(ids(self.conn, "groomers").values())[1]
        sv = ids(self.conn, "services")
        aid = operations.create_appointment(
            self.conn, pet_id, g_id, slot,
            [sv["基础洗浴"], sv["耳道护理"]])
        row = self.conn.execute("SELECT * FROM appointments WHERE id=?", (aid,)).fetchone()
        pet = self.conn.execute("SELECT * FROM pets WHERE id=?", (pet_id,)).fetchone()
        self.assertEqual(row["breed_snapshot"], pet["breed"])
        self.assertEqual(row["size_snapshot"], pet["size"])
        self.assertEqual(row["contraindication_snapshot"], pet["contraindications"])
        self.assertEqual(row["estimated_minutes"], 40 + 20)


# ---------------------------------------------------------------------------
# 2) 服务变更（主人临时改项）
# ---------------------------------------------------------------------------
class ServiceChangeTests(OpsTestBase):
    def _appt(self, minute=0):
        slot = self.fresh_slot(hour=15, minute=minute, capacity=5)
        pet_id = list(ids(self.conn, "pets").values())[1]
        g_id = list(ids(self.conn, "groomers").values())[0]
        sv = ids(self.conn, "services")
        aid = operations.create_appointment(
            self.conn, pet_id, g_id, slot, [sv["基础洗浴"], sv["耳道护理"]])
        return aid, sv

    def test_change_before_reception(self):
        aid, sv = self._appt(minute=1)
        operations.change_services(self.conn, aid,
                                   [sv["基础洗浴"], sv["牙齿清洁"]], reason="主人想加洁牙")
        row = self.conn.execute(
            "SELECT estimated_minutes FROM appointments WHERE id=?", (aid,)).fetchone()
        self.assertEqual(row["estimated_minutes"], 40 + 25)
        kinds = [r["kind"] for r in self.conn.execute(
            "SELECT kind FROM events WHERE appointment_id=?", (aid,))]
        self.assertIn("owner_change", kinds)

    def test_change_after_reception_keeps_done_items(self):
        aid, sv = self._appt(minute=2)
        care_id = operations.confirm_reception(self.conn, aid)
        items = self.conn.execute(
            "SELECT ci.id, s.name FROM care_items ci JOIN services s ON s.id=ci.service_id "
            "WHERE ci.care_record_id=? ORDER BY ci.id", (care_id,)).fetchall()
        first = next(i for i in items if i["name"] == "基础洗浴")
        operations.complete_care_item(self.conn, care_id, first["id"])

        # 想删除已完成的「基础洗浴」-> 拒绝
        with self.assertRaises(operations.AppError) as ctx:
            operations.change_services(self.conn, aid, [sv["耳道护理"], sv["牙齿清洁"]])
        self.assertIn("已完成", str(ctx.exception))

        # 新增项目 -> 护理单自动多出一条 pending
        operations.change_services(self.conn, aid,
                                   [sv["基础洗浴"], sv["耳道护理"], sv["牙齿清洁"]])
        names = {r["name_snapshot"]: r["status"] for r in self.conn.execute(
            "SELECT * FROM care_items WHERE care_record_id=?", (care_id,))}
        self.assertEqual(names["基础洗浴"], "done")
        self.assertEqual(names["牙齿清洁"], "pending")
        self.assertEqual(names["耳道护理"], "pending")

    def test_change_after_close_forbidden(self):
        aid, sv = self._appt(minute=3)
        care_id = operations.confirm_reception(self.conn, aid)
        for r in self.conn.execute("SELECT id FROM care_items WHERE care_record_id=?", (care_id,)):
            operations.complete_care_item(self.conn, care_id, r["id"])
        operations.close_care_record(self.conn, care_id)
        with self.assertRaises(operations.AppError):
            operations.change_services(self.conn, aid, [sv["基础洗浴"]])


# ---------------------------------------------------------------------------
# 3) + 4) 护理单状态机：未完成不能结单 / 不能重复结单 / 确认后不能删除
# ---------------------------------------------------------------------------
class CareLifecycleTests(OpsTestBase):
    def _open_care(self, hour=14, minute=5):
        slot = self.fresh_slot(hour=hour, minute=minute, capacity=5)
        pet_id = list(ids(self.conn, "pets").values())[2]
        g_id = list(ids(self.conn, "groomers").values())[2]
        sv = ids(self.conn, "services")
        aid = operations.create_appointment(
            self.conn, pet_id, g_id, slot,
            [sv["猫咪舒缓SPA"], sv["肛门腺护理"]])
        care_id = operations.confirm_reception(self.conn, aid)
        return aid, care_id

    def test_cannot_close_with_pending_items(self):
        _, care_id = self._open_care()
        with self.assertRaises(operations.AppError) as ctx:
            operations.close_care_record(self.conn, care_id)
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("未完成", str(ctx.exception))

    def test_close_then_reject_duplicate_and_delete(self):
        aid, care_id = self._open_care(hour=14, minute=6)
        for r in self.conn.execute("SELECT id FROM care_items WHERE care_record_id=?", (care_id,)):
            operations.complete_care_item(self.conn, care_id, r["id"])
        result = operations.close_care_record(self.conn, care_id)
        self.assertRegex(result["pickup_code"], r"^\d{6}$")

        # 重复结单
        with self.assertRaises(operations.AppError) as ctx:
            operations.close_care_record(self.conn, care_id)
        self.assertEqual(ctx.exception.status, 409)

        # 确认后不能删除
        with self.assertRaises(operations.AppError) as ctx:
            operations.delete_care_record(self.conn, care_id)
        self.assertEqual(ctx.exception.status, 403)

        # 预约已进入待取宠，且入队了取宠通知
        status = self.conn.execute("SELECT status FROM appointments WHERE id=?", (aid,)).fetchone()["status"]
        self.assertEqual(status, "done")
        n = self.conn.execute(
            "SELECT COUNT(*) n FROM notifications WHERE appointment_id=? AND status='pending'",
            (aid,)).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_unconfirmed_care_can_be_deleted(self):
        aid, care_id = self._open_care(hour=14, minute=25)
        operations.delete_care_record(self.conn, care_id)
        self.assertIsNone(self.conn.execute(
            "SELECT id FROM care_records WHERE id=?", (care_id,)).fetchone())
        self.assertEqual(self.conn.execute(
            "SELECT status FROM appointments WHERE id=?", (aid,)).fetchone()["status"], "booked")

    def test_pickup_verification(self):
        aid, care_id = self._open_care(hour=13, minute=35)
        for r in self.conn.execute("SELECT id FROM care_items WHERE care_record_id=?", (care_id,)):
            operations.complete_care_item(self.conn, care_id, r["id"])
        result = operations.close_care_record(self.conn, care_id)

        with self.assertRaises(operations.AppError) as ctx:
            operations.verify_pickup(self.conn, aid, "000000")
        self.assertEqual(ctx.exception.status, 403)

        operations.verify_pickup(self.conn, aid, result["pickup_code"])
        # 重复核验
        with self.assertRaises(operations.AppError) as ctx:
            operations.verify_pickup(self.conn, aid, result["pickup_code"])
        self.assertEqual(ctx.exception.status, 409)


# ---------------------------------------------------------------------------
# 5) 通知重试：指数退避，超过上限置 failed；成功置 sent
# ---------------------------------------------------------------------------
class NotificationRetryTests(OpsTestBase):
    def test_retry_then_give_up(self):
        # 退避间隔调为 2 秒，保证「提前 1 秒」严格早于下次运行（秒级精度存储）。
        old_interval = config.NOTIFY_INTERVAL_SECONDS
        config.NOTIFY_INTERVAL_SECONDS = 2
        try:
            at = datetime(2026, 9, 28, 10, 0, 0)
            nid = operations.enqueue_notification(
                self.conn, None, "注定失败的提醒", channel="fail-sms", run_at=at)
            for attempt in range(1, config.NOTIFY_MAX_ATTEMPTS):
                processed = operations.process_notifications(
                    self.conn, at=at, sender=operations.simulated_sender)
                self.assertIn(nid, processed)
                row = self.conn.execute(
                    "SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
                self.assertEqual(row["status"], "pending")
                self.assertEqual(row["attempts"], attempt)

                # 指数退避：下一次必须等到 next_run_at 才会再被 picked up。
                delay = config.NOTIFY_INTERVAL_SECONDS * (
                    config.NOTIFY_BACKOFF_BASE ** (attempt - 1))
                too_early = at + timedelta(seconds=delay - 1)
                self.assertEqual(
                    operations.process_notifications(
                        self.conn, at=too_early, sender=operations.simulated_sender),
                    [], f"第 {attempt} 次重试未遵守退避（delay={delay}s）")
                at = at + timedelta(seconds=delay)

            operations.process_notifications(
                self.conn, at=at, sender=operations.simulated_sender)
            row = self.conn.execute(
                "SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["attempts"], config.NOTIFY_MAX_ATTEMPTS)
        finally:
            config.NOTIFY_INTERVAL_SECONDS = old_interval

    def test_success_on_first_try(self):
        nid = operations.enqueue_notification(self.conn, None, "正常短信")
        operations.process_notifications(self.conn)
        row = self.conn.execute("SELECT status, attempts FROM notifications WHERE id=?",
                                (nid,)).fetchone()
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["attempts"], 1)

    def test_scheduler_thread_retries(self):
        """进程内后台任务真实跑一轮，把成功通知投递出去。"""
        operations.enqueue_notification(self.conn, None, "调度线程用例")
        sched = NotificationScheduler(interval=0.05)
        sched.start()
        try:
            deadline = datetime.now() + timedelta(seconds=5)
            while datetime.now() < deadline:
                row = self.conn.execute(
                    "SELECT status FROM notifications WHERE payload='调度线程用例'").fetchone()
                if row and row["status"] == "sent":
                    break
                threading.Event().wait(0.1)
            self.assertEqual(self.conn.execute(
                "SELECT status FROM notifications WHERE payload='调度线程用例'").fetchone()["status"],
                "sent")
        finally:
            sched.stop()

    def test_overdue_sweep_idempotent(self):
        slot = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
        operations.set_slot_capacity(self.conn, slot, 5)
        pet_id = list(ids(self.conn, "pets").values())[3]
        g_id = list(ids(self.conn, "groomers").values())[0]
        sv = list(ids(self.conn, "services").values())[:1]
        aid = operations.create_appointment(self.conn, pet_id, g_id, slot, sv)
        care_id = operations.confirm_reception(self.conn, aid)
        for r in self.conn.execute("SELECT id FROM care_items WHERE care_record_id=?", (care_id,)):
            operations.complete_care_item(self.conn, care_id, r["id"])
        operations.close_care_record(self.conn, care_id)
        self.conn.execute(
            "UPDATE appointments SET end_time=? WHERE id=?",
            ((datetime.now() - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M"), aid))

        n1 = operations.sweep_overdue(self.conn)
        n2 = operations.sweep_overdue(self.conn)
        self.assertGreaterEqual(n1, 1, "新建的超时未取单应生成事件")
        self.assertEqual(n2, 0, "超时事件不得重复生成")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM events WHERE appointment_id=? AND kind='overdue_pickup'",
            (aid,)).fetchone()["n"], 1)


# ---------------------------------------------------------------------------
# 事件与用品过敏
# ---------------------------------------------------------------------------
class EventTests(OpsTestBase):
    def test_supply_allergy_creates_event(self):
        slot = self.fresh_slot(hour=9, minute=20, capacity=5)
        pet_id = list(ids(self.conn, "pets").values())[0]
        g_id = list(ids(self.conn, "groomers").values())[1]
        sv = list(ids(self.conn, "services").values())[:1]
        aid = operations.create_appointment(self.conn, pet_id, g_id, slot, sv)
        care_id = operations.confirm_reception(self.conn, aid)
        operations.use_supply(self.conn, care_id, "香精护毛素", allergy=True,
                              reaction="皮肤发红")
        kinds = [r["kind"] for r in self.conn.execute(
            "SELECT kind FROM events WHERE care_record_id=?", (care_id,))]
        self.assertIn("supply_allergy", kinds)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) n FROM supply_usages WHERE care_record_id=?",
            (care_id,)).fetchone()["n"], 1)


# ---------------------------------------------------------------------------
# 照片权限：走完整 HTTP 链路（未登录 401 / 美容师 403 / 店长 200）
# ---------------------------------------------------------------------------
class PhotoPermissionHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = build_server("127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def client(self):
        jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        return opener

    def req(self, opener, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def login(self, opener, username, password):
        status, _ = self.req(opener, "POST", "/api/login",
                             {"username": username, "password": password})
        self.assertEqual(status, 200)

    def test_photo_permissions_end_to_end(self):
        anon = self.client()
        manager = self.client()
        groomer = self.client()
        self.login(manager, "admin", "admin123")
        self.login(groomer, "mei", "mei123")

        # 未登录拿元数据都不行
        status, _ = self.req(anon, "GET", "/api/photos/incidents")
        self.assertEqual(status, 401)

        # 元数据：所有登录用户可列
        status, raw = self.req(manager, "GET", "/api/photos/incidents")
        self.assertEqual(status, 200)
        photos = json.loads(raw)["photos"]
        self.assertTrue(photos, "种子数据应包含异常照片")
        photo_id = photos[0]["id"]

        # 美容师读取受限照片内容 -> 403
        status, _ = self.req(groomer, "GET", f"/api/photos/{photo_id}")
        self.assertEqual(status, 403)

        # 店长 -> 200 且真的是 PNG
        status, raw = self.req(manager, "GET", f"/api/photos/{photo_id}")
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"\x89PNG"))

        # 美容师上传异常照片 -> 403；店长 -> 201
        appts = json.loads(self.req(groomer, "GET", "/api/appointments")[1])["appointments"]
        appt_id = next(a["id"] for a in appts if a["status"] == "booked")
        _, raw = self.req(manager, "POST", f"/api/appointments/{appt_id}/events",
                          {"kind": "skin", "detail": "权限链路测试事件"})
        event_id = json.loads(raw)["id"]
        png = base64.b64encode(seed._solid_png((200, 100, 100))).decode()
        data_url = "data:image/png;base64," + png

        status, _ = self.req(groomer, "POST", f"/api/events/{event_id}/photos",
                             {"photo": data_url, "caption": "越权上传"})
        self.assertEqual(status, 403)

        status, raw = self.req(manager, "POST", f"/api/events/{event_id}/photos",
                               {"photo": data_url, "caption": "合规上传"})
        self.assertEqual(status, 201)
        new_id = json.loads(raw)["id"]
        status, _ = self.req(groomer, "GET", f"/api/photos/{new_id}")
        self.assertEqual(status, 403)
        status, _ = self.req(manager, "GET", f"/api/photos/{new_id}")
        self.assertEqual(status, 200)

    def test_full_flow_over_http(self):
        """HTTP 层走一遍：登录 -> 预约 -> 接待 -> 逐项完成 -> 结单 -> 重复结单被拒。"""
        front = self.client()
        self.login(front, "qian", "qian123")

        _, raw = self.req(front, "GET", "/api/pets")
        pets = json.loads(raw)["pets"]
        _, raw = self.req(front, "GET", "/api/groomers")
        groomers = json.loads(raw)["groomers"]
        _, raw = self.req(front, "GET", "/api/services")
        services = json.loads(raw)["services"]

        slot = datetime.now().replace(hour=12, minute=45, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
        c = connect()
        try:
            operations.set_slot_capacity(c, slot, 5)
        finally:
            c.close()

        status, raw = self.req(front, "POST", "/api/appointments", {
            "pet_id": pets[0]["id"], "groomer_id": groomers[0]["id"],
            "start_time": slot, "service_ids": [services[0]["id"]],
        })
        self.assertEqual(status, 201, raw)
        appt_id = json.loads(raw)["id"]

        status, raw = self.req(front, "POST", f"/api/appointments/{appt_id}/reception")
        self.assertEqual(status, 200)
        care_id = json.loads(raw)["care_record_id"]

        _, raw = self.req(front, "GET", f"/api/care/{appt_id}")
        items = json.loads(raw)["items"]

        # 未完成先结单 -> 409
        status, _ = self.req(front, "POST", f"/api/care/{care_id}/close")
        self.assertEqual(status, 409)

        for it in items:
            status, _ = self.req(front, "POST",
                                 f"/api/care/{care_id}/items/{it['id']}/complete")
            self.assertEqual(status, 200)

        status, raw = self.req(front, "POST", f"/api/care/{care_id}/close")
        self.assertEqual(status, 200)
        code = json.loads(raw)["pickup_code"]

        # 重复结单 -> 409
        status, _ = self.req(front, "POST", f"/api/care/{care_id}/close")
        self.assertEqual(status, 409)

        # 取宠核验
        status, _ = self.req(front, "POST", f"/api/pickup/{appt_id}/verify",
                             {"code": "wrong"})
        self.assertEqual(status, 403)
        status, _ = self.req(front, "POST", f"/api/pickup/{appt_id}/verify",
                             {"code": code})
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
