#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""企业身份集成「自动同步」调度器（闹钟 + 电池）。

背景：`identity_integration_config.auto_sync_enabled` / `sync_interval` 此前是**死配置**
—— 全仓只有配置的读写，没有任何消费者，打开「自动同步」不产生任何行为。本模块补上
「闹钟」（一轮一轮扫到期租户）与「电池」（真的发起同步）。

形状取 `capabilities/task_center/services/scheduler_service.py`：公开 `run_once()` 供
单测直接调，构造器把 `interval` / `batch` / `session_factory` / `sync_service_factory`
/ `now_fn` 全部做成注入口 —— **不取** knowledge_base 那种把循环硬编码进能力类的写法
（那种写法没法在单测里替换时钟与 DB）。

## 一轮干三件事

0. **门控**（`_block_on_gate`，波 3）：`datasource_connection_verified_at IS NULL`
   （没通过「测试连接」）的租户**不发起同步** —— 自动路径不传 `force`、无从绕过。
   被拦时走一条**独立于 `_claim` 的推进路径**（`_advance`）：只推进 `next_sync_at`
   做节流，**一个字都不写 `last_sync_at`**，并在抢占成功后写一条可见告警记录。
   详见 `_block_on_gate` docstring。
1. **扫描**（`_scan_due`，只读、跨租户）：`auto_sync_enabled IS TRUE` + `datasource_url`
   非空 + 到期（`next_sync_at IS NULL OR next_sync_at <= now`），按 `next_sync_at ASC
   NULLS FIRST` 取 `batch` 条。取完就结束这个事务。
2. **逐条 CAS 抢占**（`_claim`，**独立事务、先提交**）：把 `next_sync_at` 推到下一轮、
   `last_sync_at` 记为本轮时刻。命中 0 行 = 别的副本刚抢走 → 本轮放弃这条。
3. **逐条发起同步**（`_trigger`）：`trigger_sync(mode="incremental", trigger="auto")`。

## 为什么 `next_sync_at IS NULL` = 「立即到期」

拍板 ③：不是「还没排期、先放着」。`auto_sync_enabled` 刚被用户打开、或本列刚随
`update/039` 落地时，全部命中这一档 —— 若解释成「没排期就不跑」，用户打开开关后
要等一个周期（默认 24 小时）才看到第一次自动同步，开关像是坏的。所以首轮就为它排期。

## 并发保护（两层，不引入分布式锁）

多副本下每个副本都跑自己的循环，扫描本身没有互斥，必然扫到同一批租户。互斥靠：

- **① `next_sync_at` CAS 抢占**：`WHERE id = :id AND next_sync_at IS NOT DISTINCT FROM
  :expected`。⚠️ **必须是 `IS NOT DISTINCT FROM`，不能用 `=`** —— 首轮 `expected` 是
  `NULL`，而 `NULL = NULL` 在 SQL 里是 `NULL`（假），用 `=` 的话**首轮同步永远发不出去**
  （`auto_sync_enabled` 打开后一直是 `NULL` 的那一批，会永远留在候选集里、每轮都抢不到）。
- **② 既有部分唯一索引** `uq_identity_sync_tasks_running`（`tenant_id WHERE status =
  'running'`）兜底：两个副本 CAS 撞车后同时进到 `trigger_sync` 时，仍会被它拦下
  （`IntegrityError`）。

## 与「立即同步」（手动）互斥 —— 跳过本轮、**不记失败**

`_ensure_no_running()` 是**按租户**粒度的，所以同租户的自动与手动**互相 409**
（`SyncTaskStateError` 4005）。调度侧处置 = **跳过本轮、不记失败、不改任何状态**，
日志级别 **INFO**：

- **不记失败**：这次「没跑成」不是同步失败，人正跑着同一条链路，记一笔失败会污染
  「最近同步记录」并让 `failed_count` 说谎；
- **不进失败分支**：所以捕获点必须在 `async with get_db_session()` **之外** —— 会话里
  捕获会把业务异常翻成「本轮出错」，一路走进 WARNING 与错误计数。
