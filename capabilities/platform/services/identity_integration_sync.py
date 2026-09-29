#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""企业身份同步服务（同步任务状态机 + 用户同步引擎）。

职责：
- 同步任务状态机（SyncTaskStatus）：pending → running → success / partial_failed /
  failed；非法转移（同步中重复发起、终态回退、running 期间重复发起）→
  SyncTaskStateError(4005)。
- 立即同步（mode=incremental/full）：创建任务 → 执行 → 终态。
- 并发防重：(tenant_id) WHERE status='running' 部分唯一索引 + 状态机双兜底。
- 用户同步引擎：
  - username / display_name / email / status 主数据源赢；
  - role Jonex赢，同步不改角色；
  - external_id = external_user_id（= Keycloak sub），作为唯一匹配键（username 仅作
    存量「external_id 为空」影子用户的回填兜底，不再作主匹配键）；
  - 离职/停用 → status=0 不物理删除；
  - 全量缺失 → **当轮再拉一次 full 复核**；复核也缺才软删（is_deleted=1，可原地复活），
    复核拿不到结果则一个都不删、只记一条失败；上游恢复同一个人 → 原地复活；
  - 全量缺失、软删、复活不新增对外端点，仍是同一同步链路。
  - 同一 external_user_id 映射多账号 / username 已绑定其他 external_id（同名不同人）
    → 记失败明细，拒绝合并、不重复建号。
- 计数：user_added / user_updated / user_disabled / user_enabled / user_deleted /
  failed_count 准确；
  success ⇔ failed_count=0、partial_failed ⇔ failed_count>0。
- 失败明细逐条落 IdentitySyncFailure（entity_type=user、external_entity_id、
  failure_reason 脱敏）。
- 审计：同步写 audit（sync_identity_integration + ResourceType.IDENTITY_SYNC）。

同步执行模型：本服务在请求内同步执行（await）直至终态——「异步任务」的调度（自动
同步周期）复用 task_schedules；单次「立即同步」同步完成即可保证
返回终态 task 状态，同时状态机 + 部分唯一索引兜底并发重复发起。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.crypto import decrypt_secret
from jonex_core.common.exceptions import (
    DataSourceAuthError,
    DataSourceConnectionError,
    DataSourceContractError,
    DataSourceNotVerifiedError,
    InvalidParameterError,
    ResourceNotFoundError,
    ServiceTimeoutError,
    SyncTaskStateError,
)
from jonex_core.common.i18n import translate
from jonex_core.common.logger import get_logger
from jonex_core.common.tenant import require_tenant
from jonex_core.security.outbound_url_validator import validate_datasource_url

from capabilities.platform.dtos.identity_integration import (
    SyncFailureDetail,
    SyncRecordDetailResponse,
    SyncRecordListResponse,
    SyncRecordSummary,
    SyncResponse,
    UserChangeCounts,
)
from capabilities.platform.models.identity_integration import (
    IdentityIntegrationConfig,
    IdentitySyncFailure,
    IdentitySyncTask,
    SyncEntityType,
    SyncTaskStatus,
    SyncTrigger,
    SyncType,
)
from capabilities.platform.models.user import User
from capabilities.platform.repository.user_repository import UserRepository
from capabilities.platform.services.identity_sync_client import (
    ExternalUser,
    IdentitySyncClient,
    SyncFetchResult,
)
from capabilities.platform.services.role_binding import ensure_default_role_binding
from capabilities.platform.services.user_service import UserService

logger = get_logger(__name__)

# 状态机：终态不可回退，running 期间禁止重复发起/回退。
_TERMINAL = {
    SyncTaskStatus.SUCCESS,
    SyncTaskStatus.PARTIAL_FAILED,
    SyncTaskStatus.FAILED,
    SyncTaskStatus.CANCELED,
}

_ALLOWED: Dict[SyncTaskStatus, set] = {
    SyncTaskStatus.PENDING: {
        SyncTaskStatus.RUNNING,
        SyncTaskStatus.FAILED,
        SyncTaskStatus.CANCELED,
    },
    SyncTaskStatus.RUNNING: {
        SyncTaskStatus.SUCCESS,
        SyncTaskStatus.PARTIAL_FAILED,
        SyncTaskStatus.FAILED,
        SyncTaskStatus.CANCELED,
    },
}

# 整体失败（连接/认证/契约/超时）→ 失败明细的脱敏原因（技术细节不进消息）。
_WHOLE_FAILURE_REASONS = {
    DataSourceConnectionError: "主数据源连接失败",
    DataSourceAuthError: "主数据源认证失败",
    DataSourceContractError: "主数据源数据契约不符",
    ServiceTimeoutError: "主数据源请求超时",
}

# 全量缺失**复核未完成**的脱敏原因（唯一会因「缺失」而产生的失败明细文案）。
# ⛔ 只有「复核拿不到结果」才用它；复核确认上游已删 ⇒ 走软删，不记失败。
# 旧文案「全量同步缺失，待确认（不自动停用）」已随本变更删除。
_REVIEW_FAILED_REASON = "全量缺失复核未完成，本次未删除任何账号"
# 身份标识映射冲突的脱敏原因：同一 external_user_id 映射多账号 / username 已绑定其他
# external_id（同名不同人）。均拒绝合并、不覆盖、不重复建号。
_CONFLICT_REASON = "企业身份标识映射冲突，已拒绝合并"
# 波 3 门控：主数据源未通过连接测试 ⇒ 自动同步被跳过（写一条 failed 记录当可见出口）。
_GATE_BLOCK_REASON = "未通过连接测试，本次自动同步已跳过"

