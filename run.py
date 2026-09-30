#!/usr/bin/env python3
"""本地启动入口。

一条命令创建"预约冲突 + 温度异常"样例并启动服务：

    python3 run.py --demo            # 重建演示数据并启动（http://127.0.0.1:8000）
    python3 run.py --demo --no-monitor
    python3 run.py                   # 使用现有数据启动
    python3 run.py --seed-only       # 只重建数据不启动
"""
from __future__ import annotations

import argparse
import json
import sys

from coldstorage import config
from coldstorage.db import connect, init_db
from coldstorage.monitor import Monitor
from coldstorage.seed import seed
from coldstorage.server import make_server
from coldstorage.services import seed_catalog


def main() -> int:
    ap = argparse.ArgumentParser(description="共享冷库租格与温度计费平台")
    ap.add_argument("--demo", action="store_true", help="重建并播种虚构演示数据")
    ap.add_argument("--seed-only", action="store_true", help="只播种不启动服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-monitor", action="store_true", help="关闭后台采样任务")
    args = ap.parse_args()

    config.ensure_dirs()
    if args.demo:
        info = seed(reset=True)
        print("== 演示数据已重建 ==")
        print(json.dumps(info.get("race", {}), ensure_ascii=False, indent=2))
        print("异常样例租期 id:", info.get("anomaly_lease"),
              " 待复核账单 id:", info.get("open_bill"))
        print("账号：")
        for k, v in info.get("accounts", {}).items():
            print(f"  {k}  → {v}")
    else:
        init_db()
        conn = connect()
        seed_catalog(conn)
        conn.commit()
        conn.close()

    if args.seed_only:
        return 0

    monitor = None
    if not args.no_monitor:
        monitor = Monitor(connect).start()
        print("[monitor] 后台采样与越界检测已启动")

    httpd = make_server(args.host, args.port)
    print(f"== 冷库平台已启动：http://{args.host}:{args.port} ==")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        if monitor:
            monitor.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
