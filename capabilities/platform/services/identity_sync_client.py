#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""企业身份同步出站拉取适配器（平台 → 企业主数据源）。

封装「Jonex标准拉取 API」，以 ``httpx.AsyncClient`` 拉取企业用户，
回吐结构化实体列表。本模块不落库、不感知租户，只负责 HTTP 调用 + 分页/游标 +
契约解析与错误分类。

请求（每页）::

    GET {base_url}/users?sync_type=full
    GET {base_url}/users?sync_type=incremental[&cursor={cursor}]

- 增量游标统一用不透明 ``cursor`` 透传（企业侧内部采用 ``updated_since`` 时间戳
  还是不透明 cursor，由提供方决定；消费端只传同一个 ``cursor`` 字段，二者二选一、
  绝不混发）。

响应信封（每页）::

    {
      "users": [ {user}, ... ],      # 兼容 "data" 键
      "has_more": false,             # 是否还有下一页
      "next_cursor": "..." | null,   # 下一页游标（has_more=true 时必填）
      "cursor": "..." | null         # 增量：本次同步应持久化的新游标（最后一页生效）
    }

用户实体::

    {
      "external_user_id": str,        # 必填，与 SSO sub 语义对齐
      "username": str,                # 必填
      "name": str | null,             # 可选 → 本地 display_name
      "email": str | null,            # 可选
      "status": "active" | "inactive" # 必填
    }

错误语义:

- 连接失败（``ConnectError``/``ConnectTimeout``）→ ``DataSourceConnectionError``(5005)，
  重试 1 次退避；
- 读/写超时 → ``ServiceTimeoutError``(5004)，不重试；
- 认证失败（HTTP 401/403）→ ``DataSourceAuthError``(5006)，不重试；
- 契约不符（信封非对象 / 缺 external_user_id / 字段类型不符 / 游标无效）→
  ``DataSourceContractError``(5007)，不重试；
- 上游 5xx → ``DataSourceConnectionError``(5005)，不重试。

安全：不跟随 HTTP 30x 重定向（``follow_redirects=False``）；目标地址 SSRF 校验由
上层 service 在构造本客户端前完成（``validate_datasource_url``）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Dict, List, Optional

import httpx

from jonex_core.common.exceptions import (
    DataSourceAuthError,
    DataSourceConnectionError,
    DataSourceContractError,
    ServiceTimeoutError,
)

# 超时 / 重试（超时 30s、连接失败重试 1 次退避）
DEFAULT_TIMEOUT = 30.0
_RETRY_BACKOFF = 0.5  # 连接失败重试前退避秒数
_MAX_ATTEMPTS = 2  # 首次请求 + 1 次重试
_MAX_PAGES = 1000  # 单次拉取分页安全上限，防无限分页拖垮 worker

_ACTIVE_STATUSES = {"active", "inactive"}
_FETCH_TYPES = {"full", "incremental"}


@dataclass
class ExternalUser:
    """结构化企业用户实体（契约解析后的最小字段集）。"""

    external_user_id: str
    username: str
    name: Optional[str] = None
    email: Optional[str] = None
    status: str = "active"


@dataclass
class SyncFetchResult:
    """一次拉取的完整结果（跨页聚合 + 增量新游标）。"""

    users: List[ExternalUser] = field(default_factory=list)
    cursor: Optional[str] = None


