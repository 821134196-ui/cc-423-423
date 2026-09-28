"""照片本地存储。

- 档案照片与异常照片都写入配置中的本地目录，文件名随机化防止遍历；
- 异常照片在数据库中标记 ``sensitivity='restricted'``，
  读取必须经过授权校验（见 :func:`app.operations.get_photo_path`）。
"""
import base64
import binascii
import secrets

from . import config
from .db import connect

_EXT_BY_SIG = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG\r\n\x1a\n": ".png",
}

# 1x1 透明 PNG，用于无档案照时的占位。
PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


def guess_extension(raw: bytes) -> str:
    for sig, ext in _EXT_BY_SIG.items():
        if raw.startswith(sig):
            return ext
    return ".bin"


def save_photo(raw: bytes, kind="pet", sensitivity="normal", caption="",
               uploaded_by="前台", pet_id=None, appointment_id=None,
               event_id=None, conn=None) -> int:
    """落盘并在 photos 表登记，返回照片 id。"""
    ext = guess_extension(raw)
    name = f"{kind}_{secrets.token_hex(8)}{ext}"
    path = config.PHOTO_DIR / name
    path.write_bytes(raw)
    own_conn = conn if conn is not None else connect()
    try:
        cur = own_conn.execute(
            """INSERT INTO photos
               (file_name, kind, sensitivity, pet_id, appointment_id, event_id,
                caption, uploaded_by)
               VALUES (?,?,?,?,?,?,?,?)""",
            (name, kind, sensitivity, pet_id, appointment_id, event_id,
             caption, uploaded_by),
        )
        return cur.lastrowid
    finally:
        if conn is None:
            own_conn.close()


def read_file(file_name: str) -> bytes:
    """只允许读取 PHOTO_DIR 内文件，挡住路径穿越。"""
    photo_root = config.PHOTO_DIR.resolve()
    target = (photo_root / file_name).resolve()
    if photo_root not in target.parents and target != photo_root:
        raise PermissionError("非法照片路径")
    if not target.exists():
        raise FileNotFoundError(file_name)
    return target.read_bytes()


def decode_data_url(data_url: str) -> bytes:
    """解析 ``data:image/png;base64,....``，用于前端直接上传。"""
    if "," not in data_url:
        raise ValueError("照片数据格式错误")
    b64 = data_url.split(",", 1)[1]
    try:
        return base64.b64decode(b64, validate=True)
    except binascii.Error as exc:
        raise ValueError("照片 base64 解码失败") from exc
