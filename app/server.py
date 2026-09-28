"""零依赖 HTTP 服务：静态页面 + JSON API。

- ``ThreadingHTTPServer`` 承载，每请求独立 SQLite 连接；
- 写事务全部走 ``BEGIN IMMEDIATE``（见 app/operations.py），
  SQLite 文件写锁天然串行化并发预约；
- 异常照片内容接口在业务层校验授权，未登录或越权一律拒绝。
"""
import json
import re
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlparse

from . import auth, config, operations, photos as photos_mod, seed
from .db import connect
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATIC_DIR = Path(__file__).resolve().parent / "static"

# (方法, 正则, 处理函数名, 是否需要登录)
ROUTES = [
    ("POST", r"^/api/login$", "api_login", False),
    ("POST", r"^/api/logout$", "api_logout", True),
    ("GET", r"^/api/me$", "api_me", True),

    ("GET", r"^/api/pets$", "api_pets", True),
    ("GET", r"^/api/pets/(\d+)$", "api_pet_detail", True),
    ("GET", r"^/api/services$", "api_services", True),
    ("GET", r"^/api/groomers$", "api_groomers", True),

    ("GET", r"^/api/appointments$", "api_appointments", True),
    ("POST", r"^/api/appointments$", "api_appointment_create", True),
    ("POST", r"^/api/appointments/(\d+)/cancel$", "api_appointment_cancel", True),
    ("POST", r"^/api/appointments/(\d+)/change-services$", "api_change_services", True),
    ("POST", r"^/api/appointments/(\d+)/reception$", "api_reception", True),
    ("POST", r"^/api/appointments/(\d+)/events$", "api_add_event", True),

    ("GET", r"^/api/care/(\d+)$", "api_care_get", True),
    ("POST", r"^/api/care/(\d+)/items/(\d+)/complete$", "api_care_complete", True),
    ("POST", r"^/api/care/(\d+)/items/(\d+)/undo$", "api_care_undo", True),
    ("POST", r"^/api/care/(\d+)/close$", "api_care_close", True),
    ("DELETE", r"^/api/care/(\d+)$", "api_care_delete", True),
    ("POST", r"^/api/care/(\d+)/supplies$", "api_supply", True),

    ("GET", r"^/api/events$", "api_events", True),
    ("POST", r"^/api/events/(\d+)/photos$", "api_event_photo", True),

    ("GET", r"^/api/stations$", "api_stations", True),
    ("GET", r"^/api/pickup$", "api_pickup", True),
    ("POST", r"^/api/pickup/(\d+)/verify$", "api_pickup_verify", True),

    ("GET", r"^/api/photos/incidents$", "api_incident_photos", True),
    ("GET", r"^/api/photos/(\d+)$", "api_photo_content", True),
    ("GET", r"^/api/notifications$", "api_notifications", True),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "PawCare/1.0"

    # -- 基础收发 ----------------------------------------------------------
    def _send_json(self, payload, status=200, token=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if token:
            self.send_header(
                "Set-Cookie",
                f"paw_token={token}; HttpOnly; Path=/; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise operations.AppError("请求体不是合法 JSON", 400)

    def _token(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        if "paw_token" in cookie:
            return cookie["paw_token"].value
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:]
        return self.headers.get("X-Auth-Token")

    @property
    def user(self):
        return auth.current_user(self._token())

    def _require_login(self):
        user = self.user
        if not user:
            raise operations.AppError("请先登录", 401)
        return user

    def log_message(self, fmt, *args):  # 静默默认访问日志，保持演示终端干净
        pass

    # -- 路由 --------------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/"):
                for m, pattern, handler_name, need_login in ROUTES:
                    if m != method:
                        continue
                    match = re.match(pattern, path)
                    if match:
                        if need_login:
                            self._require_login()
                        return getattr(self, handler_name)(*match.groups())
                raise operations.AppError("接口不存在", 404)
            return self._serve_static(path)
        except operations.AppError as exc:
            self._send_json({"error": str(exc)}, status=exc.status)
        except Exception as exc:  # noqa: BLE001 - 演示系统兜底为 500 JSON
            self._send_json({"error": f"服务器错误：{exc}"}, status=500)

    def _serve_static(self, path):
        if path == "/":
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        if STATIC_DIR not in target.parents or not target.is_file():
            self.send_error(404, "Not found")
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- 鉴权 --------------------------------------------------------------
    def api_login(self):
        data = self._read_json()
        conn = connect()
        try:
            token = auth.login(conn, (data.get("username") or "").strip(),
                               data.get("password") or "")
        finally:
            conn.close()
        if not token:
            raise operations.AppError("用户名或口令错误", 401)
        user = auth.current_user(token)
        self._send_json({"token": token, "user": user}, token=token)

    def api_logout(self):
        auth.logout(self._token())
        self._send_json({"ok": True})

    def api_me(self):
        self._send_json({"user": self._require_login()})

    # -- 档案/基础数据 -----------------------------------------------------
    def api_pets(self):
        conn = connect()
        try:
            self._send_json({"pets": operations.list_pets(conn)})
        finally:
            conn.close()

    def api_pet_detail(self, pet_id):
        conn = connect()
        try:
            self._send_json({"pet": operations.get_pet(conn, int(pet_id))})
        finally:
            conn.close()

    def api_services(self):
        conn = connect()
        try:
            self._send_json({"services": operations.list_services(conn)})
        finally:
            conn.close()

    def api_groomers(self):
        conn = connect()
        try:
            self._send_json({"groomers": operations.list_groomers(conn)})
        finally:
            conn.close()

    # -- 预约 --------------------------------------------------------------
    def api_appointments(self):
        from datetime import date
        from urllib.parse import parse_qs
        params = parse_qs(urlparse(self.path).query)
        day = (params.get("date") or [date.today().isoformat()])[0]
        conn = connect()
        try:
            items = operations.appointments_between(
                conn, f"{day} 00:00", f"{day} 23:59")
            self._send_json({"date": day, "appointments": items})
        finally:
            conn.close()

    def api_appointment_create(self):
        user = self._require_login()
        data = self._read_json()
        conn = connect()
        try:
            appt_id = operations.create_appointment(
                conn, int(data["pet_id"]), int(data["groomer_id"]),
                data["start_time"], data.get("service_ids", []),
                data.get("note", ""))
            self._send_json({"id": appt_id}, status=201)
        finally:
            conn.close()

    def api_appointment_cancel(self, appt_id):
        data = self._read_json()
        conn = connect()
        try:
            operations.cancel_appointment(conn, int(appt_id), data.get("reason", ""))
            self._send_json({"ok": True})
        finally:
            conn.close()

    def api_change_services(self, appt_id):
        user = self._require_login()
        data = self._read_json()
        conn = connect()
        try:
            operations.change_services(
                conn, int(appt_id), data.get("service_ids", []),
                actor=user["display_name"], reason=data.get("reason", ""))
            self._send_json({"ok": True})
        finally:
            conn.close()

    def api_reception(self, appt_id):
        user = self._require_login()
        conn = connect()
        try:
            care_id = operations.confirm_reception(conn, int(appt_id), actor=user["display_name"])
            self._send_json({"care_record_id": care_id})
        finally:
            conn.close()

    def api_add_event(self, appt_id):
        user = self._require_login()
        data = self._read_json()
        conn = connect()
        try:
            event_id = operations.add_event(
                conn, int(appt_id), data.get("kind"), data.get("detail", ""),
                actor=user["display_name"])
            if data.get("photo"):
                raw = photos_mod.decode_data_url(data["photo"])
                operations.attach_incident_photo(
                    conn, event_id, raw, data.get("caption", ""),
                    authorized=user["can_view_incident"], actor=user["display_name"])
            self._send_json({"id": event_id}, status=201)
        finally:
            conn.close()

    # -- 护理 --------------------------------------------------------------
    def api_care_get(self, appt_id):
        conn = connect()
        try:
            self._send_json(operations.get_care_record(conn, int(appt_id)))
        finally:
            conn.close()

    def api_care_complete(self, care_id, item_id):
        user = self._require_login()
        conn = connect()
        try:
            operations.complete_care_item(conn, int(care_id), int(item_id),
                                          actor=user["display_name"])
            self._send_json({"ok": True})
        finally:
            conn.close()

    def api_care_undo(self, care_id, item_id):
        user = self._require_login()
        conn = connect()
        try:
            operations.undo_care_item(conn, int(care_id), int(item_id),
                                      actor=user["display_name"])
            self._send_json({"ok": True})
        finally:
            conn.close()

    def api_care_close(self, care_id):
        user = self._require_login()
        conn = connect()
        try:
            result = operations.close_care_record(conn, int(care_id), actor=user["display_name"])
            self._send_json({"ok": True, **result})
        finally:
            conn.close()

    def api_care_delete(self, care_id):
        self._require_login()
        conn = connect()
        try:
            operations.delete_care_record(conn, int(care_id))
            self._send_json({"ok": True})
        finally:
            conn.close()

    def api_supply(self, care_id):
        user = self._require_login()
        data = self._read_json()
        conn = connect()
        try:
            operations.use_supply(
                conn, int(care_id), data.get("supply_name", "用品"),
                float(data.get("quantity", 1)), data.get("unit", "份"),
                allergy=bool(data.get("allergy")), actor=user["display_name"],
                reaction=data.get("reaction", ""))
            self._send_json({"ok": True}, status=201)
        finally:
            conn.close()

    # -- 事件与异常照片 -----------------------------------------------------
    def api_events(self):
        from urllib.parse import parse_qs
        kind = parse_qs(urlparse(self.path).query).get("kind", [None])[0]
        conn = connect()
        try:
            self._send_json({"events": operations.list_events(conn, kind=kind)})
        finally:
            conn.close()

    def api_event_photo(self, event_id):
        user = self._require_login()
        data = self._read_json()
        raw = photos_mod.decode_data_url(data["photo"])
        conn = connect()
        try:
            photo_id = operations.attach_incident_photo(
                conn, int(event_id), raw, data.get("caption", ""),
                authorized=user["can_view_incident"], actor=user["display_name"])
            self._send_json({"id": photo_id}, status=201)
        finally:
            conn.close()

    def api_incident_photos(self):
        conn = connect()
        try:
            self._send_json({"photos": operations.list_incident_photos(conn)})
        finally:
            conn.close()

    def api_photo_content(self, photo_id):
        user = self._require_login()
        conn = connect()
        try:
            target, meta = operations.get_photo_path(
                conn, int(photo_id), authorized=user["can_view_incident"])
        finally:
            conn.close()
        body = target.read_bytes()
        ctype = "image/png" if target.suffix.lower() == ".png" else "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- 工位 / 取宠 / 通知 -------------------------------------------------
    def api_stations(self):
        from datetime import date
        from urllib.parse import parse_qs
        params = parse_qs(urlparse(self.path).query)
        day = (params.get("date") or [date.today().isoformat()])[0]
        conn = connect()
        try:
            self._send_json({"date": day, "timeline": operations.station_schedule(conn, day)})
        finally:
            conn.close()

    def api_pickup(self):
        conn = connect()
        try:
            self._send_json({"queue": operations.pickup_queue(conn)})
        finally:
            conn.close()

    def api_pickup_verify(self, appt_id):
        user = self._require_login()
        data = self._read_json()
        conn = connect()
        try:
            result = operations.verify_pickup(
                conn, int(appt_id), data.get("code", ""), actor=user["display_name"])
            self._send_json({"ok": True, **result})
        finally:
            conn.close()

    def api_notifications(self):
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT * FROM notifications ORDER BY id DESC LIMIT 30").fetchall()
            self._send_json({"notifications": [dict(r) for r in rows]})
        finally:
            conn.close()


def build_server(host="127.0.0.1", port=8000):
    config.ensure_dirs()
    return ThreadingHTTPServer((host, port), Handler)
