"""极简会话鉴权（演示用）。

- 口令以明文存演示库，仅用于本地演示；
- 登录成功发放内存不透明令牌，``manager`` 与被授权的前台可查看异常照片；
- 异常照片接口在服务层再次校验角色，前端隐藏不等于安全。
"""
import secrets
import threading

from .db import connect

# username -> token；进程内有效，重启需重新登录。
_SESSIONS = {}
_LOCK = threading.Lock()

ROLE_LABELS = {
    "front": "前台接待",
    "groomer": "美容师",
    "manager": "店长",
}

DEFAULT_USERS = [
    # 用户名, 显示名, 角色, 可看异常照片, 口令
    ("admin", "林店长", "manager", 1, "admin123"),
    ("qian", "小千", "front", 1, "qian123"),
    ("mei", "阿梅", "groomer", 0, "mei123"),
]


def ensure_users(conn):
    for username, name, role, can_view, pwd in DEFAULT_USERS:
        conn.execute(
            """INSERT OR IGNORE INTO auth_users
               (username, display_name, role, can_view_incident, password)
               VALUES (?,?,?,?,?)""",
            (username, name, role, can_view, pwd),
        )


def login(conn, username, password):
    row = conn.execute(
        "SELECT * FROM auth_users WHERE username=? AND password=?",
        (username, password),
    ).fetchone()
    if not row:
        return None
    token = secrets.token_urlsafe(24)
    with _LOCK:
        _SESSIONS[token] = {
            "username": row["username"],
            "display_name": row["display_name"],
            "role": row["role"],
            "can_view_incident": bool(row["can_view_incident"]),
        }
    return token


def logout(token):
    with _LOCK:
        _SESSIONS.pop(token, None)


def current_user(token):
    if not token:
        return None
    with _LOCK:
        return _SESSIONS.get(token)


def can_view_incident(token):
    user = current_user(token)
    return bool(user and user["can_view_incident"])
