#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""存量账号「上游主体标识」回填（一次性运维动作）。

背景：``platform.users.upstream_subject`` 是本次切键（同步/首登按上游主体标识认人）
新增的锚点列。切键之前建的存量账号该列为 ``NULL`` —— 它们**不参与全量缺失核对**，
也认不出上游的人。本服务把它们的锚点补上：

- 扫描面 = 本租户 ``upstream_subject IS NULL`` 且未软删的账号
  （``UserRepository.list_pending_backfill``）⇒ **天然只补空、不覆盖已有值**；
- 逐账号拿 ``users.external_id``（下游账号标识 = Keycloak 本地 user_id）向企业身份侧
  读 ``federated-identity``，解析出上游主体标识；
- 解析到 ⇒ 写入 + 写审计 + 清该账号权限缓存；解析不到 ⇒ 计入 ``skipped``、不中断其余。

设计取舍：

- **best-effort**：单条抛异常只记 warning 并计入 ``skipped``，不回滚整批 —— 回填是运维
  动作，一条坏数据不该让其余几十个账号白跑。
- **写前查占用**：``upstream_subject`` 在租户内唯一（partial unique index）。落库前先
  ``get_by_upstream_subject`` 查该标识是否已被**别的**账号占用，占用则跳过 —— 把唯一约束
  冲突挡在写之前，而不是等 flush 时炸整批。
- **凭据口径复用** ``AccountSyncService._resolve_realm_admin_credentials``：全平台
  「tenant → realm-admin 凭据」只此一处，避免第二条静默分叉的取凭据路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from jonex_core.common import get_logger
from jonex_core.common.realm_util import realm_name_for_tenant
from jonex_core.common.tenant import require_tenant
from jonex_core.integrations.keycloak_admin import (
    KeycloakAdminClient,
    RealmAdminCredentials,
    get_keycloak_admin_client,
)

logger = get_logger("platform.services.upstream_subject_backfill")

_AUDIT_ACTION = "identity_sync.upstream_subject_backfilled"


@dataclass
class BackfillOutcome:
    """一次回填的结果计数：扫描 / 回填 / 跳过。"""

    scanned: int = 0      # 扫描到的 upstream_subject IS NULL 的账号数
    backfilled: int = 0   # 成功回填数
    skipped: int = 0      # 未回填数（缺下游标识 / 上游查不到 / 标识已被占用 / 单条异常）


async def _resolve_credentials(
    session, realm: str, tenant_id: str
) -> RealmAdminCredentials:
    """取 realm-admin 凭据；**读不到时回落 realm-only 空凭据**，不在此处短路。

    为什么不像停用路径那样「凭据缺失 ⇒ 直接降级返回」：出站层 ``get_federated_identity``
    是 **fail-closed** 的（换 token 失败被 ``_admin_get`` 吞成 ``None``），空凭据的净效果
    就是「全部账号计入 ``skipped``」+ 一条 warning —— 与短路等价，但避免了在这里再写一份
    「凭据缺失该怎么降级」的判断，口径不会和停用路径分叉。
    """
    from capabilities.platform.auth.account_sync_service import AccountSyncService

    creds = await AccountSyncService(session)._resolve_realm_admin_credentials(
        realm, tenant_id
    )
    if creds is None:
        logger.warning(
            "未解析到 realm-admin 凭据（realm=%s），回填出站将全部降级为 skipped",
            realm,
        )
        return RealmAdminCredentials(realm=realm, client_id="", client_secret="")
    return creds


async def _record_backfill_audit(
    session, tenant_id: str, realm: str, user
) -> None:
    """写一条「该账号的上游主体标识被变更」的审计（失败不阻塞回填）。

    ⚠️ 必须**在函数内**按模块属性取 ``AuditLogService``：测试通过
    ``monkeypatch.setattr(audit_log_service, "AuditLogService", ...)`` 注入替身，
    模块顶层 ``from ... import AuditLogService`` 会把它绑死成真实类、patch 不生效。
    """
    try:
        from capabilities.platform.models.audit_enums import Outcome
        from capabilities.platform.services import audit_log_service

        await audit_log_service.AuditLogService(session).record(
            tenant_id=tenant_id,
            log_type="OPERATION",
            action=_AUDIT_ACTION,
            outcome=Outcome.SUCCESS.value,
            resource="user",
            resource_id=str(getattr(user, "id", "") or "") or None,
            request_params={"realm": realm, "username": getattr(user, "username", None)},
            sync=True,
        )
    except Exception:  # noqa: BLE001 —— 审计失败不阻塞回填
        logger.warning("回填审计写入失败（不阻塞业务）")


