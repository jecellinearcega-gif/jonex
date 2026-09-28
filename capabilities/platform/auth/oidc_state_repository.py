#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""OIDC 授权码流程 state 一次性票据 Redis 仓储（T2）。

state 票据在授权发起时写入（后续 bead），callback 侧 `consume` 原子消费
（``getdel`` 一次性，重放 / 已消费返回 None 触发拒绝）。

复用 ``jonex_core/common/cache.py`` 的 :class:`CacheUtil` 做 set；``getdel``
无 CacheUtil 原语，直连 Redis client（对齐 SessionAggregateRepository 的
``check_and_record_logout_jti`` 生命周期）。
"""

from __future__ import annotations

from typing import Optional

from jonex_core.common.cache import CacheUtil
from capabilities.platform.dtos.session import OidcState, oidc_state_key


def _to_str(raw) -> Optional[str]:
    """Redis 值归一化为 str（decode_responses 开关不确定，defensive 处理 bytes）。"""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


class OidcStateRepository:
    """OIDC 授权码 state 一次性票据仓储。"""

    async def save(self, state: OidcState, ttl: int) -> None:
        """写入 state 票据并 setex TTL（授权发起侧调用）。"""
        await CacheUtil.set(oidc_state_key(state.state), state.json(), expire=ttl)

    async def consume(self, state: str) -> Optional[OidcState]:
        """原子消费 state（getdel 一次性）：首次返回并删除，重放 / 缺失返回 None。

        getdel 保证并发下只有一个请求能消费成功，满足「授权码一次性」语义。
        """
        from jonex_core.common.cache import get_redis_client

        client = get_redis_client()
        try:
            raw = await client.getdel(oidc_state_key(state))
        finally:
            await client.aclose()

        raw = _to_str(raw)
        if raw is None:
            return None
        try:
            return OidcState.parse_raw(raw)
        except Exception:  # noqa: BLE001 —— 损坏数据按不存在处理
            return None


__all__ = ["OidcStateRepository"]
