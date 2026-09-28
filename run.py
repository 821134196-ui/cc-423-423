#!/usr/bin/env python3
"""单条本地启动命令：

    python3 run.py

选项：
  --reset            删除旧库并重新预置演示数据
  --port 8000        监听端口（默认 8000）
  --host 127.0.0.1   监听地址
  --no-scheduler     不启动进程内通知重试任务

首次启动自动：建表 -> 写入宠物/联系人演示数据 -> 预置预约、护理与异常样例。
"""
import argparse
import sys
import webbrowser

from app import auth, config
from app.db import connect, init_db
from app.notifier import NotificationScheduler
from app.seed import seed
from app.server import build_server


def prepare(reset=False):
    config.ensure_dirs()
    if reset and config.DB_PATH.exists():
        for suffix in ("", "-wal", "-shm"):
            p = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
            if p.exists():
                p.unlink()
    init_db()
    conn = connect()
    try:
        auth.ensure_users(conn)
    finally:
        conn.close()
    created = seed(force=reset)
    return created


def main(argv=None):
    parser = argparse.ArgumentParser(description="宠物美容预约与护理记录系统")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reset", action="store_true", help="删库重建并重新预置样例")
    parser.add_argument("--no-scheduler", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    seeded = prepare(reset=args.reset)
    httpd = build_server(args.host, args.port)

    scheduler = None
    if not args.no_scheduler:
        scheduler = NotificationScheduler()
        scheduler.start()

    url = f"http://{args.host}:{args.port}/"
    print("=" * 62)
    print(" 宠物美容预约与护理记录系统已启动")
    print(f" 地址：{url}")
    print(" 演示账号：admin/admin123（店长·可看异常照片）")
    print("           qian /qian123（前台·可看异常照片）")
    print("           mei  /mei123 （美容师·无异常照片权限）")
    if seeded:
        print(" 已预置：今日预约、进行中护理单、异常事件照片、超时未取与重试通知样例")
    print("=" * 62)

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        if scheduler:
            scheduler.stop()
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main())