async def backfill_upstream_subject(
    session,
    tenant_id: str,
    *,
    user_repo=None,
    admin_client: Optional[KeycloakAdminClient] = None,
) -> BackfillOutcome:
    """为存量账号批量回填上游主体标识。

    Args:
        session: 异步 DB 会话（提交权在调用方）。
        tenant_id: 目标租户；经 ``require_tenant`` 校验。
        user_repo: 缺省自建 ``UserRepository(session)``；测试注入假仓储。
        admin_client: 缺省取 ``get_keycloak_admin_client()`` 单例；测试注入替身。

    Returns:
        :class:`BackfillOutcome` —— ``scanned`` / ``backfilled`` / ``skipped``。
    """
    from capabilities.platform.repository.user_repository import UserRepository

    tenant_id = require_tenant(tenant_id)
    if user_repo is None:
        user_repo = UserRepository(session)
    if admin_client is None:
        admin_client = get_keycloak_admin_client()

    users = list(await user_repo.list_pending_backfill(tenant_id))
    outcome = BackfillOutcome(scanned=len(users))
    if not users:
        return outcome

    realm = realm_name_for_tenant(tenant_id)
    credentials = await _resolve_credentials(session, realm, tenant_id)

    for user in users:
        try:
            kc_user_id = getattr(user, "external_id", None)
            if not kc_user_id:
                # 上游主体标识与下游账号标识都缺 ⇒ 没有任何去企业身份侧的抓手。
                logger.info(
                    "回填跳过账号 %s：既无下游账号标识，无从向上游解析",
                    getattr(user, "username", "?"),
                )
                outcome.skipped += 1
                continue

            subject = await admin_client.get_federated_identity(credentials, kc_user_id)
            if not subject:
                logger.info(
                    "回填跳过账号 %s：企业身份侧未返回上游主体标识",
                    getattr(user, "username", "?"),
                )
                outcome.skipped += 1
                continue

            owner = await user_repo.get_by_upstream_subject(tenant_id, subject)
            if owner is not None and getattr(owner, "id", None) != getattr(user, "id", None):
                # 租户内唯一：该标识已被别的账号占着，写下去必撞 partial unique index。
                logger.warning(
                    "回填跳过账号 %s：解析到的上游主体标识已被另一账号占用",
                    getattr(user, "username", "?"),
                )
                outcome.skipped += 1
                continue

            user.upstream_subject = subject
            outcome.backfilled += 1
            await _record_backfill_audit(session, tenant_id, realm, user)

            try:
                from jonex_core.security.permission import invalidate_user_permissions

                await invalidate_user_permissions(tenant_id, getattr(user, "id", None))
            except Exception:  # noqa: BLE001 —— 缓存清理失败不阻塞回填
                logger.warning("回填后清理权限缓存失败（不阻塞业务）")
        except Exception as e:  # noqa: BLE001 —— best-effort：单条失败不拖垮整批
            logger.warning(
                "回填账号 %s 失败，跳过并继续：%s",
                getattr(user, "username", "?"),
                e,
            )
            outcome.skipped += 1

    # ⛔ 这里**不**调 session.flush()：审计走 `AuditLogService.record(sync=True)`，
    # 它自己会往同一个 session 里 add(AuditLog) 再 flush；真审计写入失败时（无 DB 等）
    # 那步异常被它内部吞掉，但**残留的日志对象还挂在 session 上** —— 此时我们再 flush
    # 一次就会把它一起冲出去、把一次「审计降级」放大成回填整批失败。
    # 属性赋值已落在 ORM 对象上，提交权照例交调用方（与同步侧 _apply_single_user 同口径）。
    logger.info(
        "上游主体标识回填完成（tenant=%s）：扫描 %d、回填 %d、跳过 %d",
        tenant_id,
        outcome.scanned,
        outcome.backfilled,
        outcome.skipped,
    )
    return outcome


__all__ = ["BackfillOutcome", "backfill_upstream_subject"]
