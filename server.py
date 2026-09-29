"""HTTP API + 静态资源服务（Python 标准库，零依赖）。

鉴权约定：前端在 X-Staff-Id 头中携带当前登录员工 id（演示系统）。
敏感照片即使知道文件名，也必须经 /api/photo_file 鉴权后才能取到字节流，
uploads 目录不直接对外开放。
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import services
from db import EVENT_TYPES, UPLOAD_DIR, connect, init_db
from notify import NotificationWorker

log = logging.getLogger("grooming.server")
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_.\-]+$")


class Handler(BaseHTTPRequestHandler):
    server_version = "GroomingDesk/1.0"

    # -- 工具 --------------------------------------------------------------
    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise services.AppError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise services.AppError("请求体必须是 JSON 对象")
        return data

    def _viewer(self, qs: dict) -> int | None:
        raw = self.headers.get("X-Staff-Id") or qs.get("viewer", [""])[0]
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    def _handle_error(self, exc: Exception):
        if isinstance(exc, services.AppError):
            self._send_json({"error": str(exc), "type": exc.__class__.__name__},
                            exc.http_status)
        else:
            log.exception("服务器错误")
            self._send_json({"error": f"服务器错误：{exc}"}, 500)

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    # -- 路由 --------------------------------------------------------------
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path, qs = parsed.path, parse_qs(parsed.query)
            if path.startswith("/api/"):
                self._route_get(path, qs)
            elif path == "/":
                self._serve_static("index.html")
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            else:
                self._send_json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._handle_error(exc)

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self._send_json({"error": "not found"}, 404)
                return
            self._route_post(parsed.path, self._read_json())
        except Exception as exc:  # noqa: BLE001
            self._handle_error(exc)

    def do_DELETE(self):
        try:
            parsed = urlparse(self.path)
            self._route_delete(parsed.path)
        except Exception as exc:  # noqa: BLE001
            self._handle_error(exc)

    # -- GET 路由 ----------------------------------------------------------
    def _route_get(self, path, qs):
        viewer = self._viewer(qs)

        if path == "/api/meta":
            self._send_json({
                "staff": services.list_staff(include_inactive=True),
                "stations": services.list_stations(),
                "services": services.list_services(),
                "supplies": services.list_supplies(),
                "event_types": [
                    {"key": k, "label": l, "icon": i}
                    for k, l, i, _ in EVENT_TYPES
                ],
            })
        elif path == "/api/pets":
            self._send_json(services.list_pets())
        elif m := re.fullmatch(r"/api/pets/(\d+)", path):
            self._send_json(services.pet_detail(int(m.group(1))))
        elif path == "/api/contacts":
            self._send_json(services.list_contacts())
        elif path == "/api/appointments":
            date_from = qs.get("from", [""])[0]
            date_to = qs.get("to", [""])[0]
            self._send_json(services.appointments_between(date_from, date_to))
        elif m := re.fullmatch(r"/api/appointments/(\d+)", path):
            self._send_json(services.appointment_detail(int(m.group(1))))
        elif path == "/api/orders":
            self._send_json(self._list_orders())
        elif m := re.fullmatch(r"/api/orders/(\d+)", path):
            self._send_json(services.order_detail(int(m.group(1)), viewer_staff_id=viewer))
        elif path == "/api/timeline":
            date = qs.get("date", [""])[0]
            self._send_json(services.station_timeline(date))
        elif path == "/api/shifts":
            self._send_json(services.shifts_on(qs.get("date", [""])[0]))
        elif path == "/api/events":
            self._send_json(services.list_events(qs.get("type", [None])[0]))
        elif path == "/api/photos":
            self._send_json(services.list_all_photos(viewer))
        elif m := re.fullmatch(r"/api/photos/(\d+)", path):
            self._send_json(services.get_photo_meta(int(m.group(1)), viewer))
        elif path == "/api/photo_file":
            self._serve_photo_file(qs.get("filename", [""])[0], viewer)
        elif path == "/api/notifications":
            self._send_json(services.list_notifications())
        elif path == "/api/availability":
            self._send_json(services.check_availability(
                int(qs["pet"][0]), int(qs.get("staff", ["0"])[0]) or None,
                int(qs["station"][0]), qs["date"][0], qs["start"][0], qs["end"][0]))
        else:
            self._send_json({"error": "not found"}, 404)

    def _list_orders(self) -> list[dict]:
        conn = connect()
        try:
            return [dict(r) for r in conn.execute(
                """SELECT o.*, p.name pet_name, a.appt_date, a.start_time,
                          s.name staff_name
                   FROM care_orders o
                   JOIN pets p ON p.id=o.pet_id
                   JOIN appointments a ON a.id=o.appointment_id
                   LEFT JOIN staff s ON s.id=o.staff_id
                   ORDER BY (o.status='open') DESC, o.id DESC""")]
        finally:
            conn.close()

    # -- POST 路由 ---------------------------------------------------------
    def _route_post(self, path, body):
        if path == "/api/appointments":
            appt_id = services.create_appointment(
                int(body["pet_id"]), body.get("staff_id"), int(body["station_id"]),
                body["date"], body["start"], [int(x) for x in body["service_ids"]],
                body.get("note", ""), int(body.get("reminder_minutes", 30)))
            self._send_json({"id": appt_id}, 201)

        elif m := re.fullmatch(r"/api/appointments/(\d+)/confirm-reception", path):
            order_id = services.confirm_reception(
                int(m.group(1)), body.get("by", "前台"),
                body.get("skin_note", ""), body.get("coat_note", ""))
            self._send_json({"order_id": order_id}, 201)

        elif m := re.fullmatch(r"/api/appointments/(\d+)/verify-pickup", path):
            result = services.verify_pickup(
                int(m.group(1)), str(body.get("code", "")),
                body.get("verifier", "前台"))
            self._send_json(result)

        elif m := re.fullmatch(r"/api/orders/(\d+)/items/(\d+)/status", path):
            item = services.set_item_status(
                int(m.group(2)), body.get("status", "done"),
                body.get("by", "美容师"), body.get("remark", ""))
            self._send_json(item)

        elif m := re.fullmatch(r"/api/orders/(\d+)/close", path):
            self._send_json(services.close_order(
                int(m.group(1)), body.get("by", "美容师"), body.get("summary", "")))

        elif m := re.fullmatch(r"/api/orders/(\d+)/confirm", path):
            services.confirm_record(int(m.group(1)), body.get("by", "店长"))
            self._send_json({"confirmed": True})

        elif m := re.fullmatch(r"/api/orders/(\d+)/change-services", path):
            order = services.owner_change_services(
                int(m.group(1)), [int(x) for x in body["service_ids"]],
                body.get("by", "前台"), body.get("reason", ""))
            self._send_json(order)

        elif m := re.fullmatch(r"/api/orders/(\d+)/supplies", path):
            usage_id = services.record_supply_usage(
                int(m.group(1)), int(body["supply_id"]), float(body.get("qty", 1)),
                body.get("by", "美容师"), bool(body.get("caused_allergy", False)),
                body.get("allergy_detail", ""))
            self._send_json({"usage_id": usage_id}, 201)

        elif path == "/api/events":
            event_id = services.add_event(
                body["type"], body.get("title", ""), body.get("detail", ""),
                body.get("by", "前台"), body.get("pet_id"),
                body.get("appointment_id"), body.get("order_id"))
            self._send_json({"id": event_id}, 201)

        elif path == "/api/photos":
            filename = body.get("filename", "photo.png")
            data = base64.b64decode(body["content_base64"])
            photo = services.save_photo(
                data, filename, kind=body.get("kind", "normal"),
                pet_id=body.get("pet_id"), appointment_id=body.get("appointment_id"),
                event_id=body.get("event_id"), caption=body.get("caption", ""),
                uploaded_by=body.get("uploaded_by", "前台"))
            self._send_json(photo, 201)

        elif m := re.fullmatch(r"/api/notifications/(\d+)/send", path):
            self._send_json(services.attempt_send(int(m.group(1))))

        else:
            self._send_json({"error": "not found"}, 404)

    def _route_delete(self, path):
        if m := re.fullmatch(r"/api/orders/(\d+)", path):
            services.delete_order(int(m.group(1)))
            self._send_json({"deleted": True})
        else:
            self._send_json({"error": "not found"}, 404)

    # -- 静态文件与照片 -----------------------------------------------------
    def _serve_static(self, rel: str):
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_photo_file(self, filename: str, viewer):
        if not filename or not SAFE_FILENAME.fullmatch(filename):
            raise services.NotFound("照片不存在")
        conn = connect()
        try:
            row = conn.execute("SELECT * FROM photos WHERE filename=?",
                               (filename,)).fetchone()
        finally:
            conn.close()
        if not row:
            raise services.NotFound("照片不存在")
        photo = dict(row)
        if photo["kind"] == "sensitive":
            # 文件级鉴权：非授权人员拿不到任何字节
            if not services.is_authorized_sensitive(viewer):
                raise services.PermissionDenied("异常照片仅限授权人员查看")
        path = UPLOAD_DIR / filename
        if not path.is_file():
            raise services.NotFound("照片文件缺失")
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if filename.endswith(".svg"):
            ctype = "image/svg+xml"
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(body)


def build_server(host: str, port: int) -> ThreadingHTTPServer:
    init_db()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd
