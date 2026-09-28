#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""账号状态双向同步（Keycloak SSO 接入）。

以 ``platform.users`` 为权威，Keycloak 侧最终对齐：

- 平台 → Keycloak：本地停用/启用账号（``status=0/1``）、软删等效停用，
  调用 Keycloak Admin API ``PUT /admin/realms/{realm}/users/{user_id}`` 更新
  ``enabled``（``user_id`` = 本地 ``users.external_id``）。
- Keycloak → 平台：预留入口，收到 ``USER_DISABLED`` / 轮询时更新本地
  ``users.status``。

分区策略：同步失败只记日志、降级不抛，绝不因 IdP 不可用阻塞本地判定。
``last_login_at`` 回流由登录成功路径写回，不归本模块。
"""

from __future__ import annotations

import os
from typing import NamedTuple, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.models.user import User
from jonex_core.common import get_logger
from jonex_core.common.tenant import require_tenant
from jonex_core.integrations.keycloak_admin import (
    KEYCLOAK_SERVER_URL,
    KEYCLOAK_TIMEOUT,
    AdminToken,
    KeycloakAdminClient,
    RealmAdminCredentials,
    get_keycloak_admin_client,
)

logger = get_logger("platform.auth.account_sync")

# 环境变量占位：realm-admin 凭据注入（禁明文硬编码）。
# 平台级单 client 模型：整个平台共用一个 realm-admin 凭据（非按 realm 配置，有意设计）；
# 生产应由 Secret 管理 / 加密存储注入；此处仅作「读不到则降级跳过并告警」的占位。
KEYCLOAK_REALM_ADMIN_CLIENT_ID = "KEYCLOAK_REALM_ADMIN_CLIENT_ID"
KEYCLOAK_REALM_ADMIN_CLIENT_SECRET = "KEYCLOAK_REALM_ADMIN_CLIENT_SECRET"

_DEFAULT_TIMEOUT = 30.0


def _env_float(name: str, default: float) -> float:
    """读数值型 env，容错空串 / 空白串 / 非法值 —— 一律回退 ``default``。

    键存在但为空串（``KEYCLOAK_TIMEOUT=``）时，裸 ``float()`` 会抛 ``ValueError``，
    让未填真实 SSO 配置的部署起不来。语义与 ``keycloak_admin._env_float`` 一致。
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


# 原因码 → 人话（差异清单文案用；码与文案同处一地，改码不会漏改文案）。
KC_SYNC_REASON_TEXT: dict = {
    "no_credentials": "未解析到 Keycloak realm-admin 凭据",
    "lookup_miss": "无法向 Keycloak 核实该账号（查询未取得答复），停用可能未生效",
    "not_provisioned": "Keycloak 侧没有该账号（从未通过 SSO 登录），无需停用",
    "http_error": "Keycloak 调用失败（网络异常或非 2xx）",
}

#: 反查落空后的两种结论（``_resolve_by_case_insensitive_username`` 的返回值域）。
#: ⛔ 二者**必须分开**：``ABSENT`` 是「Keycloak 明确说没这个人」（无事可做），
#: ``AMBIGUOUS`` 是「名单里有多条同名」（不敢猜、可能停错人）—— 合成一个 None
#: 会让后者被当成前者，把一个真风险判成良性。
_MATCH_HIT = "hit"
_MATCH_ABSENT = "absent"
_MATCH_AMBIGUOUS = "ambiguous"

#: 拉名单反查时用的单页条数；**返回条数 == 这个数**即视为名单可能被截断。
_LIST_PAGE_SIZE = 100


class _LookupOutcome(NamedTuple):
    """``_resolve_missing_external_id`` 的结论。

    ``external_id`` 非空 ⇒ 定位到了；为空时 ``reason`` 给出原因码：
    ``NOT_PROVISIONED``（Keycloak 明确没有该账号，良性）或 ``LOOKUP_MISS``（没核实到，判红）。
    """

    external_id: Optional[str] = None
    reason: Optional[str] = None


class AccountSyncService:
    """账号状态与 Keycloak 双向同步服务。"""

    # ── 出站结果原因码（`sync_status_to_keycloak` 的返回值域）──────────────────
    # ⚠️ 契约：``None`` ⇒ **已送达**；``NOT_PROVISIONED`` ⇒ **无需送达**（良性）；
    # 其余非空原因码 ⇒ **未送达**（只有这一档才落差异清单 + 计 failed_count）。
    # 调用方（identity_integration_sync）据此把「本地已停用但 Keycloak 未停用」
    # 落成差异清单 —— 从前返回 None 且两层 except 吞异常，调用方无从分辨。
    SYNC_REASON_NO_CREDENTIALS = "no_credentials"
    SYNC_REASON_LOOKUP_MISS = "lookup_miss"
    SYNC_REASON_HTTP_ERROR = "http_error"
    #: 良性档：Keycloak **明确答复**没有这个账号（本人从未通过 SSO 登录 ⇒ 无从停用）。
    #: 它不是「未送达」—— 没有可送达的对象；据此判红就是误报。
    SYNC_REASON_NOT_PROVISIONED = "not_provisioned"

    #: 「无需送达」的原因码集合：出现这些 ⇒ **不算差异、不判红**。
    #: ⛔ 保守方向是**不在这个集合里的一律按未送达处置**（含 ``sync_error`` 这类
    #: 调用方自己兜的码），加成员必须有「Keycloak 明确答复」这个前提。
    BENIGN_REASONS = frozenset({SYNC_REASON_NOT_PROVISIONED})

    @classmethod
    def is_not_delivered(cls, reason: Optional[str]) -> bool:
        """判「未送达」：非空原因码且**不在** ``BENIGN_REASONS`` 里。

        调用方（``identity_integration_sync``）据此决定要不要落差异清单 —— 别自己
        写 ``reason == "not_provisioned"`` 的字符串比较，免得多一处会漂移的判据。
        """
        return reason is not None and reason not in cls.BENIGN_REASONS

    def __init__(
        self,
        session: AsyncSession,
        *,
        server_url: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        admin_client: Optional[KeycloakAdminClient] = None,
        timeout: Optional[float] = None,
        audit_service: Optional[object] = None,
    ) -> None:
        self.session = session
        # 审计服务注入点：缺省为 None ⇒ 落回 AuditLogService(self.session)；
        # 测试注入手写替身，无需真实 DB。
        self.audit_service = audit_service
        self._server_url = (
            server_url or os.environ.get(KEYCLOAK_SERVER_URL, "")
        ).rstrip("/")
        self._transport = transport
        self._admin_client = admin_client
        self._timeout = (
            float(timeout)
            if timeout is not None
            else _env_float(KEYCLOAK_TIMEOUT, _DEFAULT_TIMEOUT)
        )
        self._client: Optional[httpx.AsyncClient] = None
        #: 上一次反查是否**核实到**「Keycloak 名单里确实没有这个人」（见
        #: `_resolve_by_case_insensitive_username`）；只在同一次反查内有效。
        self._verified_absent: bool = False

    # ── HTTP 客户端 ──────────────────────────────────────────────────────────

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            )
        return self._client

    def _get_admin_client(self) -> KeycloakAdminClient:
        if self._admin_client is None:
            self._admin_client = get_keycloak_admin_client()
        return self._admin_client

    # ── 凭据来源（占位） ─────────────────────────────────────────────────────

    async def _resolve_realm_admin_credentials(
        self, realm: str, tenant_id: str
    ) -> Optional[RealmAdminCredentials]:
        """解析某 realm 的 realm-admin 凭据。

        占位实现：优先读环境变量注入的 client_id / client_secret；生产应按需从
        ``platform.tenant_realm_map``（client_id）与加密存储 / Secret 解出 secret。
        读不到凭据时返回 ``None``，由调用方降级跳过并告警（不硬编码明文 secret）。
        """
        client_id = os.environ.get(KEYCLOAK_REALM_ADMIN_CLIENT_ID, "").strip()
        client_secret = os.environ.get(KEYCLOAK_REALM_ADMIN_CLIENT_SECRET, "").strip()
        if not client_id or not client_secret:
            return None
        return RealmAdminCredentials(
            realm=realm, client_id=client_id, client_secret=client_secret
        )

    async def _audit_skip(self, tenant_id: str, realm: str, reason: str) -> None:
        """凭据缺失导致账号状态同步降级跳过时写审计（可观测信号，失败不阻塞）。"""
        try:
            from capabilities.platform.models.audit_enums import Outcome
            from capabilities.platform.services.audit_log_service import AuditLogService

            await AuditLogService(self.session).record(
                tenant_id=tenant_id,
                log_type="OPERATION",
                action="identity_sync.missing_admin_credentials",
                outcome=Outcome.FAILED.value,
                resource="user",
                request_params={"realm": realm, "reason": reason},
                error_message=reason,
                sync=True,
            )
        except Exception:  # 审计失败不阻塞账号状态判定
            logger.warning("账号同步降级审计写入失败（不阻塞业务）")

    async def _audit_lookup_miss(
        self, tenant_id: str, realm: str, user, *, absent: bool = False
    ) -> None:
        """反查落空时写审计（失败不阻塞）。

        ⚠️ 这行审计**不是可选项**：反查落空是**预期内**的 —— Keycloak 的 username 与
        平台 ``username`` 可能不一致（broker username 大小写差异，全仓无大小写归一），
        故「查不到」时它是信号；静默跳过等于把停用洞藏起来。

        两种结论分两条 action（``absent=True`` ⇒ 明确没有该账号）：
        「Keycloak 说没这个人」是**查证成功**（SUCCESS，无可停用对象），
        「没问到 Keycloak」才是**失败**（FAILED，可能有洞没堵上）。
        ⛔ 别把两者合回一条 —— 合并后审计页会把良性档也刷成红的。
        """
        reason = (
            "Keycloak 侧没有该账号（从未通过 SSO 登录），无需停用"
            if absent
            else "未能向 Keycloak 核实该账号，停用可能未生效（本地状态照常生效）"
        )
        try:
            from capabilities.platform.models.audit_enums import Outcome

            audit = self.audit_service
            if audit is None:
                from capabilities.platform.services.audit_log_service import AuditLogService

                audit = AuditLogService(self.session)
            await audit.record(
                tenant_id=tenant_id,
                log_type="OPERATION",
                action=(
                    "identity_sync.keycloak_user_absent"
                    if absent
                    else "identity_sync.upstream_user_not_found"
                ),
                outcome=(Outcome.SUCCESS if absent else Outcome.FAILED).value,
                resource="user",
                resource_id=str(getattr(user, "id", "") or "") or None,
                request_params={
                    "realm": realm,
                    "username": getattr(user, "username", None),
                    "reason": reason,
                },
                error_message=None if absent else reason,
                sync=True,
            )
        except Exception:  # 审计失败不阻塞账号状态判定
            logger.warning("账号同步反查落空审计写入失败（不阻塞业务）")

    async def _resolve_missing_external_id(
        self,
        credentials: RealmAdminCredentials,
        realm: str,
        tenant_id: str,
        user,
    ) -> _LookupOutcome:
        """``external_id`` 为空时按 username 反查 Keycloak 本地 user_id。

        场景：KC 侧存在账号，但该人**从未通过本平台 SSO 登录**（登录过的人 JIT 已回填
        ``external_id``）⇒ 平台侧无从知道该停用谁，只能反查。

        查到 ⇒ 回填 ``user.external_id``（值就是 KC 本地 user_id，该列语义不变）；
        查不到 ⇒ 写审计后返回原因码（调用方跳过 PUT，本地 status 照常生效）。

        ⛔ 「查不到」要分两档（本次修复的核心）：Keycloak **明确答复没有这个人**
        ⇒ ``NOT_PROVISIONED``（良性，别判红）；**没问到 Keycloak** ⇒ ``LOOKUP_MISS``
        （判红，可能真有洞）。区分不了就按后者处置 —— 保守方向。
        """
        username = getattr(user, "username", None)
        if not username:
            logger.warning(
                "账号 %s 既缺 external_id 又缺 username，跳过 Keycloak 状态同步",
                getattr(user, "id", "?"),
            )
            # 连反查的键都没有 = 没核实到（不是「确认没人」）⇒ 按未送达留痕。
            await self._audit_lookup_miss(tenant_id, realm, user)
            return _LookupOutcome(reason=self.SYNC_REASON_LOOKUP_MISS)

        resolved = await self._get_admin_client().find_user_id_by_username(
            credentials, username
        )
        if not resolved:
            # 精确反查落空 ⇒ 兜底：拉列表做**不区分大小写**的本地比对。
            # 精确查询（exact=true）区分大小写，而平台 username 与 Keycloak username
            # 常仅大小写不同（broker username）⇒ 只靠它会**恒落空**，静默跳过停用。
            resolved = await self._resolve_by_case_insensitive_username(
                credentials, username
            )
        if resolved:
            # 回填锚点：下次停用免反查（落库由调用方的 flush / 提交完成）。
            user.external_id = resolved
            return _LookupOutcome(external_id=resolved)

        logger.warning(
            "按用户名反查 Keycloak 未命中（realm=%s, user_id=%s），跳过停用并记审计",
            realm,
            getattr(user, "id", "?"),
        )
        absent = self._verified_absent
        await self._audit_lookup_miss(tenant_id, realm, user, absent=absent)
        return _LookupOutcome(
            reason=(
                self.SYNC_REASON_NOT_PROVISIONED
                if absent
                else self.SYNC_REASON_LOOKUP_MISS
            )
        )

    async def _resolve_by_case_insensitive_username(
        self, credentials: RealmAdminCredentials, username: str
    ) -> Optional[str]:
        """兜底定位：拉 Keycloak 用户列表，按 username **不区分大小写**比对。

        **唯一命中**才返回其 ``id``；0 条或**多条**（歧义）一律返回 None —— 停用
        是不可逆的安全动作，宁可留成差异清单让人来看，也不能赌一个停错人。

        副产物 ``self._verified_absent``：本次是否**核实到**「名单里确实没有这个人」。
        ⛔ 只有三条同时成立才置 True（见 `_is_verified_absent`）；拿不到答复 / 名单被
        截断 / 歧义命中都算「没核实到」，调用方据此判红。
        """
        self._verified_absent = False
        wanted = (username or "").strip().lower()
        if not wanted:
            return None
        answered, users = await self._get_admin_client().list_users_checked(
            credentials, max_results=_LIST_PAGE_SIZE
        )
        matched = [
            item
            for item in users
            if str(item.get("username") or "").strip().lower() == wanted
        ]
        if len(matched) != 1:
            self._verified_absent = self._is_verified_absent(answered, users, matched)
            if len(matched) > 1:
                logger.warning(
                    "按用户名兜底比对命中 %d 条（歧义，realm=%s），按落空处理",
                    len(matched),
                    credentials.realm,
                )
            return None
        found = matched[0].get("id")
        if found:
            logger.info("按用户名兜底比对命中（realm=%s），已回填锚点", credentials.realm)
        return found or None

    @staticmethod
    def _is_verified_absent(answered: bool, users: list, matched: list) -> bool:
        """能否下结论「Keycloak 侧确实没有这个账号」。

        三条**同时**成立才算：

        1. 名单拿到了合法答复（不是网络 / HTTP / 报文故障）——否则只是「没问到」；
        2. 名单**没被 ``max_results`` 截断**（条数 < 页大小）——截断说明可能漏了人，
           此时「没搜到」不构成证据；
        3. 不区分大小写比对**零命中**（``matched`` 为空）——多条命中是歧义，
           那是「有账号但分不清是哪个」，比「没有账号」更危险，绝不能判良性。

        差一条一律按「没核实到」处置。
        """
        return answered and len(users) < _LIST_PAGE_SIZE and not matched

    # ── 平台 → Keycloak ─────────────────────────────────────────────────────

    async def sync_status_to_keycloak(self, user, enabled: bool) -> Optional[str]:
        """把本地账号状态同步到 Keycloak ``enabled``。

        据 ``user.tenant_id`` 拼出 realm（``jonex-{tenant_id}``），经 realm-admin
        凭据换取 admin token 后调用 ``PUT /admin/realms/{realm}/users/{external_id}``。

        ``external_id`` 为空时**不再直接放弃**（那会让 KC 侧账号永远停不掉 = 安全洞）：
        先按 username 反查 KC 本地 user_id（精确落空再按大小写归一兜底）——查到则
        停用并回填锚点，查不到则写审计后跳过。
        同步失败（IdP 不可用 / 5xx / 网络异常）只记日志，不抛（分区策略）。

        返回值（契约，见类常量 ``SYNC_REASON_*``）：``None`` ⇒ **已送达**；
        ``NOT_PROVISIONED`` ⇒ **无需送达**（Keycloak 明确没有该账号，良性）；
        其余非空 ⇒ **未送达原因码**。调用方据此判「KC 到底停没停」—— 从前恒回 None，
        「没停成」与「停好了」在调用方看来一模一样，于是静默降级成
        「平台显示已停用、本人仍能登录」。

        ⛔ 调用方判「要不要留痕/判红」请用 :meth:`is_not_delivered`，别写
        ``if reason:`` —— 那会把良性档也刷成差异清单。
        """
        tenant_id = require_tenant(getattr(user, "tenant_id", None))
        external_id = getattr(user, "external_id", None)

        realm = "jonex-" + tenant_id
        try:
            credentials = await self._resolve_realm_admin_credentials(realm, tenant_id)
            if credentials is None:
                logger.warning(
                    "未解析到 realm-admin 凭据（realm=%s），跳过账号状态同步", realm
                )
                await self._audit_skip(
                    tenant_id, realm, "未解析到 realm-admin 凭据，账号状态同步跳过"
                )
                return self.SYNC_REASON_NO_CREDENTIALS
            token = await self._get_admin_client().get_admin_token(credentials)
            if not external_id:
                outcome = await self._resolve_missing_external_id(
                    credentials, realm, tenant_id, user
                )
                if outcome.external_id is None:
                    # 可能是 LOOKUP_MISS（未送达，判红）或 NOT_PROVISIONED（良性）。
                    return outcome.reason or self.SYNC_REASON_LOOKUP_MISS
                external_id = outcome.external_id
            await self._put_user_enabled(realm, external_id, enabled, token)
        except Exception as e:  # noqa: BLE001 —— 分区策略：IdP 不可用不阻塞本地判定
            logger.warning(
                "同步账号状态到 Keycloak 失败（realm=%s, external_id=%s, enabled=%s）：%s",
                realm,
                external_id,
                enabled,
                e,
            )
            return self.SYNC_REASON_HTTP_ERROR
        return None

    async def disable_user(self, user) -> None:
        """停用账号（等价 ``sync_status_to_keycloak(user, False)``，软删等效停用）。"""
        await self.sync_status_to_keycloak(user, False)

    async def _put_user_enabled(
        self, realm: str, external_id: str, enabled: bool, token: AdminToken
    ) -> None:
        url = f"{self._server_url}/admin/realms/{realm}/users/{external_id}"
        headers = {"Authorization": token.authorization_header}
        resp = await self._get_client().put(
            url, json={"enabled": enabled}, headers=headers
        )
        resp.raise_for_status()

    # ── Keycloak → 平台 ─────────────────────────────────────────────────────

    async def apply_keycloak_status(
        self, tenant_id: str, external_id: str, enabled: bool
    ) -> None:
        """Keycloak → 平台方向：更新本地 ``users.status``（enabled → status 1/0）。

        一期只提供入口、不接线触发源：定时轮询与 Keycloak Events 订阅均未实现，
        故这里只做 DB 更新，反向对齐仅在被显式调用时发生。后续任一触发源落地
        （定时轮询，或订阅 ``USER_DISABLED`` 事件并映射为 ``enabled=False``）后
        调用本方法即可完成对齐。失败同样降级不抛。
        """
        try:
            tenant_id = require_tenant(tenant_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("非法租户，跳过 Keycloak 状态回写：%s", e)
            return
        if not external_id:
            logger.warning("external_id 为空，跳过 Keycloak 状态回写")
            return

        status = 1 if enabled else 0
        try:
            result = await self.session.execute(
                select(User).where(
                    User.tenant_id == tenant_id,
                    User.external_id == external_id,
                    User.is_deleted == 0,
                )
            )
            user = result.scalar_one_or_none()
            if user is None:
                logger.warning(
                    "未找到匹配账号（tenant_id=%s, external_id=%s），跳过状态回写",
                    tenant_id,
                    external_id,
                )
                return
            user.status = status
            await self.session.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("回写本地账号状态失败（external_id=%s）：%s", external_id, e)