_BATCH_LIMIT = 100000  # 全量缺失核对时拉取存量用户的安全上限


class IdentityIntegrationSyncService:
    """企业身份同步服务。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        client_factory: Optional[Any] = None,
    ) -> None:
        self.session = session
        self.user_repo = UserRepository(session)
        self.user_service = UserService(session)
        self._client_factory = client_factory

    # ── 出站客户端装配 ────────────────────────────────────────────────────────

    async def _build_client(self, base_url: str, token: str):
        """构造出站客户端；测试可注入 client_factory 替换。"""
        if self._client_factory is not None:
            return await self._client_factory(base_url, token)
        return IdentitySyncClient(base_url, token)

    # ── 状态机 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _transition(task: IdentitySyncTask, target: SyncTaskStatus) -> None:
        """执行一次状态转移；非法转移抛 SyncTaskStateError(409)。"""
        current = (
            task.status
            if isinstance(task.status, SyncTaskStatus)
            else SyncTaskStatus(task.status)
        )
        target = (
            target if isinstance(target, SyncTaskStatus) else SyncTaskStatus(target)
        )
        if current in _TERMINAL:
            raise SyncTaskStateError(
                message=translate(
                    "err.identity_sync.illegal_transition",
                    fallback="同步任务已处于终态，不可再变更状态",
                )
            )
        if target not in _ALLOWED.get(current, set()):
            raise SyncTaskStateError(
                message=translate(
                    "err.identity_sync.illegal_transition",
                    fallback="同步任务状态非法转移",
                )
            )
        task.status = target.value

    async def _ensure_no_running(self, tenant_id: str) -> None:
        """存在 running 态任务时抛 SyncTaskStateError（并发防重，状态机兜底）。"""
        result = await self.session.execute(
            select(IdentitySyncTask.id)
            .where(
                IdentitySyncTask.tenant_id == tenant_id,
                IdentitySyncTask.status == SyncTaskStatus.RUNNING.value,
                IdentitySyncTask.is_deleted == 0,
            )
            .limit(1)
        )
        if result.scalar_one_or_none() is not None:
            raise SyncTaskStateError(
                message=translate(
                    "err.identity_sync.already_running",
                    fallback="同步进行中，禁止重复发起",
                )
            )

    # ── 配置 / 游标 ───────────────────────────────────────────────────────────

    async def _load_config(self, tenant_id: str) -> IdentityIntegrationConfig:
        result = await self.session.execute(
            select(IdentityIntegrationConfig).where(
                IdentityIntegrationConfig.tenant_id == tenant_id
            )
        )
        config = result.scalar_one_or_none()
        if config is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.identity_integration.not_configured",
                    fallback="尚未配置企业身份集成主数据源",
                )
            )
        return config

    async def _last_cursor(self, tenant_id: str) -> Optional[str]:
        """增量同步的起始游标：最近一次成功/部分成功任务持久化的 cursor。"""
        result = await self.session.execute(
            select(IdentitySyncTask.cursor)
            .where(
                IdentitySyncTask.tenant_id == tenant_id,
                IdentitySyncTask.status.in_(
                    [
                        SyncTaskStatus.SUCCESS.value,
                        SyncTaskStatus.PARTIAL_FAILED.value,
                    ]
                ),
                IdentitySyncTask.cursor.isnot(None),
                IdentitySyncTask.is_deleted == 0,
            )
            .order_by(IdentitySyncTask.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    # ── 立即同步 ──────────────────────────────────────────────────────────

    async def trigger_sync(
        self,
        tenant_id: str,
        mode: str,
        *,
        trigger: str = SyncTrigger.MANUAL.value,
        operator_user_id: Optional[int] = None,
        operator_username: Optional[str] = None,
        force: bool = False,
    ) -> SyncResponse:
        """立即同步：创建任务 → 执行 → 终态，返回 task_id + status。

        同步中重复发起 / 终态回退 → SyncTaskStateError(409)；整体失败（连接/认证/
        契约/超时）不抛给调用方，任务记 failed 并落失败明细。

        `force=True` 绕过波 3 的「未通过连接测试」门控（手动路径的二次确认用）；自动
        路径不传它，因此恒被闸门拦下 —— 这就是契约要求的「自动同步禁且不给绕过」。
        """
        tenant_id = require_tenant(tenant_id)
        mode_enum = self._coerce_mode(mode)
        trigger_enum = SyncTrigger(trigger) if isinstance(trigger, str) else trigger

        # 1) 并发防重 + 状态机：存在 running → 409（不排队、不叠加）
        await self._ensure_no_running(tenant_id)

        # 2) 配置 + SSRF 校验
        config = await self._load_config(tenant_id)
        url = (config.datasource_url or "").strip()
        if not url:
            raise InvalidParameterError(
                message=translate(
                    "err.identity_integration.datasource_url_missing",
                    fallback="未配置主数据源地址",
                )
            )
        token = decrypt_secret(config.token_ciphertext or "")
        validate_datasource_url(url)

        # 2.5) 波 3 门控：主数据源未通过连接测试 ⇒ 拒绝同步（手动需 force 二次确认）。
        #      位置在「地址非空校验(1001)」**之后** —— 地址为空是更早、更具体的错，
        #      不应被 5008 抢先。
        bypassed = False
        if config.datasource_connection_verified_at is None:
            if not force:
                raise DataSourceNotVerifiedError()
            bypassed = True

        # 3) 创建任务 pending
        task = IdentitySyncTask(
            tenant_id=tenant_id,
            status=SyncTaskStatus.PENDING.value,
            sync_type=mode_enum.value,
            trigger=trigger_enum.value,
        )
        self.session.add(task)
        await self.session.flush()

        # 4) pending → running
        self._transition(task, SyncTaskStatus.RUNNING)
        task.started_at = datetime.now()
        await self.session.flush()

        # 5) 执行 → 终态
        await self._run(task, tenant_id, mode_enum, url, token)

        # 6) 审计（失败不阻塞同步主流程；`bypassed` 让「这是绕过门控」可辨认）
        await self._write_audit(
            task,
            tenant_id,
            operator_user_id,
            operator_username,
            bypassed=bypassed,
        )

        return SyncResponse(task_id=task.id, status=task.status)

    async def _run(
        self,
        task: IdentitySyncTask,
        tenant_id: str,
        mode: SyncType,
        url: str,
        token: str,
    ) -> None:
        client = None
        try:
            cursor = None
            if mode == SyncType.INCREMENTAL:
                cursor = await self._last_cursor(tenant_id)
            client = await self._build_client(url, token)
            result: SyncFetchResult = await client.fetch_users(mode.value, cursor=cursor)
            # 同一个 client 交给用户同步引擎：全量缺失的「同轮复核」要用它再拉一次，
            # ⛔ 不新建连接、不新增超时配置（沿用本客户端既有超时）。
            await self._apply_users(task, tenant_id, mode, result.users, client=client)
            task.cursor = result.cursor
            task.finished_at = datetime.now()
            self._transition(
                task,
                SyncTaskStatus.SUCCESS
                if task.failed_count == 0
                else SyncTaskStatus.PARTIAL_FAILED,
            )
        except (
            DataSourceConnectionError,
            DataSourceAuthError,
            DataSourceContractError,
            ServiceTimeoutError,
        ) as exc:
            task.finished_at = datetime.now()
            self._transition(task, SyncTaskStatus.FAILED)
            await self._record_failure(
                task,
                SyncEntityType.USER.value,
                None,
                _WHOLE_FAILURE_REASONS.get(type(exc), "主数据源拉取失败"),
            )
        except Exception:  # noqa: BLE001 —— 保证任务不卡在 running，落终态失败
            logger.exception("同步执行异常（tenant=%s）", tenant_id)
            task.finished_at = datetime.now()
            self._transition(task, SyncTaskStatus.FAILED)
            await self._record_failure(task, SyncEntityType.USER.value, None, "同步执行异常")
        finally:
            if client is not None and hasattr(client, "aclose"):
                try:
                    await client.aclose()
                except Exception:  # noqa: BLE001
                    logger.warning("关闭主数据源客户端失败", exc_info=True)
            await self.session.flush()

    @staticmethod
    def _coerce_mode(mode: str) -> SyncType:
        if isinstance(mode, SyncType):
            return mode
        if mode not in {e.value for e in SyncType}:
            raise InvalidParameterError(
                message=translate(
                    "err.identity_sync.invalid_mode",
                    fallback=f"非法同步模式: {mode}",
                )
            )
        return SyncType(mode)

    # ── 用户同步引擎 ──────────────────────────────────────────────────────────

    async def _apply_users(
        self,
        task: IdentitySyncTask,
        tenant_id: str,
        mode: SyncType,
        external_users: list,
        *,
        client=None,
    ) -> None:
        """应用一批企业用户：冲突裁决 + 建号/更新 + 全量缺失核对。

        ``client`` 是**首拉用的同一个出站客户端**，只在全量缺失复核时复用；
        传 None（如直连单测）⇒ 复核拿不到结果 ⇒ 走「一个都不删 + 记一条失败」。
        """
        seen_ext_to_username: Dict[str, str] = {}
        # ⚠️ seen 集合按**上游主体标识**收集（键与主匹配键同源）：全量缺失核对只拿它比对，
        # 若仍按 username 收集，上游给某人改了名就会凭空产一条「缺失」明细。
        seen_subjects = set()

        for eu in external_users:
            prev_username = seen_ext_to_username.get(eu.external_user_id)
            if prev_username is None:
                seen_ext_to_username[eu.external_user_id] = eu.username
                seen_subjects.add(eu.external_user_id)
                await self._apply_single_user(task, tenant_id, eu)
                continue
            if prev_username != eu.username:
                # 同一 external_user_id 映射到不同账号 → 记失败明细，不重复建号
                await self._record_failure(
                    task, SyncEntityType.USER.value, eu.external_user_id, _CONFLICT_REASON
                )
                continue
            # 完全重复（同 external_user_id 同 username）→ 幂等去重，静默跳过

        if mode == SyncType.FULL:
            await self._apply_full_missing(
                task, tenant_id, seen_subjects, client=client
            )

    async def _apply_single_user(
        self, task: IdentitySyncTask, tenant_id: str, eu: ExternalUser
    ) -> None:
        """建号或更新单个用户（上游主体标识为唯一匹配键，字段权威裁决）。

        匹配顺序（与 J 侧统一，入口不同）：
        ⓪ 曾因全量缺失被软删、上游又出现了 → **原地复活**（保留 id / 角色 / 数据），
           带 username 占用护栏（撞名则拒绝复活、记冲突）
        ① ``upstream_subject`` 命中 → 复用（不碰角色）
        ② ``external_id`` 命中 → 复用 + 回填 ``upstream_subject``
        ③ ``username`` 兜底 → 两道护栏（``password_hash`` 非空 / 锚点非空且不符）→ 拒绝合并
        ④ 都未命中 → 建号（写 ``upstream_subject``，⛔ 不写 ``external_id``）

        ⛔ ``external_id`` 语义冻结（= Keycloak ``sub``，被 ``account_sync_service`` 用来拼
        停用 URL）：同步入参 ``eu.external_user_id`` 承载的是**上游主体标识**，只能落
        ``upstream_subject``；写进 ``external_id`` 会让停用打到假 id 上（静默失效 = 安全洞）。
        """
        new_status = 1 if eu.status == "active" else 0

        # 0) 复活：同一上游主体标识存在**已被软删**的行 ⇒ 原地复活，不新建。
        #    为什么必须有这一步：软删行不在任何 partial unique index 的谓词内，
        #    而 ①②③ 用的都是「is_deleted == 0」的查询 —— 没有这一步，上游恢复同一个人
        #    会认不出旧行、走 ④ 建出第二个 id 的账号（原 id / 角色 / 数据全丢）。
        restored = await self.user_repo.get_by_upstream_subject_including_deleted(
            tenant_id, eu.external_user_id
        )
        if restored is not None and restored.is_deleted:
            # 护栏（用户裁定 C3）：同名活跃账号已存在且不是这一行 ⇒ 拒绝复活。
            #    复活会把 is_deleted 拨回 0、该行重新进入 username 的 partial unique
            #    index —— 撞上会让整轮同步在 flush 处崩掉。业务上「上游会处理重名，
            #    Jonex只跟随」，此护栏**只为防撞库**，不是另一条业务规则。
            #    ⚠️ 判据用**本地行的 username**（复活不改 username：「资料保留原值」），
            #    那才是索引键。
            taken = await self.user_repo.get_by_username(tenant_id, restored.username)
            if taken is not None and taken.id != restored.id:
                await self._record_failure(
                    task, SyncEntityType.USER.value, eu.external_user_id, _CONFLICT_REASON
                )
                return
            restored.is_deleted = 0
            restored.status = new_status
            # 复活计入既有 user_enabled 口径（F4）：不新增列、不新增前端数字。
            task.user_enabled += 1
            await self._restore_user_side_effects(task, tenant_id, restored, new_status)
            updated = False
            if eu.name is not None and restored.display_name != eu.name:
                restored.display_name = eu.name
                updated = True
            if eu.email is not None and restored.email != eu.email:
                restored.email = eu.email
                updated = True
            if updated:
                task.user_updated += 1
            await self.session.flush()
            return

        # 1) 主匹配键 = upstream_subject：命中直接更新，不依赖 username、不新建。
        existing = await self.user_repo.get_by_upstream_subject(
            tenant_id, eu.external_user_id
        )
        if existing is not None:
            await self._update_existing_user(task, tenant_id, eu, existing, new_status)
            return

        # 2) upstream_subject 未命中：按 external_id（= KC sub）复用并回填上游锚点
        #    （存量账号首登时已由 JIT 回填过 sub，此处补齐上游侧）。
        existing = await self.user_repo.get_by_external_id(
            tenant_id, eu.external_user_id
        )
        if existing is not None:
            if existing.upstream_subject is None:
                existing.upstream_subject = eu.external_user_id
            await self._update_existing_user(task, tenant_id, eu, existing, new_status)
            return

        # 3) 兜底：按 username 匹配存量迁移账号 —— 两道护栏，任一不过即拒绝合并。
        by_username = await self.user_repo.get_by_username(tenant_id, eu.username)
        if by_username is not None:
            # 护栏 1：本地密码账号（password_hash 非空）⇒ 绝不合并，否则等于把本地账号
            # 连同其密码语义交出去（防账号接管）。
            if by_username.password_hash is not None:
                await self._record_failure(
                    task, SyncEntityType.USER.value, eu.external_user_id, _CONFLICT_REASON
                )
                return
            # 护栏 2：锚点非空且对不上 ⇒ 同名不同人，拒绝合并、不覆盖。
            #    ⚠️ 判据必须是 upstream_subject（不是 external_id）—— 存量账号的
            #    external_id 可能仍是旧契约写下的假 id，拿它判会让老用户每次同步都判冲突、
            #    永不回填（这处最易漏）。
            if by_username.upstream_subject is not None:
                await self._record_failure(
                    task, SyncEntityType.USER.value, eu.external_user_id, _CONFLICT_REASON
                )
                return
            by_username.upstream_subject = eu.external_user_id
            await self._update_existing_user(task, tenant_id, eu, by_username, new_status)
            return

        # 4) 新建：只写 upstream_subject；external_id 留空（等本人 SSO 首登 JIT 回填 sub）；
        #    角色归Jonex权威，默认 user；password_hash 置 NULL（密码校验上移 Keycloak）。
        user = User(
            tenant_id=tenant_id,
            username=eu.username,
            external_id=None,
            upstream_subject=eu.external_user_id,
            password_hash=None,
            display_name=eu.name,
            email=eu.email,
            role="user",
            status=new_status,
        )
        self.session.add(user)
        await self.session.flush()
        # 建号后补默认角色绑定（否则新用户零角色、权限集为空、侧边栏全空，
        # 且全程 HTTP 200）。角色缺失时降级为 warning 不抛异常，不污染整批同步。
        # 上面三个命中分支（upstream_subject / external_id / username 兜底）与
        # _update_existing_user 一律不动角色：同步不改角色（Jonex权威）。
        await ensure_default_role_binding(
            self.user_service, tenant_id, user.id, source="identity_sync"
        )
        task.user_added += 1

    async def _update_existing_user(
        self,
        task: IdentitySyncTask,
        tenant_id: str,
        eu: ExternalUser,
        existing: User,
        new_status: int,
    ) -> None:
        """更新存量用户：status / display_name / email 主数据源赢，role 不碰，username 不覆盖。"""
        if existing.status == 1 and new_status == 0:
            # 离职/停用 → 停用不物理删除；并即时吊销会话（对齐 disable 四件套）
            existing.status = 0
            task.user_disabled += 1
            await self._disable_user_side_effects(task, tenant_id, existing)
        else:
            # 启用是独立动作：只计 user_enabled，**不混计** user_updated（两者可同时 +1：
            # 状态启用 + 资料变更各算各的）。副效应走启用三件套（清权限缓存 + 推 Keycloak
            # enabled=true + 审计），⛔ 刻意不吊销会话 —— 与停用四件套的不对称点。
            if new_status == 1 and existing.status != 1:
                existing.status = 1
                task.user_enabled += 1
                await self._enable_user_side_effects(task, tenant_id, existing)
            updated = False
            if eu.name is not None and existing.display_name != eu.name:
                existing.display_name = eu.name
                updated = True
            if eu.email is not None and existing.email != eu.email:
                existing.email = eu.email
                updated = True
            if updated:
                task.user_updated += 1
        await self.session.flush()

    async def _disable_user_side_effects(
        self, task: IdentitySyncTask, tenant_id: str, user: User
    ) -> None:
        """停用副效应（对齐 UserService.disable 四件套）：清权限缓存 + 同步 Keycloak
        enabled=false + 吊销 refresh 家族 + 审计。同步/吊销/审计失败降级不阻塞，
        绝不因 IdP / Redis 不可用阻塞本地停用判定（主数据源权威，离职即停用）。
        """
        from jonex_core.security.permission import invalidate_user_permissions

        # invalidate 内部吞异常故放 try 外；同步/吊销失败降级不抛（与 disable 一致）。
        await invalidate_user_permissions(tenant_id, user.id)
        kc_reason: Optional[str] = None
        try:
            from capabilities.platform.auth.account_sync_service import AccountSyncService
            from capabilities.platform.auth.auth_service import AuthService

            kc_reason = await AccountSyncService(self.session).sync_status_to_keycloak(
                user, False
            )
            await AuthService(self.session).revoke_user_sessions(tenant_id, user.id)
        except Exception:  # 同步/吊销失败不阻塞业务
            logger.exception("身份集成停用同步 Keycloak / 吊销会话失败: user_id=%s", user.id)
            # 抛异常 = KC 侧状态未知，与「明确未送达」同档：都要留痕，不得静默。
            kc_reason = kc_reason or "sync_error"
        if kc_reason:
            # ⛔ 降级≠静默：本地照常停用（主数据源权威），但「本地已停用、KC 未停用」
            # 这件事必须可被看见 —— 落差异清单并计入 failed_count（⇒ partial_failed）。
            await self._record_kc_not_applied(task, user, kc_reason)
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService

            await AuditLogService(self.session).record(
                tenant_id=tenant_id,
                log_type="OPERATION",
                action="identity_sync.user_disabled",
                user_id=None,
                resource="user",
                resource_id=str(user.id),
                request_params={
                    "source": "identity_sync",
                    "sync_task_id": task.id,
                    "username": user.username,
                },
                sync=True,
            )
        except Exception:  # 审计失败不阻塞业务
            logger.exception("身份集成停用审计日志写入失败: user_id=%s", user.id)

    async def _record_kc_not_applied(
        self,
        task: IdentitySyncTask,
        user: User,
        reason_code: str,
        *,
        deleted: bool = False,
    ) -> None:
        """把「本地已停用/已删除但 Keycloak 未跟随」落成同步记录的失败明细（差异清单）。

        `deleted=True` 走软删侧文案（「本地已删除但 Keycloak 未删除」）；停用侧原样不改。

        与 `AccountSyncService._audit_lookup_miss` 的分工：那条是**出站层**的即时
        审计信号；本条是**同步记录**的差异清单（前端「同步记录详情」看得到），
        并计入 `failed_count` ⇒ `_run` 把整次同步收敛为 `partial_failed`，
        不再以一个 success 掩盖「离职人员的认证入口没关」。
        """
        from capabilities.platform.auth.account_sync_service import (
            KC_SYNC_REASON_TEXT,
            AccountSyncService,
        )

        if not AccountSyncService.is_not_delivered(reason_code):
            # 良性档（``not_provisioned``）：Keycloak 明确没有这个账号 ⇒ 本来就无可
            # 停用的对象，不是「未送达」。落进差异清单只会刷一条无事可做的红字，
            # 把真告警淹掉（本地该停用/软删照旧，只是不留痕判红）。
            logger.info(
                "Keycloak 侧无此账号，无需停用（reason=%s, username=%s）",
                reason_code,
                user.username,
            )
            return

        detail = KC_SYNC_REASON_TEXT.get(reason_code, "同步 Keycloak 时发生异常")
        prefix = (
            "本地已删除但 Keycloak 未删除" if deleted else "本地已停用但 Keycloak 未停用"
        )
        await self._record_failure(
            task,
            SyncEntityType.USER.value,
            user.username,
            f"{prefix}：{detail}",
        )

    async def _enable_user_side_effects(
        self, task: IdentitySyncTask, tenant_id: str, user: User
    ) -> None:
        """启用副效应（对齐 UserService.enable 三件套）：清权限缓存 + 同步 Keycloak
        enabled=true + 审计。

        ⛔ **刻意不吊销会话**（与 `_disable_user_side_effects` 四件套的不对称点）：照抄
        停用分支会把刚启用的人立刻踢下线。同步/审计失败降级不阻塞，绝不因 IdP 不可用
        阻塞本地启用判定（主数据源权威，在职即启用）。
        """
        from jonex_core.security.permission import invalidate_user_permissions

        # invalidate 内部吞异常故放 try 外；同步失败降级不抛（与 disable 一致）。
        await invalidate_user_permissions(tenant_id, user.id)
        try:
            from capabilities.platform.auth.account_sync_service import AccountSyncService

            await AccountSyncService(self.session).sync_status_to_keycloak(user, True)
        except Exception:  # 同步失败不阻塞业务
            logger.exception("身份集成启用同步 Keycloak 失败: user_id=%s", user.id)
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService

            await AuditLogService(self.session).record(
                tenant_id=tenant_id,
                log_type="OPERATION",
                action="identity_sync.user_enabled",
                user_id=None,
                resource="user",
                resource_id=str(user.id),
                request_params={
                    "source": "identity_sync",
                    "sync_task_id": task.id,
                    "username": user.username,
                },
                sync=True,
            )
        except Exception:  # 审计失败不阻塞业务
            logger.exception("身份集成启用审计日志写入失败: user_id=%s", user.id)

    async def _soft_delete_user(
        self, task: IdentitySyncTask, tenant_id: str, user: User
    ) -> None:
        """软删一个被复核确认「上游已删」的账号（契约 §2.2）。

        顺序对齐 `_disable_user_side_effects` 四件套，**只多一件事**：置 ``is_deleted``。

        ⛔ **只动 ``is_deleted``，不动 ``status``**（对齐 `UserService.delete`）：
        软删是「这条账号不存在了」，不是「这个人离职了」。上游恢复 ⇒ 原地复活。
        """
        from jonex_core.security.permission import invalidate_user_permissions

        user.is_deleted = 1
        # invalidate 内部吞异常故放 try 外；同步/吊销失败降级不抛（与停用一致）。
        await invalidate_user_permissions(tenant_id, user.id)
        kc_reason: Optional[str] = None
        try:
            from capabilities.platform.auth.account_sync_service import AccountSyncService
            from capabilities.platform.auth.auth_service import AuthService

            kc_reason = await AccountSyncService(self.session).sync_status_to_keycloak(
                user, False
            )
            await AuthService(self.session).revoke_user_sessions(tenant_id, user.id)
        except Exception:  # 同步/吊销失败不阻塞业务
            logger.exception("身份集成软删同步 Keycloak / 吊销会话失败: user_id=%s", user.id)
            # 抛异常 = KC 侧状态未知，与「明确未送达」同档：都要留痕，不得静默。
            kc_reason = kc_reason or "sync_error"
        if kc_reason:
            await self._record_kc_not_applied(task, user, kc_reason, deleted=True)
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService

            await AuditLogService(self.session).record(
                tenant_id=tenant_id,
                log_type="OPERATION",
                action="identity_sync.user_deleted",
                user_id=None,
                resource="user",
                resource_id=str(user.id),
                request_params={
                    "source": "identity_sync",
                    "sync_task_id": task.id,
                    "username": user.username,
                },
                sync=True,
            )
        except Exception:  # 审计失败不阻塞业务
            logger.exception("身份集成软删审计日志写入失败: user_id=%s", user.id)
        task.user_deleted += 1

    async def _restore_user_side_effects(
        self,
        task: IdentitySyncTask,
        tenant_id: str,
        user: User,
        new_status: int,
    ) -> None:
        """原地复活的副效应：清权限缓存 + 推 Keycloak + 审计（契约 §2.4）。

        ⛔ **刻意不吊销会话**（与启用侧同形的不对称点）：照抄软删/停用会把刚复活的人
        立刻踢下线。推送的 enabled 由上游状态决定（``new_status == 1`` ⇒ true）。
        推送/审计失败降级不阻塞 —— 主数据源权威，人回来了就该在Jonex可见。
        """
        from jonex_core.security.permission import invalidate_user_permissions

        await invalidate_user_permissions(tenant_id, user.id)
        try:
            from capabilities.platform.auth.account_sync_service import AccountSyncService

            await AccountSyncService(self.session).sync_status_to_keycloak(
                user, new_status == 1
            )
        except Exception:  # 同步失败不阻塞业务
            logger.exception("身份集成复活同步 Keycloak 失败: user_id=%s", user.id)
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService

            await AuditLogService(self.session).record(
                tenant_id=tenant_id,
                log_type="OPERATION",
                action="identity_sync.user_restored",
                user_id=None,
                resource="user",
                resource_id=str(user.id),
                request_params={
                    "source": "identity_sync",
                    "sync_task_id": task.id,
                    "username": user.username,
                },
                sync=True,
            )
        except Exception:  # 审计失败不阻塞业务
            logger.exception("身份集成复活审计日志写入失败: user_id=%s", user.id)

    async def _apply_full_missing(
        self,
        task: IdentitySyncTask,
        tenant_id: str,
        seen_subjects: set,
        *,
        client=None,
    ) -> None:
        """全量缺失核对：候选 → **当轮复核** → 复核也缺才软删（契约 §2.1）。

        比对键 = ``upstream_subject``（与主匹配键同源）：上游改了 username 而人没变
        不算缺失。⚠️ 删的是 ``user`` 这一行（``is_deleted=1``），不改 ``status``。

        ⛔ **安全不变量：拿不到复核结果就绝不删人**。复核客户端不可用 / 抛异常 /
        报文不可解析 ⇒ 一个账号都不删，只记一条失败（「复核未完成」）让整次红一次
        —— 「拿不到」不等于「人不在」，凭一次观测删账号是不可接受的。

        ⛔ **缺失本身不再计 failed_count**（D2）：复核确认上游已删 ⇒ 走软删（计
        ``user_deleted``）；复核里人还在 ⇒ 什么都不做。旧文案「待确认（不自动停用）」
        已退休，它既不准确（平台其实会跟进）又每轮重现、永不自愈。

        尚未回填上游主体标识的存量账号（``upstream_subject IS NULL``）**先排除**：
        它们永远不在 ``seen_subjects`` 里，照直比对会把「我们还没回填」误报成
        「上游删了这个人」（假阳性）。
        """
        existing_users = await self.user_repo.list_by_tenant(
            tenant_id, offset=0, limit=_BATCH_LIMIT
        )
        skipped_unbackfilled = 0
        candidates: list[User] = []
        for user in existing_users:
            if user.upstream_subject is None:
                skipped_unbackfilled += 1
                continue
            if user.upstream_subject not in seen_subjects:
                candidates.append(user)
        if skipped_unbackfilled:
            # 记明跳过条数：这批人暂不参与全量核对（上游删了也不会被发现），回填越早越好。
            logger.info(
                "全量缺失核对跳过 %d 个尚未回填上游主体标识的存量账号，"
                "请尽快执行 upstream_subject 回填",
                skipped_unbackfilled,
            )
        if not candidates:
            # ⚠️ 无候选 ⇒ **不做第二次拉取**（不白拉一次上游）。
            return
        if client is None:
            # 安全不变量：拿不到复核能力 ⇒ 绝不动手。
            await self._record_failure(
                task, SyncEntityType.USER.value, None, _REVIEW_FAILED_REASON
            )
            return
        try:
            review: SyncFetchResult = await client.fetch_users(
                SyncType.FULL.value, cursor=None
            )
        except (
            DataSourceConnectionError,
            DataSourceAuthError,
            DataSourceContractError,
            ServiceTimeoutError,
        ) as exc:
            # D3：复核拉取失败 ⇒ 不动手 + 记一条失败（带既有脱敏原因）。
            await self._record_failure(
                task,
                SyncEntityType.USER.value,
                None,
                f"{_REVIEW_FAILED_REASON}：{_WHOLE_FAILURE_REASONS.get(type(exc), '主数据源拉取失败')}",
            )
            return
        except Exception:  # noqa: BLE001 —— 复核报文不可解析等一律按「拿不到」处置
            logger.exception("全量缺失复核异常（tenant=%s）", tenant_id)
            await self._record_failure(
                task, SyncEntityType.USER.value, None, _REVIEW_FAILED_REASON
            )
            return
        review_subjects = {eu.external_user_id for eu in review.users}
        # 逐人独立判定：复核报文里回来了就跳过那一个，不影响其余候选。
        # ⛔ 只删、不补建：复核报文里新出现的人本地本来就正常。
        for user in candidates:
            if user.upstream_subject in review_subjects:
                continue
            await self._soft_delete_user(task, tenant_id, user)

    async def _record_failure(
        self,
        task: IdentitySyncTask,
        entity_type: str,
        external_entity_id: Optional[str],
        reason: str,
    ) -> None:
        """落一条失败明细，并推进 failed_count。"""
        failure = IdentitySyncFailure(
            sync_task_id=task.id,
            entity_type=entity_type,
            external_entity_id=external_entity_id,
            failure_reason=reason,
        )
        self.session.add(failure)
        task.failed_count += 1
        await self.session.flush()

    async def record_gate_block(self, tenant_id: str) -> int:
        """记一条「自动同步被门控拦下」的记录（波 3 的可见出口之一），返回 task_id。

        由调度器在闸门拦下**且 CAS 抢占成功后**调用 —— **task 的构造口径留在 service
        层**，调度器不 import 模型自己拼记录。

        - `status = failed`：`SyncTaskStatus` **没有** blocked / skipped 态，用 `failed`
          的最大好处是**前端零改动**（`TenantDetail/index.tsx` 已把 failed 渲染成红字，
          `SyncRecordDrawer.tsx` 已渲染 reason 列）。⛔ 不要新增枚举值。
        - 失败原因走既有 `IdentitySyncFailure` 落明细，文案自解释。
        - ⚠️ `datetime.now()` 必须是**朴素值** —— 本表列是 `TIMESTAMP WITHOUT TIME ZONE`，
          写 tz-aware 值 asyncpg 会抛 `TypeError`（线上才炸，单测是假 session 看不出来）。
        - ⚠️ 该表唯一索引 `uq_identity_sync_tasks_running` 只锁 `running`，写 `failed`
          **不冲突**。
        """
        tenant_id = require_tenant(tenant_id)
        now = datetime.now()
        task = IdentitySyncTask(
            tenant_id=tenant_id,
            status=SyncTaskStatus.FAILED.value,
            sync_type=SyncType.INCREMENTAL.value,
            trigger=SyncTrigger.AUTO.value,
            started_at=now,
            finished_at=now,
        )
        self.session.add(task)
        await self.session.flush()
        await self._record_failure(
            task, SyncEntityType.USER.value, None, _GATE_BLOCK_REASON
        )
        await self.session.flush()
        return task.id

    # ── 同步记录查询 ─────────────────────────────────────────────────

    async def list_sync_records(
        self, tenant_id: str, page: int = 1, page_size: int = 20
    ) -> SyncRecordListResponse:
        tenant_id = require_tenant(tenant_id)
        offset = (page - 1) * page_size
        result = await self.session.execute(
            select(IdentitySyncTask)
            .where(
                IdentitySyncTask.tenant_id == tenant_id,
                IdentitySyncTask.is_deleted == 0,
            )
            .order_by(IdentitySyncTask.id.desc())
            .offset(offset)
            .limit(page_size)
        )
        tasks = list(result.scalars().all())
        total_result = await self.session.execute(
            select(func.count())
            .select_from(IdentitySyncTask)
            .where(
                IdentitySyncTask.tenant_id == tenant_id,
                IdentitySyncTask.is_deleted == 0,
            )
        )
        total = total_result.scalar() or 0
        items = [self._to_summary(t) for t in tasks]
        return SyncRecordListResponse(items=items, total=total)

    async def get_sync_record(
        self, tenant_id: str, record_id: int
    ) -> SyncRecordDetailResponse:
        tenant_id = require_tenant(tenant_id)
        task = await self._get_task(tenant_id, record_id)
        failures = await self._list_failures(record_id)
        return SyncRecordDetailResponse(
            sync_time=task.started_at,
            sync_type=task.sync_type,
            status=task.status,
            failed_count=task.failed_count,
            user_change=UserChangeCounts(
                added=task.user_added,
                updated=task.user_updated,
                disabled=task.user_disabled,
                enabled=task.user_enabled,
                deleted=task.user_deleted,
            ),
            failures=[
                SyncFailureDetail(
                    entity_type=f.entity_type,
                    external_entity_id=f.external_entity_id,
                    reason=f.failure_reason,
                )
                for f in failures
            ],
        )

    async def _get_task(self, tenant_id: str, record_id: int) -> IdentitySyncTask:
        result = await self.session.execute(
            select(IdentitySyncTask).where(
                IdentitySyncTask.id == record_id,
                IdentitySyncTask.tenant_id == tenant_id,
                IdentitySyncTask.is_deleted == 0,
            )
        )
        task = result.scalar_one_or_none()
        if task is None:
            raise ResourceNotFoundError(
                message=translate(
                    "err.identity_sync.record_not_found",
                    fallback=f"同步记录不存在: {record_id}",
                )
            )
        return task

    async def _list_failures(self, record_id: int) -> list:
        result = await self.session.execute(
            select(IdentitySyncFailure)
            .where(IdentitySyncFailure.sync_task_id == record_id)
            .order_by(IdentitySyncFailure.id)
        )
        return list(result.scalars().all())

    @staticmethod
    def _to_summary(task: IdentitySyncTask) -> SyncRecordSummary:
        return SyncRecordSummary(
            id=task.id,
            sync_time=task.started_at,
            status=task.status,
            user_change=(
                task.user_added
                + task.user_updated
                + task.user_disabled
                + task.user_enabled
                + task.user_deleted
            ),
            failed_count=task.failed_count,
            trigger=task.trigger,
        )

    # ── 审计 ──────────────────────────────────────────────────────────────────

    async def _write_audit(
        self,
        task: IdentitySyncTask,
        tenant_id: str,
        operator_user_id: Optional[int],
        operator_username: Optional[str],
        *,
        bypassed: bool = False,
    ) -> None:
        """写一条同步审计。

        `bypassed=True` 表示本次是**绕过门控**跑的（`force=True` 且当时未通过连接测试）
        —— 只在 `request_params` 上加一个标记键，**不新增审计 action**。
        """
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService

            outcome = (
                "FAILED"
                if task.status
                in {SyncTaskStatus.FAILED.value, SyncTaskStatus.PARTIAL_FAILED.value}
                else "SUCCESS"
            )
            await AuditLogService(self.session).record(
                tenant_id=tenant_id,
                log_type="OPERATION",
                action="sync_identity_integration",
                outcome=outcome,
                service_name="platform",
                user_id=operator_user_id,
                username=operator_username,
                resource="identity_sync",
                resource_id=str(task.id),
                request_params={
                    "sync_type": task.sync_type,
                    "trigger": task.trigger,
                    **({"gate_bypassed": True} if bypassed else {}),
                },
                sync=True,
            )
        except Exception:  # noqa: BLE001 —— 审计失败不阻塞同步主流程
            logger.exception("记录同步审计日志失败（不影响同步流程）")


__all__ = ["IdentityIntegrationSyncService"]
