"""单条本地启动命令：

    python3 app.py

自动完成：建库 → 预置预约/护理/异常样例（空库时）→ 启动进程内通知任务
→ 启动 Web 服务。浏览器打开 http://localhost:8000

选项：
    --host 0.0.0.0 --port 8000   监听地址
    --no-seed                    不自动预置
    --reseed                     强制重建演示数据
    --no-worker                  不启动进程内通知任务
"""
from __future__ import annotations

import argparse
import logging
import os
import webbrowser

import seed as seed_mod
from notify import NotificationWorker
from server import build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="宠物美容预约与护理记录系统")
    parser.add_argument("--host", default=os.environ.get("GROOM_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GROOM_PORT", "8000")))
    parser.add_argument("--no-seed", action="store_true", help="不自动预置演示数据")
    parser.add_argument("--reseed", action="store_true", help="强制重建演示数据")
    parser.add_argument("--no-worker", action="store_true", help="不启动通知任务")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("grooming")

    if args.reseed:
        log.info("重建演示数据 ...")
        summary = seed_mod.seed()
        log.info("预置完成：%s", summary)
    elif not args.no_seed:
        if seed_mod.ensure_seeded():
            log.info("空库，已自动预置预约 / 护理 / 异常样例")
        else:
            log.info("检测到已有数据，跳过预置（--reseed 可重建）")

    worker = None
    if not args.no_worker:
        worker = NotificationWorker()
        worker.start()
        log.info("进程内提醒任务已启动（到点提醒 / 失败重试 / 超时未取扫描）")

    httpd = build_server(args.host, args.port)
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
    log.info("服务已启动 → %s", url)
    log.info("演示账号：周海/夏栀 可查看异常照片；阿淼 无权限")
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("正在停止 ...")
    finally:
        httpd.shutdown()
        if worker:
            worker.stop()
            worker.join(timeout=3)


if __name__ == "__main__":
    main()
