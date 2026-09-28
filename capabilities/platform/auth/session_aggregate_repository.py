#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""会话聚合根 + refresh token 家族 Redis 仓储。

复用 ``jonex_core/common/cache.py`` 的 :class:`CacheUtil`（异步 Redis 静态方法）。

键结构（见 ``capabilities/platform/dtos/session.py``）：
- 会话聚合根 ``jonex:session:{sid}``：对齐 Keycloak SSO Session ``sid``，存 JSON。
- refresh 家族 ``jonex:refresh:{tenant_id}:{user_id}``：存当前 refresh token 的 ``jti``，
  用于轮换 / 撤销 / 重放检测。吊销 = del，TTL 对齐 Keycloak 在线会话
  （``ssoSessionIdleTimeout`` 7 天，滑动续期）。
"""

from __future__ import annotations

from typing import Optional

from jonex_core.common.cache import CacheUtil
from capabilities.platform.dtos.session import (
    SessionAggregate,
    logout_jti_key,
    refresh_family_key,
    session_key,
)


def _to_str(raw) -> Optional[str]:
    """Redis 值归一化为 str（decode_responses 开关不确定，defensive 处理 bytes）。"""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


class SessionAggregateRepository:
    """会话聚合根 + refresh 家族的 Redis 仓储。"""

    # ── refresh 家族 ────────────────────────────────────────────────────────

    async def get_family_jti(self, tenant_id: str, user_id: int, sid: str) -> Optional[str]:
        """返回该 sid 设备当前 refresh token 的 jti；家族/字段不存在（登出/吊销）返回 None。"""
        return _to_str(await CacheUtil.hget(refresh_family_key(tenant_id, user_id), sid))

    async def set_family_jti(
        self, tenant_id: str, user_id: int, sid: str, jti: str, ttl: int
    ) -> None:
        """写入/轮换该 sid 设备的家族 jti 并 setex TTL（滑动续期）。"""
        key = refresh_family_key(tenant_id, user_id)
        await CacheUtil.hset(key, sid, jti)
        await CacheUtil.expire(key, ttl)

    async def delete_family(self, tenant_id: str, user_id: int) -> None:
        """吊销家族（登出 / SLO / 账号停用）——全量 DEL 整个 HASH（吊销用户所有会话）。"""
        await CacheUtil.delete(refresh_family_key(tenant_id, user_id))

    async def delete_family_sid(self, tenant_id: str, user_id: int, sid: str) -> None:
        """吊销单个 sid 设备的 refresh 家族字段（单会话吊销，不影响其他设备）。"""
        await CacheUtil.hdel(refresh_family_key(tenant_id, user_id), sid)

    # ── 会话聚合根 ──────────────────────────────────────────────────────────

    async def get_session(self, sid: str) -> Optional[SessionAggregate]:
        """按 sid 读会话聚合根；不存在或 JSON 损坏返回 None。"""
        raw = _to_str(await CacheUtil.get(session_key(sid)))
        if raw is None:
            return None
        try:
            return SessionAggregate.parse_raw(raw)
        except Exception:  # noqa: BLE001 —— 损坏数据按不存在处理
            return None

    async def save_session(self, aggregate: SessionAggregate, ttl: int) -> None:
        """写入/覆盖会话聚合根并 setex TTL。"""
        await CacheUtil.set(session_key(aggregate.sid), aggregate.json(), expire=ttl)

    async def delete_session(self, sid: str) -> None:
        """删除会话聚合根（吊销）。"""
        await CacheUtil.delete(session_key(sid))

    # ── logout_token jti 防重放 ─────────────────────────────────────────────

    async def check_and_record_logout_jti(self, jti: str, ttl: int) -> bool:
        """logout_token jti 防重放：首次返回 True 并 setnx，重放返回 False。

        CacheUtil 无 setnx 原子原语，这里直连 Redis client（对齐 CacheUtil 的
        ``finally: await client.aclose()`` 生命周期）。
        """
        from jonex_core.common.cache import get_redis_client

        client = get_redis_client()
        try:
            return await client.set(logout_jti_key(jti), "1", nx=True, ex=ttl) is not None
        finally:
            await client.aclose()


__all__ = ["SessionAggregateRepository"]
