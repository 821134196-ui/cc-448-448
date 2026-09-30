"""全局配置：路径、温区定义、计费费率。

温区（zone）是冷库内独立的温控区域；货格（cell）是温区内可租用的最小单元。
所有温区带上下限（摄氏度），预留余量给开门波动。
"""
from __future__ import annotations

import os
from pathlib import Path

# ---- 路径 ----------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CS_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "coldstorage.db"
PHOTO_DIR = DATA_DIR / "photos"

# ---- 温区 ----------------------------------------------------------------
# key: (下限, 上限, 每小时基础租金 元/m³, 目标温度)
ZONES = {
    "F":  {"name": "冷冻区", "temp_min": -25.0, "temp_max": -18.0, "target": -21.5, "rate": 1.20},
    "C":  {"name": "冷藏区", "temp_min":   0.0, "temp_max":   4.0, "target":   2.0, "rate": 0.80},
    "AC": {"name": "恒温区", "temp_min":  12.0, "temp_max":  18.0, "target":  15.0, "rate": 0.55},
}
ZONE_ORDER = ["F", "C", "AC"]

# ---- 计费规则（全部虚构） --------------------------------------------------
# 重量附加费：货品实际重量超过 300 kg/m³ 标准堆码密度的部分按吨时另计。
WEIGHT_DENSITY_KG_PER_M3 = 300.0
WEIGHT_SURCHARGE_PER_TON_HOUR = 0.35

# 越界温度：每分钟补偿（元），以采样点之间的时长积分
EXCURSION_RATE_PER_MIN = 0.60
# 断电补偿：每分钟
OUTAGE_RATE_PER_MIN = 1.00
# 门未关：每次（持续超过 5 分钟记录一次）
DOOR_OPEN_FEE = 30.0
DOOR_OPEN_GRACE_MIN = 5
# 货品渗漏：每次清理费
LEAK_FEE = 120.0
# 临时降温：按申请的降温时长 × 每小时附加（管理员操作）
TEMP_PULL_RATE_PER_HOUR = 8.0
# 提前退租违约金：未满最短租期（24h）部分租金的 30%
EARLY_EXIT_MIN_HOURS = 24.0
EARLY_EXIT_PENALTY_RATE = 0.30
# 超容罚金：超出预约容量部分，每立方米一次性
OVERCAPACITY_FEE_PER_M3 = 50.0

# ---- 采样 ----------------------------------------------------------------
SAMPLE_INTERVAL_SEC = 60          # 模拟适配器产生读数的节奏
MONITOR_INTERVAL_SEC = 30         # 进程内任务扫描间隔
EXCURSION_GRACE_SEC = 120         # 连续越界超过此时长才计为异常（抗抖动）

# ---- 演示 ---------------------------------------------------------------
DEMO_TENANTS = [
    ("t_lvyuan", "绿源生鲜", "pass1234"),
    ("t_beiji",  "北极渔业", "pass1234"),
    ("t_tianshan", "天山乳品", "pass1234"),
]
ADMIN_USER = ("admin", "管理员", "admin1234")
FINANCE_USER = ("finance", "财务复核", "finance1234")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