- ⚠️ **代价（有意接受）**：抢占在第 2 步已经提交，`next_sync_at` 已推到下一轮，所以
  这次跳过会让该租户等满一个周期。可接受 —— 此刻真的有一次同步在跑，数据不会因此变旧。
  （反过来「先查有没有 running、再抢占」会把判定与抢占拆成两个事务，多副本下照样撞车，
  等于白加一层。）

## 其余约定

- **逐条兜异常**（`run_once` 的循环里）：一个租户出问题不能让同轮剩下的租户不跑。
- **失败处理**：数据源那四类异常（5004/5005/5006/5007）已由 `IdentityIntegrationSyncService._run`
  内部吞掉并落任务终态 `failed`，调度侧**无需额外处置**，只按返回的 `status` 记
  WARNING（`failed`）/ INFO（其余）。**v1 不做退避**（周期本身以小时计）。
- **默认 mode = `incremental`**：全量会走「快照缺失待确认」⇒ 每次自动全量都落
  `partial_failed`，那是人工流程，不该由闹钟触发。
- **时间一律用 Python 侧的 `now_fn`**：⚠️ async ORM 里**不许用 `func.now()`**
  （见 `async-orm-sqlalchemy-func-now`）—— 一轮里的「现在」必须是同一个值，写进
  `last_sync_at` 的、拿去比对到期的、算 `next_sync_at` 的必须是同一个时刻；用数据库
  的 `now()` 会随语句漂移，且单测无法注入假时钟。
- **时钟是朴素时间（`datetime.now()`）**：本表两列是 `TIMESTAMP WITHOUT TIME ZONE`
  （同 `datasource_connection_verified_at`），**不要**写 tz-aware 值 —— asyncpg 对
  `timestamp` 参数执行 `obj - datetime(2000, 1, 1)` 的朴素减（见
  `asyncpg/pgproto/codecs/datetime.pyx::timestamp_encode`），带 tzinfo 会直接
  `TypeError`，而那会在线上才炸（单测是假 session、看不出来）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, AsyncContextManager, Callable, NamedTuple, Optional

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from jonex_core.common.config import get_config
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import SyncTaskStateError

from ..models.identity_integration import (
    IdentityIntegrationConfig,
    SyncTaskStatus,
    SyncTrigger,
    SyncType,
)

logger = logging.getLogger(__name__)

#: 同步周期的**硬下限**（小时）。
#: 兜住「单位被理解成分钟」的最坏解读：`sync_interval` 存的是小时，但若有人按分钟填
#: （填 30 想表达「半小时一次」），不加下限就成了每 30 小时一次。取 1 小时是「小时」
#: 这个单位下最小的有意义值 —— 自动同步是外部系统出站拉取，再密就该走手动/增量之外
#: 的别的机制了。
MIN_INTERVAL_HOURS = 1

#: 一轮统计的键（`run_once()` 的返回值）。全为整数，便于调用方断言 / 打点。
_STAT_KEYS = (
    "scanned",  # 本轮扫到几个到期租户
    "claimed",  # CAS 抢占命中几个
    "skipped",  # CAS 抢占未命中（被别的副本抢走）几个
    "blocked",  # 未通过连接测试被门控拦下的几个（波 3；已记告警、已推进周期）
    "conflict",  # 与手动同步（或别的副本）互斥、跳过的几个（**不算失败**）
    "succeeded",  # 同步任务落终态且不是 failed 的几个
    "failed",  # 同步任务落 failed 的几个
    "errors",  # 处理途中有租户抛异常的几个（逐条兜异常，不影响其余租户）
)


class DueTenant(NamedTuple):
    """扫描结果的一行 —— **只带抢占要用的原值与算周期要用的配置**。

    刻意在扫描事务**内部**就把 ORM 行拆成朴素值：会话一关，ORM 实体就 detached，
    之后访问它的属性可能触发对已关闭会话的惰性加载（`DetachedInstanceError`）。
    `task_center` 的 `_fire_pass` 用的是同一手法。
    """

    config_id: int
    tenant_id: str
    sync_interval: Optional[int]
    next_sync_at: Optional[datetime]
    #: 波 3 门控判据：为空 ⇒ 未通过连接测试 ⇒ 拦截。随扫描一次取回，**不为门控再发查询**。
    datasource_connection_verified_at: Optional[datetime]


