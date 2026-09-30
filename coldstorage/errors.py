"""跨层共享的业务错误类型。"""


class DomainError(Exception):
    """业务规则冲突（HTTP 层映射为 4xx）。"""