class IdentitySyncClient:
    """Jonex标准拉取 API 出站客户端。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._token = token or ""
        self._timeout = float(timeout)
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
            )
        return self._client

    async def aclose(self) -> None:
        """关闭底层连接（幂等；无客户端时为空操作）。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    # ── 主入口 ────────────────────────────────────────────────────────────────

    async def fetch_users(
        self, sync_type: str, cursor: Optional[str] = None
    ) -> SyncFetchResult:
        """拉取企业用户（全量快照或增量变更），跨页聚合后返回。

        Args:
            sync_type: ``"full"`` 或 ``"incremental"``。
            cursor: 增量模式下的起始游标（首轮为 ``None``）；全量模式忽略。

        Returns:
            ``SyncFetchResult``：结构化用户列表 + 下次增量的新游标。

        Raises:
            DataSourceContractError: 契约不符（信封/游标/用户实体字段）。
            DataSourceAuthError: 认证失败（401/403）。
            DataSourceConnectionError: 连接失败 / 上游 5xx。
            ServiceTimeoutError: 读/写超时。
        """
        if sync_type not in _FETCH_TYPES:
            raise DataSourceContractError(
                details={"field": "sync_type", "value": sync_type}
            )
        client = self._get_client()
        users: List[ExternalUser] = []
        page_cursor = cursor
        final_cursor: Optional[str] = None

        for _ in range(_MAX_PAGES):
            params: Dict[str, Any] = {"sync_type": sync_type}
            if sync_type == "incremental" and page_cursor:
                params["cursor"] = page_cursor

            resp = await self._get_with_retry(client, params)
            payload = self._parse_envelope(resp)

            users.extend(self._parse_users_page(payload))

            # 增量游标：仅接受字符串或 null；类型不符即契约无效
            if "cursor" in payload:
                raw_cursor = payload["cursor"]
                if raw_cursor is not None and not isinstance(raw_cursor, str):
                    raise DataSourceContractError(
                        details={"field": "cursor", "reason": "增量游标类型不符"}
                    )
                final_cursor = raw_cursor

            if not payload.get("has_more", False):
                break
            next_cursor = payload.get("next_cursor")
            if next_cursor is None or next_cursor == "":
                raise DataSourceContractError(
                    details={"reason": "has_more 为真但缺 next_cursor"}
                )
            if not isinstance(next_cursor, str):
                raise DataSourceContractError(
                    details={"field": "next_cursor", "reason": "分页游标类型不符"}
                )
            page_cursor = next_cursor
        else:
            raise DataSourceContractError(details={"reason": "分页超过安全上限"})

        return SyncFetchResult(users=users, cursor=final_cursor)

    async def probe_contract(self) -> int:
        """单页探测上游是否可达且符合契约（供「测试连接」使用）。

        只发**一次** ``GET /users?sync_type=full``，解析信封与用户列表后返回本次
        探测耗时（毫秒）。**不翻页、不持久化**：即便信封自称 ``has_more=true`` 也
        只取第一页，绝不沿 ``next_cursor`` 续拉。

        Returns:
            int: 本次探测耗时毫秒。

        Raises:
            DataSourceAuthError: 认证失败（401/403）。
            DataSourceContractError: 契约不符（信封 / 用户实体 / 非 JSON）。
            DataSourceConnectionError: 连接失败 / 上游 5xx。
            ServiceTimeoutError: 读/写超时。

        Note:
            调用方负责 ``await client.aclose()``。
        """
        started = perf_counter()
        resp = await self._get_with_retry(self._get_client(), {"sync_type": "full"})
        payload = self._parse_envelope(resp)
        self._parse_users_page(payload)
        return int((perf_counter() - started) * 1000)

    # ── HTTP + 重试 ───────────────────────────────────────────────────────────

    async def _get_with_retry(
        self, client: httpx.AsyncClient, params: Dict[str, Any]
    ) -> httpx.Response:
        """发起 GET；连接失败重试 1 次退避，认证/契约/超时不重试。"""
        last_conn_error: Optional[Exception] = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                return await client.get(
                    self._base_url + "/users",
                    params=params,
                    headers=self._headers(),
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # 连接失败（含连接超时）：可重试
                last_conn_error = exc
                if attempt < _MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_RETRY_BACKOFF)
                    continue
                raise DataSourceConnectionError(cause=exc) from exc
            except (httpx.ReadTimeout, httpx.TimeoutException) as exc:
                # 读/写超时：不重试
                raise ServiceTimeoutError(cause=exc) from exc
            except httpx.RequestError as exc:
                # 其他网络/协议错误：连接失败，不重试
                raise DataSourceConnectionError(cause=exc) from exc
        raise DataSourceConnectionError(cause=last_conn_error)

    def _parse_envelope(self, resp: httpx.Response) -> Dict[str, Any]:
        """解析 HTTP 响应：状态码分类 + 信封对象校验。"""
        status = resp.status_code
        if status in (401, 403):
            raise DataSourceAuthError(details={"status_code": status})
        if status >= 500:
            raise DataSourceConnectionError(details={"status_code": status})
        if status == 400:
            raise DataSourceContractError(details={"status_code": status})
        if status != 200:
            # 其余非 2xx（含 3xx，因 follow_redirects=False 不会自动跟随）
            raise DataSourceConnectionError(details={"status_code": status})
        try:
            payload = resp.json()
        except ValueError as exc:
            raise DataSourceContractError(
                cause=exc, details={"reason": "响应体非 JSON"}
            ) from exc
        if not isinstance(payload, dict):
            raise DataSourceContractError(details={"reason": "响应信封非对象"})
        return payload

    def _parse_users_page(self, payload: Dict[str, Any]) -> List[ExternalUser]:
        """从信封解析本页用户列表（``users`` 兼容 ``data``），逐条做实体契约校验。

        Raises:
            DataSourceContractError: 缺列表键 / 类型不符 / 任一实体契约不符。
        """
        raw_users = payload.get("users", payload.get("data"))
        if not isinstance(raw_users, list):
            raise DataSourceContractError(
                details={"reason": "响应信封缺 users 列表或类型不符"}
            )
        return [self._parse_user(raw) for raw in raw_users]

    @staticmethod
    def _parse_user(raw: Any) -> ExternalUser:
        """校验单个用户实体契约，不符抛 DataSourceContractError。"""
        if not isinstance(raw, dict):
            raise DataSourceContractError(details={"reason": "用户实体非对象"})

        ext_id = raw.get("external_user_id")
        if not isinstance(ext_id, str) or not ext_id:
            raise DataSourceContractError(
                details={"field": "external_user_id", "reason": "缺失或类型不符"}
            )

        username = raw.get("username")
        if not isinstance(username, str) or not username:
            raise DataSourceContractError(
                details={"field": "username", "reason": "缺失或类型不符"}
            )

        status = raw.get("status")
        if status not in _ACTIVE_STATUSES:
            raise DataSourceContractError(
                details={"field": "status", "reason": "非法取值或缺失"}
            )

        name = raw.get("name")
        email = raw.get("email")
        if name is not None and not isinstance(name, str):
            raise DataSourceContractError(details={"field": "name", "reason": "类型不符"})
        if email is not None and not isinstance(email, str):
            raise DataSourceContractError(details={"field": "email", "reason": "类型不符"})

        return ExternalUser(
            external_user_id=ext_id,
            username=username,
            name=name,
            email=email,
            status=status,
        )


__all__ = [
    "DEFAULT_TIMEOUT",
    "ExternalUser",
    "IdentitySyncClient",
    "SyncFetchResult",
]