def _default_sync_service_factory(session: Any) -> Any:
    """默认的同步服务工厂（惰性 import，避免模块导入期的循环依赖）。"""
    from .identity_integration_sync import IdentityIntegrationSyncService

    return IdentityIntegrationSyncService(session)


class IdentitySyncScheduler:
    """自动同步后台循环：扫到期租户 → CAS 抢占 → 发起同步。"""

    def __init__(
        self,
        *,
        interval: Optional[float] = None,
        batch: Optional[int] = None,
        session_factory: Optional[Callable[[], AsyncContextManager[Any]]] = None,
        sync_service_factory: Optional[Callable[[Any], Any]] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        config = get_config()
        self._interval = (
            interval
            if interval is not None
            else getattr(config, "IDENTITY_SYNC_SCHEDULER_INTERVAL", 60.0)
        )
        self._batch = (
            batch
            if batch is not None
            else getattr(config, "IDENTITY_SYNC_SCHEDULER_BATCH", 50)
        )
        self._default_interval_hours = getattr(
            config, "IDENTITY_SYNC_DEFAULT_INTERVAL_HOURS", 24
        )
        # 三处注入口：单测替换掉真实 DB / 真实同步服务 / 真实时钟。
        self._session_factory = session_factory or get_db_session
        self._sync_service_factory = (
            sync_service_factory or _default_sync_service_factory
        )
        self._now_fn = now_fn or datetime.now
        self._task: Optional[asyncio.Task] = None

    # ── 生命周期 ──────────────────────────────────────────

    async def start(self) -> None:
        """起循环。**启动时立刻跑第一轮**（不等第一个间隔，拍板 ③）。"""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "[IdentitySync] 自动同步调度器已启动（间隔 %.1fs，每轮最多 %s 个租户，"
            "默认周期 %s 小时）",
            self._interval,
            self._batch,
            self._default_interval_hours,
        )

    async def stop(self) -> None:
        """停循环（未起过就什么都不做）。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("[IdentitySync] 自动同步调度器已停止")

    async def _loop(self) -> None:
        first_run = True
        while True:
            try:
                if not first_run:
                    await asyncio.sleep(self._interval)
                first_run = False
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 一轮失败不该让循环死掉，下一轮接着来
                logger.warning("[IdentitySync] 自动同步本轮出错，跳过本轮：%s", exc)

    # ── 一轮 ──────────────────────────────────────────────

    async def run_once(self) -> dict:
        """跑一轮：扫描 → 逐条抢占 → 逐条发起同步；返回本轮统计（见 `_STAT_KEYS`）。

        单测直接调这个方法，不必等定时器。
        """
        now = self._now_fn()
        async with self._session_factory() as session:
            due = await self._scan_due(session, now, self._batch)

        stats = {key: 0 for key in _STAT_KEYS}
        stats["scanned"] = len(due)
        for target in due:
            # 逐条兜异常：一个租户出问题（DB 抖、配置脏、服务层意外）不能让**同轮剩下的
            # 租户**不跑 —— 否则一条毒数据就等于全平台自动同步停摆。
            try:
                await self._run_one(target, now, stats)
            except Exception as exc:  # noqa: BLE001 —— 逐条兜，见上
                stats["errors"] += 1
                logger.warning(
                    "[IdentitySync] 调度租户 %s 出错（本轮其余租户不受影响）：%s",
                    target.tenant_id,
                    exc,
                )
        if stats["scanned"] or stats["errors"]:
            logger.info(
                "[IdentitySync] 本轮：扫到 %d，抢占 %d，被抢走 %d，门控拦下 %d，"
                "与手动同步互斥跳过 %d，成功 %d，失败 %d，异常 %d",
                stats["scanned"],
                stats["claimed"],
                stats["skipped"],
                stats["blocked"],
                stats["conflict"],
                stats["succeeded"],
                stats["failed"],
                stats["errors"],
            )
        return stats

    async def _run_one(self, target: DueTenant, now: datetime, stats: dict) -> None:
        """处理一个到期租户：门控 → CAS 抢占 → 发起同步。"""
        expected = target.next_sync_at
        next_at = now + timedelta(hours=self._interval_hours(target.sync_interval))

        # ⓪ 波 3 门控：未通过连接测试 ⇒ 拦下（自动同步不传 `force`、无从绕过）。
        #    判据 = `datasource_connection_verified_at IS NULL`（不加时效、不做自愈）。
        if target.datasource_connection_verified_at is None:
            await self._block_on_gate(target, stats, expected=expected, next_at=next_at)
            return

        # ① CAS 抢占（**独立事务、先提交**）：抢到就把下一次的时刻推走了，后面
        #    `trigger_sync` 失败也不会同一轮反复抢同一条。抢不到 = 别的副本刚处理过。
        if not await self._claim(
            target.config_id, expected=expected, next_sync_at=next_at, now=now
        ):
            stats["skipped"] += 1
            logger.debug(
                "[IdentitySync] 租户 %s 的 next_sync_at 已被别的副本抢走，本轮跳过",
                target.tenant_id,
            )
            return
        stats["claimed"] += 1

        # ② 发起同步。**互斥的捕获点在这个 `async with` 之外** —— 见模块 docstring
        #    「与『立即同步』互斥」一段：这是「跳过、不记失败」，不是「本轮出错」。
        try:
            async with self._session_factory() as session:
                service = self._sync_service_factory(session)
                response = await service.trigger_sync(
                    target.tenant_id,
                    SyncType.INCREMENTAL.value,
                    trigger=SyncTrigger.AUTO.value,
                )
        except (SyncTaskStateError, IntegrityError) as exc:
            # 同租户已有 running 的任务 ⇒ 手动同步正在跑（或另一个副本刚抢跑）。
            # 跳过本轮、不记失败、不改任何状态（抢占那一步的写入除外，见类 docstring）。
            stats["conflict"] += 1
            logger.info(
                "[IdentitySync] 租户 %s 已有同步进行中（自动/手动互斥），跳过本轮、"
                "不记失败，下次 %s：%s",
                target.tenant_id,
                next_at,
                exc,
            )
            return

        status = getattr(response, "status", None)
        if status == SyncTaskStatus.FAILED.value:
            stats["failed"] += 1
            logger.warning(
                "[IdentitySync] 租户 %s 自动同步落 failed（task=%s），下次 %s",
                target.tenant_id,
                getattr(response, "task_id", None),
                next_at,
            )
        else:
            stats["succeeded"] += 1
            logger.info(
                "[IdentitySync] 租户 %s 自动同步完成 status=%s（task=%s），下次 %s",
                target.tenant_id,
                status,
                getattr(response, "task_id", None),
                next_at,
            )

    async def _block_on_gate(
        self,
        target: DueTenant,
        stats: dict,
        *,
        expected: Optional[datetime],
        next_at: datetime,
    ) -> None:
        """门控拦下一条：**推进周期 → 抢占成功才写告警 → return**。

        顺序与取舍（契约 C4，逐条都是判据不是偏好）：

        1. **必须推进 `next_sync_at`**（这是「会不会刷屏」的唯一开关）：闸门若放
           `_scan_statement` 的 WHERE 里过滤掉，该租户**永远留在候选集**、每轮都扫到
           —— 要么刷屏、要么只能静默（静默就违背「必须产生可见告警」，用户看到的将
           与波 1 修掉的「死配置」bug 无法区分）。放在抢占的位置上则天然按周期节流，
           **一个周期一条，不用另做去重**。
        2. **⛔ 一个字都不写 `last_sync_at`**：该列语义是「最近一次自动同步的**发起**
           时刻」，闸门拦下时并没有发起同步，写它就是说谎（见 `_advance_statement`）。
        3. **告警必须在抢占成功之后才写**：`_advance` 的 CAS 是多副本互斥点 —— 先写
           告警再抢占，两个副本会各写一条重复告警。
        """
        if not await self._advance(
            target.config_id, expected=expected, next_sync_at=next_at
        ):
            stats["skipped"] += 1
            logger.debug(
                "[IdentitySync] 租户 %s 未通过连接测试，且 next_sync_at 已被别的副本"
                "抢走，本轮跳过",
                target.tenant_id,
            )
            return
        stats["blocked"] += 1

        # 抢占事务已提交 ⇒ 才写可见告警（task 的构造口径留在 service 层）
        async with self._session_factory() as session:
            service = self._sync_service_factory(session)
            await service.record_gate_block(target.tenant_id)

        logger.warning(
            "[IdentitySync] 租户 %s 未通过连接测试，自动同步已被门控拦下"
            "（已记一条同步记录），下次 %s",
            target.tenant_id,
            next_at,
        )

    # ── 扫描 / 抢占 ───────────────────────────────────────

    async def _scan_due(
        self, session: Any, now: datetime, limit: Optional[int]
    ) -> list[DueTenant]:
        """扫到期租户（跨租户）。**只读、只取一批 id 与抢占要用的原值**，取完就结束。"""
        result = await session.execute(self._scan_statement(now, limit))
        rows = result.scalars().all()
        return [
            DueTenant(
                config_id=row.id,
                tenant_id=row.tenant_id,
                sync_interval=row.sync_interval,
                next_sync_at=row.next_sync_at,
                datasource_connection_verified_at=(
                    row.datasource_connection_verified_at
                ),
            )
            for row in rows
        ]

    async def _claim(
        self,
        config_id: int,
        *,
        expected: Optional[datetime],
        next_sync_at: datetime,
        now: datetime,
    ) -> bool:
        """CAS 抢占：返回是否命中（命中 0 行 = 别的副本刚抢走 → 本轮放弃该条）。

        **自己开一个会话 = 自己一个事务**（`get_db_session` 在块尾 commit）：
        「扫描」与「抢占」必须是两个事务，抢占得**先提交**再发起同步。
        """
        async with self._session_factory() as session:
            result = await session.execute(
                self._claim_statement(
                    config_id, expected=expected, next_sync_at=next_sync_at, now=now
                )
            )
        return (getattr(result, "rowcount", 0) or 0) > 0

    async def _advance(
        self,
        config_id: int,
        *,
        expected: Optional[datetime],
        next_sync_at: datetime,
    ) -> bool:
        """门控拦下时的周期推进（CAS），返回是否命中。

        **自己开一个会话 = 自己一个事务**，与 `_claim` 同构 —— 抢占先提交，之后才写告警。
        """
        async with self._session_factory() as session:
            result = await session.execute(
                self._advance_statement(
                    config_id, expected=expected, next_sync_at=next_sync_at
                )
            )
        return (getattr(result, "rowcount", 0) or 0) > 0

    @staticmethod
    def _scan_statement(now: datetime, limit: Optional[int]):
        """构造扫描语句（独立成方法：单测可直接断言编译出的 SQL）。

        due 谓词三部分：

        1. `auto_sync_enabled IS TRUE` —— 用户打开过「自动同步」；
        2. `datasource_url` 非空 —— 没配数据源的租户跑了也只会落一条失败（
           `trigger_sync` 会抛 `InvalidParameterError`），不如不扫它；
           ⚠️ 这一条**必须留在查询 WHERE 里**，不能进 `update/039` 的索引谓词 ——
           它是**易变列**（用户随时改地址），进索引谓词等于每次改地址都要重建索引；
        3. 到期：`next_sync_at IS NULL`（首轮，见模块 docstring）或 `<= now`。

        `ORDER BY next_sync_at ASC NULLS FIRST`：先到期的先跑；`NULL`（首轮）排最前
        —— 与 `update/039` 的索引排序声明（`ASC NULLS FIRST`）严格对齐，扫描可以走索引。
        """
        return (
            select(IdentityIntegrationConfig)
            .where(IdentityIntegrationConfig.auto_sync_enabled.is_(True))
            .where(IdentityIntegrationConfig.datasource_url.isnot(None))
            .where(IdentityIntegrationConfig.datasource_url != "")
            .where(
                or_(
                    IdentityIntegrationConfig.next_sync_at.is_(None),
                    IdentityIntegrationConfig.next_sync_at <= now,
                )
            )
            .order_by(IdentityIntegrationConfig.next_sync_at.asc().nulls_first())
            .limit(limit)
        )

    @staticmethod
    def _claim_statement(
        config_id: int,
        *,
        expected: Optional[datetime],
        next_sync_at: datetime,
        now: datetime,
    ):
        """构造 CAS 抢占语句（独立成方法：单测可直接断言编译出的 SQL）。

        ⚠️ **`IS NOT DISTINCT FROM` 不是 `=`**，理由见模块 docstring 第 ① 条 —— 这是
        本模块最容易被「顺手简化」掉、且简化后**不报错、只是首轮静默同步不了**的一处。

        一次写入两个水位：`next_sync_at` 推到下一轮、`last_sync_at` 记本轮时刻
        （`last_sync_at` 的语义 = **最近一次自动同步的发起时刻**；手动同步不写它）。
        """
        return (
            update(IdentityIntegrationConfig)
            .where(IdentityIntegrationConfig.id == config_id)
            .where(
                IdentityIntegrationConfig.next_sync_at.is_not_distinct_from(expected)
            )
            .values(next_sync_at=next_sync_at, last_sync_at=now)
        )

    @staticmethod
    def _advance_statement(
        config_id: int,
        *,
        expected: Optional[datetime],
        next_sync_at: datetime,
    ):
        """构造「门控拦下」时的推进语句（独立成方法：单测可直接断言编译出的 SQL）。

        **只写 `next_sync_at`，⛔ 绝不写 `last_sync_at`** —— 该列语义是「最近一次自动
        同步的**发起**时刻」（`_claim_statement` docstring / `update/039` 注释已钉死，
        且明确「手动同步不写它」）。闸门拦下时**并没有发起同步**，写它就是说谎，会让
        卡片上的「最近同步」显示一个根本没发生的时刻。

        CAS 谓词与 `_claim_statement` **逐字一致**（`IS NOT DISTINCT FROM`，理由见模块
        docstring 第 ① 条）—— 它同样是多副本互斥点。
        """
        return (
            update(IdentityIntegrationConfig)
            .where(IdentityIntegrationConfig.id == config_id)
            .where(
                IdentityIntegrationConfig.next_sync_at.is_not_distinct_from(expected)
            )
            .values(next_sync_at=next_sync_at)
        )

    # ── 周期 ──────────────────────────────────────────────

    def _interval_hours(self, sync_interval: Optional[int]) -> float:
        """本租户的同步周期（小时）：`sync_interval` 兜底默认值，再压一道硬下限。

        - 未配（`NULL`）→ `IDENTITY_SYNC_DEFAULT_INTERVAL_HOURS`（默认 24，
          **与前端 `SyncConfigDrawer.tsx` 的 `DEFAULT_INTERVAL_HOURS` 同值**）；
        - `0` / 负数 / 脏值 → 落到 `MIN_INTERVAL_HOURS`（1 小时）；
        - 脏值（非数字）→ 告警 + 用默认值（列是 Integer，正常取不出脏值，这里是防御性兜底：
          让它退化成「按默认周期跑」而不是让这个租户**每轮必抛**、永久卡在候选集里）。
        """
        hours = self._default_interval_hours if sync_interval is None else sync_interval
        try:
            hours = float(hours)
        except (TypeError, ValueError):
            logger.warning(
                "[IdentitySync] 租户的 sync_interval 不是数字（%r），本轮用默认周期 %s 小时",
                sync_interval,
                self._default_interval_hours,
            )
            hours = float(self._default_interval_hours)
        return max(hours, float(MIN_INTERVAL_HOURS))


# ============================================================
# 进程内的单例（能力模块 initialize / shutdown 时用）
# ============================================================

_scheduler: Optional[IdentitySyncScheduler] = None


def get_scheduler() -> IdentitySyncScheduler:
    """取本进程的自动同步调度器（首次调用时创建）。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = IdentitySyncScheduler()
    return _scheduler


async def start_scheduler() -> None:
    """起自动同步调度器。**恒开、不设开关**（对齐 task_center）。

    要禁掉它只有两个办法：把 `IDENTITY_SYNC_SCHEDULER_BATCH` 设 0（每轮扫 0 条），
    或让所有租户的 `auto_sync_enabled` 保持关闭 —— 后者才是产品语义上的「关」。
    """
    await get_scheduler().start()


async def stop_scheduler() -> None:
    """停自动同步调度器（未起过就什么都不做）。"""
    global _scheduler
    if _scheduler is None:
        return
    await _scheduler.stop()
    _scheduler = None


__all__ = [
    "DueTenant",
    "IdentitySyncScheduler",
    "MIN_INTERVAL_HOURS",
    "get_scheduler",
    "start_scheduler",
    "stop_scheduler",
]
