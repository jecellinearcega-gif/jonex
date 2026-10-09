#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务中心 ORM 公共小工具。"""

from datetime import datetime

# 服务器本地时区，与 TimestampMixin 的 datetime.now()（naive 本地时间）落库口径一致。
# 序列化出口据此补齐真实时区偏移（如 +08:00），避免 naive 值被硬标成 UTC 导致前端 +8h 偏差。
_LOCAL_TZ = datetime.now().astimezone().tzinfo


def iso(value: datetime | None) -> str | None:
    """把库里的 naive 本地时间序列化成带时区偏移的 ISO8601 字符串。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_LOCAL_TZ)
    return value.isoformat()


__all__ = ["iso"]
