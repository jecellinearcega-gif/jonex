#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Keycloak Admin API client（Layer 0，独立子任务）。

实现 per-realm realm-admin 凭据换取 admin token：

- confidential client 用 OIDC ``client_credentials`` grant 换 token
  （``POST /realms/{realm}/protocol/openid-connect/token``）；
- per-realm realm-admin 凭据每租户独立，**禁授 master 域**（per-realm 隔离，
  泄露只波及单租户、可独立轮换）；
- admin token **内存缓存 TTL 60s + 到期前轮换**（异步安全 + 并发去重）；
- 凭据脱敏：``RealmAdminCredentials.to_dict()`` 默认不返回明文 secret。

凭据安全注入契约：
- 每租户 realm-admin 凭据由调用方从 DB（``crypto.decrypt_secret``）或环境变量/
  Secret 解出后以 :class:`RealmAdminCredentials` 传入，本模块不落明文、不硬编码；
- 全局 server URL / TTL / 超时读环境变量（``KEYCLOAK_`` 前缀，键名约定见下方常量，
  在 ``jonex_core/common/config.py`` 落地）。

缓存为单进程内存缓存（与 STS 临时密钥缓存同策略）：多 worker 各自缓存只多几次换证，
不影响正确性；跨进程共享需 Redis，留到需要时再议。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import quote

import httpx

from jonex_core.common import get_logger
from jonex_core.common.exceptions import (
    InvalidParameterError,
    JonexException,
    ServiceUnavailableError,
    UpstreamServiceError,
)

logger = get_logger("integrations.keycloak_admin")

# ── 配置键名约定（KEYCLOAK_ 前缀，在 config.py 落地）──────────
# 本模块直接读 os.environ（与 config.py 的 BaseSettings 同源，均来自环境变量 / .env）。
KEYCLOAK_SERVER_URL = "KEYCLOAK_SERVER_URL"
# Keycloak 基地址，如 http://localhost:8080（不含 /realms/... 后缀）。

KEYCLOAK_ADMIN_TOKEN_TTL_SECONDS = "KEYCLOAK_ADMIN_TOKEN_TTL_SECONDS"
# admin token 内存缓存 TTL（秒），默认 60。

KEYCLOAK_TIMEOUT = "KEYCLOAK_TIMEOUT"
# httpx 请求超时（秒），默认 30。

# 预留（master 域 bootstrap 凭据，属 realm 开通/纳管任务，非本 client 职责）：
#   KEYCLOAK_ADMIN_REALM         默认 "master"
#   KEYCLOAK_ADMIN_CLIENT_ID     bootstrap confidential client
#   KEYCLOAK_ADMIN_CLIENT_SECRET bootstrap secret（环境变量/Secret 注入，禁明文）

_DEFAULT_TOKEN_TTL_SECONDS = 60
_DEFAULT_REFRESH_BEFORE_SECONDS = 5
_DEFAULT_TIMEOUT = 30.0
_REDACTED = "***"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _env_float(name: str, default: float) -> float:
    """读数值型 env，容错空串 / 空白串 / 非法值 —— 一律回退 ``default``。

    为什么要容错：``docker-compose`` 从空变量插值、或 env 文件写成
    ``KEYCLOAK_TIMEOUT=`` 时，键**存在但值为空串**，裸 ``float(os.environ.get(...))``
    会抛 ``ValueError``，让「未填真实 SSO 配置」的部署直接起不来。
    非法值（如 ``abc``）与「未配置」同档处理：一个笔误不应让服务崩。
    """
    raw = os.environ.get(name, "")
    if isinstance(raw, str):
        raw = raw.strip()
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    """``_env_float`` 的整数版（语义见 :func:`_env_float`）。"""
    raw = os.environ.get(name, "")
    if isinstance(raw, str):
        raw = raw.strip()
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RealmAdminCredentials:
    """per-realm realm-admin 凭据（每租户独立，禁授 master 域）。

    ``client_secret`` 由调用方从加密存储（Fernet）解出或安全注入传入，
    本结构不负责加解密；``to_dict()`` 默认脱敏，杜绝列表/详情泄露明文。
    """

    realm: str
    client_id: str
    client_secret: str

    def to_dict(self, *, redact: bool = True) -> dict:
        """序列化凭据；默认脱敏 secret（列表/详情不返回明文）。"""
        secret = self.client_secret
        if redact:
            secret = _REDACTED if secret else ""
        return {
            "realm": self.realm,
            "client_id": self.client_id,
            "client_secret": secret,
        }


