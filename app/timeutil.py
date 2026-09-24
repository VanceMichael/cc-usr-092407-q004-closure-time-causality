"""时间处理工具。

事件输入可以携带不同 UTC 偏移，区间比较前统一转换为带时区的 UTC 时间。
机场本地日期只用于判断是否跨午夜。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.errors import ValidationError

# Accept ISO 8601 date-time with Z, +HH:MM or explicit timezone.
# Naive timestamps are rejected: a disruption window without a zone is ambiguous.
_OFFSET_RE = re.compile(r"(Z|[+-]\d{2}:\d{2})$")


def parse_event_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValidationError(
            f"Field '{field}' must be an ISO 8601 date-time string",
            {"field": field},
        )
    text = value.strip()
    if not _OFFSET_RE.search(text):
        raise ValidationError(
            f"Field '{field}' must include a timezone designator (Z or ±HH:MM)",
            {"field": field, "received": value},
        )
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        raise ValidationError(
            f"Field '{field}' is not a valid ISO 8601 date-time",
            {"field": field, "received": value},
        ) from None
    if dt.tzinfo is None:  # pragma: no cover - guarded by regex above
        raise ValidationError(
            f"Field '{field}' must include a timezone designator (Z or ±HH:MM)",
            {"field": field, "received": value},
        )
    return dt.astimezone(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(timezone.utc)


def load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError(
            f"Airport timezone '{name}' is not a valid IANA timezone",
            {"timezone": name},
        ) from None


def airport_zone(airport) -> ZoneInfo:
    """机场记录的 IANA 时区（加载时已校验）。"""
    return ZoneInfo(airport.timezone)


def iso_local(local_dt: datetime) -> str:
    """格式化机场本地墙钟时间，偏移随当日 DST 状态变化（错误明细用）。"""
    if local_dt.tzinfo is None:
        raise ValueError("local datetime must be timezone-aware")
    return local_dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def localize_ambiguous(local_naive: datetime, tz: ZoneInfo, *, fold: int) -> datetime:
    """显式标注 DST 重复时刻属于 fold 前(0)还是 fold 后(1)的那一次。

    事件输入携带固定 UTC 偏移，正常解析不会产生歧义；该工具供需要直接
    构造机场本地墙钟的校验/测试使用。
    """
    return local_naive.replace(tzinfo=tz, fold=fold)


def overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    """计算左闭右开区间的重叠，端点相接不算重叠。"""
    return start_a < end_b and start_b < end_a


def overlap_minutes(
    start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime
) -> int:
    start = max(start_a, start_b)
    end = min(end_a, end_b)
    return max(0, int((end - start).total_seconds() // 60))


def crosses_local_midnight(
    start: datetime, end: datetime, tz: ZoneInfo
) -> bool:
    """判断左闭右开区间在指定时区内是否跨越两个自然日。"""
    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)
    return local_start.date() != local_end.date()


def minutes_until(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)
