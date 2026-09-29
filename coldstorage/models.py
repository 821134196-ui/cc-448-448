"""领域常量与状态枚举（虚构经营规则）。"""

from __future__ import annotations

# 状态
PENDING = "pending"
CONFIRMED = "confirmed"
CHECKED_IN = "checked_in"
MOVE_PENDING = "move_pending"
CHECKED_OUT = "checked_out"
SETTLED = "settled"
REJECTED = "rejected"
CANCELLED = "cancelled"

# 移格
MR_REQUESTED = "requested"
MR_APPROVED = "approved"
MR_REJECTED = "rejected"

# 异常类型
INC_OVERLOAD = "overload"              # 超容
INC_PULL_DOWN = "temp_pull_down"       # 临时降温
INC_BREACH = "temp_breach"             # 温度越界
INC_OUTAGE = "power_outage"            # 断电
INC_DOOR_OPEN = "door_left_open"       # 门未关
INC_LEAK = "leakage"                   # 货品渗漏
INC_EARLY = "early_checkout"           # 提前退租
INC_MANUAL_TEMP = "manual_temp_change"  # 管理员手工改温

BILLABLE_INCIDENT_KINDS = (
    INC_OVERLOAD,
    INC_PULL_DOWN,
    INC_BREACH,
    INC_OUTAGE,
)

# 计费规则（虚构）
OVERLOAD_SURCHARGE_RATE = 0.30   # 超容附加 = 该段占格费 × 30%
PULL_DOWN_HOUR_RATE = 1.50       # 临时降温能耗 元/小时
OUTAGE_COMP_RATE = 0.50          # 断电补偿 = 该段费用 × 50%
BREACH_COMP_RATE = 0.30          # 越界补偿 = 该段费用 × 30%

# 门磁
DOOR_OPEN_LIMIT_SECONDS = 300    # 开门超过 5 分钟记“门未关”

# 提前退租判定：实际结束早于计划结束 1 小时以上
EARLY_GRACE_SECONDS = 3600

# 温区类型
ZONE_FROZEN = "frozen"
ZONE_CHILLED = "chilled"
ZONE_AMBIENT = "ambient"

ZONE_TYPE_LABEL = {
    ZONE_FROZEN: "深冷",
    ZONE_CHILLED: "冷藏",
    ZONE_AMBIENT: "恒温",
}

STATUS_LABEL = {
    PENDING: "待确认",
    CONFIRMED: "已锁定货格",
    CHECKED_IN: "入库计费中",
    MOVE_PENDING: "跨温区移格待确认",
    CHECKED_OUT: "已退库待结账",
    SETTLED: "已结账",
    REJECTED: "已拒绝",
    CANCELLED: "已取消",
}

INCIDENT_LABEL = {
    INC_OVERLOAD: "超容",
    INC_PULL_DOWN: "临时降温",
    INC_BREACH: "温度越界",
    INC_OUTAGE: "断电补偿",
    INC_DOOR_OPEN: "门未关",
    INC_LEAK: "货品渗漏",
    INC_EARLY: "提前退租",
    INC_MANUAL_TEMP: "手工改温",
}

SEG_LABEL = {
    "normal": "正常计费",
    INC_OVERLOAD: "超容",
    INC_PULL_DOWN: "临时降温",
    INC_OUTAGE: "断电",
    INC_BREACH: "温度越界",
}


class PlatformError(Exception):
    """业务规则错误。"""


class CellCapacityError(PlatformError):
    """货格容量不足或已被锁定（两笔预约争抢最后一个货格时败方抛出）。"""


class ZoneMismatchError(PlatformError):
    """目标温区与库区温区不兼容，或跨温区移位未经重新确认。"""


class BillingClosedError(PlatformError):
    """已结账租期不可重新打开。"""


class PermissionDenied(PlatformError):
    """租户越权访问他人货格，或角色无权操作。"""


def yuan(value: float) -> float:
    return round(value + 0.0, 2)


def hours(seconds: float) -> float:
    return round(seconds / 3600.0, 4)