@dataclass(frozen=True)
class AdminToken:
    """换取到的 admin token + 绝对过期时间（UTC）。"""

    access_token: str
    token_type: str
    expires_at: datetime

    @property
    def authorization_header(self) -> str:
        """组装 ``Authorization`` 请求头值。"""
        return f"{self.token_type or 'Bearer'} {self.access_token}"


class KeycloakAdminClient:
    """Keycloak Admin API client：per-realm 凭据 → 缓存 admin token。

    异步接口；缓存策略对齐 STS 临时密钥缓存（per-realm 键 + 到期前提前量刷新 +
    per-realm 锁并发去重）。换证失败 fail-closed 抛 :class:`JonexException` 子类。
    """

    def __init__(
        self,
        *,
        server_url: Optional[str] = None,
        token_ttl_seconds: Optional[int] = None,
        refresh_before_seconds: int = _DEFAULT_REFRESH_BEFORE_SECONDS,
        timeout: Optional[float] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        """
        Args:
            server_url: Keycloak 基地址；缺省读 ``KEYCLOAK_SERVER_URL``。
            token_ttl_seconds: 缓存 TTL（秒）；缺省读 ``KEYCLOAK_ADMIN_TOKEN_TTL_SECONDS``（默认 60）。
            refresh_before_seconds: 到期前提前量（秒），剩余有效期低于它即轮换。
            timeout: httpx 超时（秒）；缺省读 ``KEYCLOAK_TIMEOUT``。
            transport: 注入 httpx transport（测试用 MockTransport）。
            clock: 注入时钟（测试用，便于推进时间触发过期）。
        """
        self._server_url = (
            server_url or os.environ.get(KEYCLOAK_SERVER_URL, "")
        ).rstrip("/")
        self._ttl = (
            int(token_ttl_seconds)
            if token_ttl_seconds is not None
            else _env_int(KEYCLOAK_ADMIN_TOKEN_TTL_SECONDS, _DEFAULT_TOKEN_TTL_SECONDS)
        )
        self._refresh_before = timedelta(seconds=refresh_before_seconds)
        self._timeout = (
            float(timeout)
            if timeout is not None
            else _env_float(KEYCLOAK_TIMEOUT, _DEFAULT_TIMEOUT)
        )
        self._transport = transport
        self._clock = clock or _utcnow
        self._client: Optional[httpx.AsyncClient] = None
        self._token_cache: dict[str, AdminToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ── 校验 ────────────────────────────────────────────────────────────────

    def _require_server_url(self) -> None:
        if not self._server_url:
            raise ServiceUnavailableError(
                message=f"Keycloak Admin 客户端初始化失败：缺少环境变量 {KEYCLOAK_SERVER_URL}。"
            )

    @staticmethod
    def _forbid_master_realm(realm: str) -> None:
        """禁授 master 域（per-realm 隔离契约）。

        守卫的**唯一实现**，吃 realm 字符串 —— 这样不持有 ``RealmAdminCredentials``
        的读方法（只拿得到 realm 名）也能复用同一条约束。
        """
        if realm == "master":
            raise InvalidParameterError(
                message="禁止使用 master 域作为 per-realm realm-admin 凭据（per-realm 隔离契约）"
            )

    @staticmethod
    def _forbid_master(credentials: RealmAdminCredentials) -> None:
        """保留既有调用点签名（``get_admin_token`` 在用），委派给 :meth:`_forbid_master_realm`。"""
        KeycloakAdminClient._forbid_master_realm(credentials.realm)

    def _is_fresh(self, token: AdminToken, now: datetime) -> bool:
        """token 剩余有效期是否超过提前量（即无需轮换）。"""
        return token.expires_at - now > self._refresh_before

    # ── HTTP ────────────────────────────────────────────────────────────────

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            )
        return self._client

    async def _fetch_token(self, credentials: RealmAdminCredentials) -> AdminToken:
        """用 client_credentials 换取 admin token（不缓存，纯请求）。"""
        url = f"{self._server_url}/realms/{credentials.realm}/protocol/openid-connect/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
        }
        try:
            resp = await self._get_client().post(url, data=data)
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as e:
            raise UpstreamServiceError(
                message="Keycloak admin token 换取失败",
                details={"realm": credentials.realm},
                cause=e,
            ) from e

        access_token = payload.get("access_token")
        if not access_token:
            raise UpstreamServiceError(
                message="Keycloak token 响应缺少 access_token",
                details={"realm": credentials.realm},
            )

        # 缓存 TTL 恒定 60s；上游 expires_in 更短则取短（尊重真实寿命）。
        ttl = self._ttl
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, int) and expires_in > 0:
            ttl = min(expires_in, self._ttl)

        return AdminToken(
            access_token=access_token,
            token_type=payload.get("token_type") or "Bearer",
            expires_at=self._clock() + timedelta(seconds=ttl),
        )

    # ── 对外 API ────────────────────────────────────────────────────────────

    async def get_admin_token(self, credentials: RealmAdminCredentials) -> AdminToken:
        """获取某 realm 的 admin token：命中缓存直接返回，否则换取并缓存。

        并发去重：同 realm 多协程同时 miss 时，通过 per-realm 锁 + 双重检查
        保证只发一次 token 请求。换证失败 fail-closed 抛 JonexException 子类。
        """
        self._forbid_master(credentials)
        self._require_server_url()

        realm = credentials.realm
        now = self._clock()
        cached = self._token_cache.get(realm)
        if cached is not None and self._is_fresh(cached, now):
            return cached

        lock = self._locks.setdefault(realm, asyncio.Lock())
        async with lock:
            # 双重检查：等待锁期间可能有别的协程已完成换证。
            now = self._clock()
            cached = self._token_cache.get(realm)
            if cached is not None and self._is_fresh(cached, now):
                return cached

            token = await self._fetch_token(credentials)
            self._token_cache[realm] = token
            logger.info("Keycloak admin token 已换取并缓存（realm: %s）", realm)
            return token

    async def _admin_get_checked(
        self, credentials: RealmAdminCredentials, url: str
    ) -> tuple[bool, Optional[object]]:
        """带 admin token 的 GET，**区分「答复了」与「没答复」、不抛**。

        返回 ``(answered, payload)``：

        - ``(True, obj)``  —— HTTP 2xx 且 body 是合法 JSON（调用方据此可以下结论）
        - ``(False, None)`` —— 网络异常 / HTTP 非 2xx / body 非 JSON（**没问到**，
          调用方不得据此判断「上游没有这个对象」）

        ⛔ 必须同时接住 ``httpx.HTTPError`` 与 ``ValueError``：只接前者时，上游返回
        **200 但 body 非 JSON** 会以 ``json.JSONDecodeError``（``ValueError`` 子类）
        穿透。这些方法跑在 SSO 登录与身份同步的关键路径上 —— 上游的一个坏响应
        不能变成平台登录不可用。

        守卫与 base URL 校验放在 ``try`` **之外**：它们是调用方的契约违规（程序员错误），
        必须响亮地抛；吞成 None 只会让「拿 master 域去换证」这种配置错误静默失效。

        ⚠️ 为什么要单开这一层：``_admin_get`` 把「打不通」与「答复为空」压成同一个
        ``None``，于是「Keycloak 说没有这个人」和「Keycloak 调用失败」在停用链路上
        写成同一句差异清单 —— 前者无事可做、后者可能真有洞，混在一起就是误报红。
        """
        self._forbid_master_realm(credentials.realm)
        self._require_server_url()
        try:
            token = await self.get_admin_token(credentials)
            resp = await self._get_client().get(
                url, headers={"Authorization": token.authorization_header}
            )
            resp.raise_for_status()
            return True, resp.json()
        except (httpx.HTTPError, ValueError, JonexException) as e:
            logger.warning(
                "Keycloak Admin GET 失败，按「取不到」降级（realm: %s）：%s",
                credentials.realm,
                e,
            )
            return False, None

    async def _admin_get(
        self, credentials: RealmAdminCredentials, url: str
    ) -> Optional[object]:
        """``_admin_get_checked`` 的「压平」版：没答复与答复为空都回 ``None``。

        供**不需要区分**两种情况的调用方使用（如 ``get_federated_identity``：
        它要的只是「有没有这个值」）。需要区分时用 ``_admin_get_checked``。
        """
        _answered, payload = await self._admin_get_checked(credentials, url)
        return payload

    async def get_federated_identity(
        self, credentials: RealmAdminCredentials, kc_user_id: str
    ) -> Optional[str]:
        """读某 Keycloak 用户的上游主体标识（= ``federated-identity`` 里的 ``userId``）。

        返回首个元素的 ``userId``；**失败一律返回 None（不抛）**：HTTP 错误 / 404 /
        200 但非 JSON / 空列表 / 元素缺 ``userId``。调用方据此降级、不阻断登录。
        """
        url = (
            f"{self._server_url}/admin/realms/{credentials.realm}"
            f"/users/{quote(kc_user_id)}/federated-identity"
        )
        payload = await self._admin_get(credentials, url)
        if not isinstance(payload, list) or not payload:
            return None
        first = payload[0]
        if not isinstance(first, dict):
            return None
        subject = first.get("userId")
        return subject or None

    async def lookup_user_id(
        self, credentials: RealmAdminCredentials, username: str
    ) -> tuple[bool, Optional[str]]:
        """按 username 精确反查 Keycloak 本地 user_id，**区分「答复了」与「没答复」**。

        返回 ``(answered, user_id)``：

        - ``(True, "kc-1")`` —— 命中
        - ``(True, None)``  —— **Keycloak 明确答复：没有这个用户名的账号**
        - ``(False, None)`` —— 没问到 Keycloak（网络 / HTTP / 报文异常），
          **不得**据此判断「人不在」

        ``exact=true`` 是必需的 —— 缺了它 Keycloak 按**前缀模糊**匹配，查到的是别人。
        """
        url = (
            f"{self._server_url}/admin/realms/{credentials.realm}"
            f"/users?username={quote(username)}&exact=true"
        )
        answered, payload = await self._admin_get_checked(credentials, url)
        if not answered:
            return False, None
        if not isinstance(payload, list) or not payload:
            return True, None
        first = payload[0]
        if not isinstance(first, dict):
            return True, None
        user_id = first.get("id")
        return True, user_id or None

    async def find_user_id_by_username(
        self, credentials: RealmAdminCredentials, username: str
    ) -> Optional[str]:
        """``lookup_user_id`` 的「压平」版：**失败一律返回 None（不抛）**。

        ⚠️ 拿它判断「上游有没有这个人」是错的 —— 「查无此人」与「查不了」都是
        ``None``；需要区分时用 ``lookup_user_id``。
        """
        _answered, user_id = await self.lookup_user_id(credentials, username)
        return user_id

    async def list_users_checked(
        self,
        credentials: RealmAdminCredentials,
        *,
        first: int = 0,
        max_results: int = 100,
    ) -> tuple[bool, list]:
        """列出 realm 内用户，**区分「答复了」与「没答复」**。

        返回 ``(answered, users)``：``answered=True`` ⇒ 拿到了一份合法名单（可能是
        空的，也可能被 ``max_results`` 截断 —— 调用方需自行按长度判断是否完整）；
        ``False`` ⇒ 没问到，名单不可用。

        ⛔ 不用 ``search=`` 参数：Keycloak 的 ``search`` 是**跨 username/email/
        firstName/lastName 的前缀模糊匹配**，拿它当「大小写不敏感的精确定位」会命中
        别人。宁可拉列表回来做**精确**的本地 ``lower()`` 比对。
        """
        url = (
            f"{self._server_url}/admin/realms/{credentials.realm}"
            f"/users?first={int(first)}&max={int(max_results)}"
            f"&briefRepresentation=true"
        )
        answered, payload = await self._admin_get_checked(credentials, url)
        if not answered or not isinstance(payload, list):
            return answered, []
        return True, [item for item in payload if isinstance(item, dict)]

    async def list_users(
        self,
        credentials: RealmAdminCredentials,
        *,
        first: int = 0,
        max_results: int = 100,
    ) -> list:
        """``list_users_checked`` 的「压平」版：**失败一律返回 ``[]``（不抛）**。

        供**不需要区分**两种情况的调用方使用；需要区分时用 ``list_users_checked``。
        """
        _answered, users = await self.list_users_checked(
            credentials, first=first, max_results=max_results
        )
        return users

    def invalidate(self, realm: str) -> None:
        """主动失效某 realm 的缓存 token（凭据轮换 / 泄露撤销时用）。

        同时清掉该 realm 的锁，避免 ``_locks`` 随历史 realm 无界增长。
        """
        self._token_cache.pop(realm, None)
        self._locks.pop(realm, None)

    async def aclose(self) -> None:
        """关闭底层 httpx client（应用退出前调用，释放连接）。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# 全局单例（生产接线用；测试直接构造 KeycloakAdminClient 注入 transport）。
_client_instance: Optional[KeycloakAdminClient] = None


def get_keycloak_admin_client() -> KeycloakAdminClient:
    """获取 Keycloak Admin client 单例实例。"""
    global _client_instance
    if _client_instance is None:
        _client_instance = KeycloakAdminClient()
    return _client_instance


__all__ = [
    "RealmAdminCredentials",
    "AdminToken",
    "KeycloakAdminClient",
    "get_keycloak_admin_client",
    "KEYCLOAK_SERVER_URL",
    "KEYCLOAK_ADMIN_TOKEN_TTL_SECONDS",
    "KEYCLOAK_TIMEOUT",
]
