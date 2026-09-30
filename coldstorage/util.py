"""时间与杂项工具。时间统一用 UTC ISO 字符串（秒级）存储，界面转本地显示。"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

ISO_FMT = "%Y-%m-%dT%H:%M:%S"


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(ISO_FMT)


def now_iso() -> str:
    return iso(now())


def parse(s: str) -> datetime:
    dt = datetime.strptime(s, ISO_FMT)
    return dt.replace(tzinfo=timezone.utc)


def hours_between(a: str, b: str) -> float:
    return max(0.0, (parse(b) - parse(a)).total_seconds() / 3600.0)


def minutes_between(a: str, b: str) -> float:
    return hours_between(a, b) * 60.0


def shift_iso(s: str, **kw) -> str:
    return iso(parse(s) + timedelta(**kw))


def hash_password(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    h = hashlib.sha256(f"{salt}:{pw}".encode()).hexdigest()
    return f"{salt}${h}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(pw, salt), stored)
