#!/usr/bin/env python3
"""本地启动入口（仅需 Python 3.10+ 标准库）。

一条命令创建预约冲突和温度异常样例并启动服务：

    python3 run.py --demo --serve

其它用法：
    python3 run.py --demo            # 仅重建演示库（含冲突/温度异常样例）后退出
    python3 run.py --serve           # 用现有库启动 Web
    python3 run.py --demo --serve --port 8080

演示身份（一键登录，无需密码）：
    管理员 admin / 财务 finance / 租户 tenant1..3
数据文件：data/coldstore.db（SQLite），货品照片：data/photos/
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from coldstorage.adapters import MonitorLoop, SAMPLING_SECONDS
from coldstorage.seed import seed_demo
from coldstorage.webapp import make_server

DEFAULT_DB = os.path.join(os.path.dirname(__file__), "data", "coldstore.db")
DEFAULT_PHOTOS = os.path.join(os.path.dirname(__file__), "data", "photos")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="共享冷库租格与温度计费平台（虚构数据）")
    ap.add_argument("--demo", action="store_true", help="重建演示库（预约冲突+温度异常样例）")
    ap.add_argument("--serve", action="store_true", help="启动 Web 服务")
    ap.add_argument("--db", default=DEFAULT_DB, help="SQLite 数据库路径")
    ap.add_argument("--photos", default=DEFAULT_PHOTOS, help="货品照片目录")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-monitor", action="store_true", help="不启动后台温度/门磁监测任务")
    ap.add_argument("--monitor-interval", type=int, default=SAMPLING_SECONDS)
    args = ap.parse_args(argv)

    if not args.demo and not args.serve:
        ap.print_help()
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)

    if args.demo:
        print("▶ 写入虚构演示数据（库区/货格/租户/租约/采样/异常）…")
        report = seed_demo(args.db, args.photos)
        c = report["contention"]
        print(f"  ✓ 两笔预约争抢最后一个货格：胜者 #{c['winner']}，"
              f"败者 #{c['loser']}（{c['error']}）")
        print(f"  ✓ 温度异常样例：断电 / 越界 / 临时降温窗口已回填到温度曲线")
        print(f"  ✓ 待结账账单 #{report['reservation_open_bill']}；"
              f"已结账账单 #{report['reservation_settled']}")
        print(f"  ✓ 跨温区移格待确认：租约 #{report['reservation_cross_move']}")
        print(f"  数据库：{args.db}")

    if not args.serve:
        return 0

    monitor = None
    if not args.no_monitor:
        monitor = MonitorLoop(args.db, interval=args.monitor_interval)
        monitor.start()
        print(f"▶ 进程内监测任务已启动（每 {args.monitor_interval}s 采样一次）")

    httpd = make_server(args.db, args.photos, args.host, args.port, monitor=monitor)
    url = f"http://{args.host}:{args.port}/"
    print("▶ Web 已启动：", url)
    print("  演示登录令牌：tok-admin（管理员）/ tok-finance（财务）/ tok-tenant1（租户）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n▶ 正在停止…")
    finally:
        if monitor:
            monitor.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
