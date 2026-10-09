#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Repository for Knowledge Base documents."""

from sqlalchemy import exists, func, or_ as sa_or, select
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.repository import BaseRepository

from ..models import DocStatus, KnowledgeDocument, OntologyStatus
from .processing_task_repository import (
    EXTRACT_STATUS_MAP,
    PARSE_STATUS_MAP,
    TASK_STATUS_FAILED,
    TASK_TYPE_EXTRACT,
    TASK_TYPE_PARSE,
    record_processing_task,
)
from ..models.task_center import ACTIVE_STATES, TaskCenterDocument


def _value(status: str | DocStatus | OntologyStatus | None) -> str | None:
    return status.value if hasattr(status, "value") else status


# 未分类文件夹哨兵值：folder_id 查询/计数时用于指代 folder_id IS NULL 的文档。
# 真实 folder_id 为 uuid4().hex（32 位 hex），不会与该值冲突。
UNCLASSIFIED_SENTINEL = "__unclassified__"


class KnowledgeDocumentRepository(BaseRepository[KnowledgeDocument]):
    model = KnowledgeDocument

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_documents(
        self,
        tenant_id: str,
        status: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[KnowledgeDocument]:
        conditions = []
        if status:
            conditions.append(KnowledgeDocument.status == status)
        return await self.list_all(
            tenant_id=tenant_id,
            offset=offset,
            limit=limit,
            extra_conditions=conditions,
        )

    async def list_by_knowledge_base(
        self,
        tenant_id: str,
        knowledge_base_id: str,
    ) -> list[KnowledgeDocument]:
        stmt = select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == self._tenant_id(tenant_id),
            KnowledgeDocument.is_deleted == 0,
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def list_by_folder(
        self,
        tenant_id: str,
        folder_id: str,
    ) -> list[KnowledgeDocument]:
        """查询指定文件夹下的文档（用于删除文件夹时解除关联）。"""
        stmt = select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == self._tenant_id(tenant_id),
            KnowledgeDocument.is_deleted == 0,
            KnowledgeDocument.folder_id == folder_id,
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    @staticmethod
    def _not_task_center_managed():
        """`NOT EXISTS` 子查询：把「正被任务中心接管」的文档排除出扫描面。

        ⚠️ **必须下沉成 SQL**（不能取一批再在 Python 里 `continue`）：一个全是接管
        文档的知识库会把 `limit` 的扫描额度吃光，真正需要救的文档排不进来 ——
        而这条循环 30 秒一轮、恒开，「排不进来」就是永久。

        判据只认状态、不认识接管表的其他列（**纯 NOT EXISTS，不含任何业务判断**）：
        业务侧「哪些状态即使被接管也不能排除」由调用方用 `task_center_kept_statuses`
        显式列出（见 `list_by_ontology_status_for_reconciliation`）。
        """
        return ~exists().where(
            TaskCenterDocument.document_id == KnowledgeDocument.id,
            TaskCenterDocument.state.in_(list(ACTIVE_STATES)),
        )

    async def list_by_status_for_reconciliation(
        self,
        status: str | DocStatus | list[str | DocStatus],
        limit: int = 100,
        *,
        exclude_task_center_managed: bool = False,
    ) -> list[KnowledgeDocument]:
        """Cross-tenant scan reserved for internal reconciliation workers.

        [jonex] 批次 2-B：支持单 status 或多 status 列表扫描，
        使 reconcile_documents / patrol_parsing_timeout 可同时覆盖 PARSING + INGESTING。

        `exclude_task_center_managed=True` 时跳过**正被任务中心接管**的文档：这些文档的
        `PARSING` 是任务中心链的第一步（不是老链路的「解析+入库一次做完」），既有对账按老
        形态去问会得到错误的结论 —— `patrol_parsing_timeout` 尤其危险，它判超时后会
        **重新 insert**，与任务中心链彻底分叉、产出重复向量。
        """

        if isinstance(status, list):
            status_cond = KnowledgeDocument.status.in_([_value(s) for s in status])
        else:
            status_cond = KnowledgeDocument.status == _value(status)

        conditions = [
            KnowledgeDocument.is_deleted == 0,
            status_cond,
        ]
        if exclude_task_center_managed:
            conditions.append(self._not_task_center_managed())

        stmt = (
            select(KnowledgeDocument)
            .where(*conditions)
            .order_by(KnowledgeDocument.updated_at.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def list_by_ontology_status_for_reconciliation(
        self,
        statuses: list[str | OntologyStatus],
        limit: int = 50,
        *,
        skip_locked: bool = False,
        exclude_task_center_managed: bool = False,
        task_center_kept_statuses: list[str | OntologyStatus] | None = None,
    ) -> list[KnowledgeDocument]:
        """Cross-tenant ontology scan reserved for internal reconciliation workers.

        [jonex] P1-F：skip_locked=True 时用 FOR UPDATE SKIP LOCKED 领取，
        多个 KB service 实例并发对账时不会重复领取同一批文档。

        ── 与任务中心接管的关系（`exclude_task_center_managed`）──

        被任务中心接管的文档，其「本体抽取」由任务中心链的 compile 步发起，**不能再被对账
        发起第二次**（两套驱动器同跑会写两次 Neo4j、token 翻倍）。所以这里排除它们。

        ⚠️ **但有一档必须留下**：`EXTRACTING`。它是**收尾**、不是新提交 —— 写 Neo4j 的
        唯一入口 `ReconciliationService._handle_completed` 只从 `_reconcile_one` 与
        `_retry_ontology_one` 两处被调，而后者对 EXTRACTING 文档干的正是「轮询 atomic-rag
        → 写图 → 置 READY」。把 EXTRACTING 一并排除掉，compile 步就永远收不了尾。

        所以「哪些状态即使被接管也不排除」由**调用方**用 `task_center_kept_statuses`
        显式给出 —— 仓储不认识业务状态的含义，不替调用方猜。
        """

        conditions = [
            KnowledgeDocument.is_deleted == 0,
            KnowledgeDocument.status == DocStatus.READY.value,
            KnowledgeDocument.ontology_status.in_([_value(status) for status in statuses]),
        ]
        if exclude_task_center_managed:
            kept = [_value(s) for s in (task_center_kept_statuses or [])]
            not_managed = self._not_task_center_managed()
            if kept:
                # 被接管的文档里，只有落在 kept 里的状态仍归对账管。
                conditions.append(
                    sa_or(
                        KnowledgeDocument.ontology_status.in_(kept),
                        not_managed,
                    )
                )
            else:
                conditions.append(not_managed)

        stmt = (
            select(KnowledgeDocument)
            .where(
                *conditions,
                # [jonex] P0-3：已判定需人工介入的文档不再领取——门闩在
                # _retry_ontology_one 入口也拦一次，但只靠那里仍会让这些文档
                # 持续占用 limit=50 的配额，累积满后对账扫不到任何新文档。
                # NULL 语义已实测（本地 PG：键缺失 / 列 NULL / 显式 false
                # 均保留，只有 true 被排除），故从未写过该键的文档不受影响。
                KnowledgeDocument.extra_metadata["ontology_auto_retry_disabled"]
                .as_boolean().isnot(True),
            )
            .order_by(KnowledgeDocument.updated_at.asc())
            .limit(limit)
        )
        if skip_locked:
            stmt = stmt.with_for_update(skip_locked=True)
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def count_by_ontology_status(
        self,
        statuses: list[str | OntologyStatus],
    ) -> int:
        """跨租户统计处于给定本体状态的文档数（对账在途限流用）。"""
        stmt = (
            select(func.count())
            .select_from(KnowledgeDocument)
            .where(
                KnowledgeDocument.is_deleted == 0,
                KnowledgeDocument.status == DocStatus.READY.value,
                KnowledgeDocument.ontology_status.in_([_value(status) for status in statuses]),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one() or 0)

    async def claim_ontology_for_retry(self, doc) -> bool:
        """[jonex] P1-F：CAS 领取——把文档从当前 ontology_status 原子转 EXTRACTING 占位。

        仅当 ontology_status 仍为读取时的值、且 content_generation 未变时成功（rowcount>0）。
        多实例并发时只有一个能领取到，其余得到 rowcount=0 → 跳过。
        提交 atomic-rag 前调用；提交失败由调用方回退状态。
        """
        from sqlalchemy import update

        stmt = (
            update(KnowledgeDocument)
            .where(
                KnowledgeDocument.id == doc.id,
                KnowledgeDocument.tenant_id == doc.tenant_id,
                KnowledgeDocument.ontology_status == _value(doc.ontology_status),
                KnowledgeDocument.content_generation == (doc.content_generation or 0),
            )
            .values(ontology_status=OntologyStatus.EXTRACTING.value)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return (result.rowcount or 0) > 0

    async def revert_ontology_status(self, doc, status: str | OntologyStatus) -> None:
        """把文档本体状态回退为指定值（提交失败时释放 EXTRACTING 占位，不 ++retry）。"""
        from sqlalchemy import update

        await self.session.execute(
            update(KnowledgeDocument)
            .where(
                KnowledgeDocument.id == doc.id,
                KnowledgeDocument.tenant_id == doc.tenant_id,
            )
            .values(ontology_status=_value(status))
        )
        await self.session.flush()

    async def set_status(
        self,
        doc: KnowledgeDocument,
        status: str | DocStatus,
        *,
        rag_task_id: str | None = None,
        rag_doc_ids: list[str] | None = None,
        error_message: str | None = None,
    ) -> KnowledgeDocument:
        fresh = await self.get_by_id(doc.id, doc.tenant_id)
        # [jonex] 数据运营二期：改动前的状态要**在赋值之前**取（merge 兜底路径
        # 拿不到旧值，故在 merge 之前算，否则那条路的终态事件会丢）。
        prev_status = fresh.status if fresh is not None else None
        if fresh is None:
            # doc not found in this session — use merge as last resort
            fresh = await self.session.merge(doc)
        fresh.status = _value(status)
        if rag_task_id is not None:
            fresh.rag_task_id = rag_task_id
        if rag_doc_ids is not None:
            fresh.rag_doc_ids = rag_doc_ids
        if error_message is not None:
            fresh.error_message = error_message
        await self.session.flush()
        # [jonex] 数据运营二期：文件解析任务埋点（parse）。
        # ① 只在**状态真的变了**时写：重复 `set_status(READY)` 会把已收工的
        #    终态行覆盖成新的一次开工，`pipeline_task_stats` 随之虚高；
        # ② 复用调用方已有 session（`record_processing_task` 自己吞异常，
        #    埋点失败不影响主流程）。
        _task_status = PARSE_STATUS_MAP.get(fresh.status)
        if _task_status and _value(prev_status) != fresh.status:
            await record_processing_task(
                self.session, doc.tenant_id,
                asset_id=fresh.id, task_type=TASK_TYPE_PARSE, status=_task_status,
                doc_id=fresh.id, kb_id=fresh.knowledge_base_id,
                content_generation=fresh.content_generation,
                error=fresh.error_message if _task_status == TASK_STATUS_FAILED else None,
            )
        return fresh

    async def set_rag_task_id(
        self, document_id: str, tenant_id: str, rag_task_id: str
    ) -> None:
        """**只写 `rag_task_id` 一列**（不碰文档状态、不读回写整行）。

        存在的理由：接管行交还前要把在途的下游任务号交给文档行
        （`task_runner_service.hand_over_in_flight_task_id`）—— 那一步**只想写这一列**。
        走 `set_status(doc, doc.status, rag_task_id=…)` 也能写，但它会把「刚才读到的那个
        状态值」一并回写：若这期间别处（看门狗收口 / 既有对账）已经把它推成 READY /
        FAILED，那个旧值就把新值盖回去了（丢更新）。这里只 UPDATE 一列，谁也盖不着。

        `tenant_id` 一并带上：这份行必须属于它 —— 跨租户的 id 一次都不该被改到；
        `is_deleted = 0` 同理（已删的行不该再被任何流程写）。
        """
        from datetime import datetime

        from sqlalchemy import update

        stmt = (
            update(KnowledgeDocument)
            .where(
                KnowledgeDocument.id == document_id,
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.is_deleted == 0,
            )
            .values(rag_task_id=rag_task_id, updated_at=datetime.now())
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def set_ontology_status(
        self,
        doc: KnowledgeDocument,
        status: str | OntologyStatus,
        *,
        error: str | None = None,
        increment_retry: bool = False,
        applied_schema_version: int | None = None,
        applied_schema_hash: str | None = None,
        emit_task: bool = True,
    ) -> KnowledgeDocument:
        """更新本体状态。

        [jonex] 数据运营二期：`emit_task=False` 用于**不是真的跑了一次知识抽取**
        的调用方（目前只有 wiki 上传那条强制置 READY 的路径 —— 它压根没走
        抽取，落一行 `extract/success` 会让「知识抽取任务数」凭空多出来）。
        """
        fresh = await self.get_by_id(doc.id, doc.tenant_id)
        prev_status = fresh.ontology_status if fresh is not None else None
        if fresh is None:
            fresh = await self.session.merge(doc)
        fresh.ontology_status = _value(status)
        if error is not None:
            fresh.ontology_error = error
        if increment_retry:
            fresh.ontology_retry_count = (fresh.ontology_retry_count or 0) + 1
        # [jonex] P1-E：Neo4j 写成功后记录已应用的 schema 版本/hash
        if applied_schema_version is not None:
            fresh.ontology_applied_schema_version = applied_schema_version
        if applied_schema_hash is not None:
            fresh.ontology_applied_schema_hash = applied_schema_hash
        await self.session.flush()
        # [jonex] 数据运营二期：知识抽取任务埋点（extract）。
        _task_status = EXTRACT_STATUS_MAP.get(fresh.ontology_status)
        if emit_task and _task_status and _value(prev_status) != fresh.ontology_status:
            await record_processing_task(
                self.session, doc.tenant_id,
                asset_id=fresh.id, task_type=TASK_TYPE_EXTRACT, status=_task_status,
                doc_id=fresh.id, kb_id=fresh.knowledge_base_id,
                content_generation=fresh.content_generation,
                error=fresh.ontology_error if _task_status == TASK_STATUS_FAILED else None,
            )
        return fresh

    async def count_by_knowledge_bases(self, tenant_id: str, kb_ids: list[str]) -> dict[str, int]:
        """按知识库批量统计未删除文档数（接口1 KB 维度的 document_count）。

        返回 {knowledge_base_id: count}，无文档的 KB 不出现（调用方按 0 兜底）。
        """
        if not kb_ids:
            return {}
        deduped = list(dict.fromkeys(kb_ids))
        stmt = (
            select(KnowledgeDocument.knowledge_base_id, func.count())
            .where(
                *self._tenant_conditions(tenant_id),
                *self._soft_delete_conditions(),
                KnowledgeDocument.knowledge_base_id.in_(deduped),
            )
            .group_by(KnowledgeDocument.knowledge_base_id)
        )
        rows = await self.session.execute(stmt)
        return {row[0]: row[1] for row in rows.all()}

    async def count_by_source_type(self, tenant_id: str, knowledge_base_id: str) -> dict[str, int]:
        """按来源方式（data_source_type 冗余真实列）统计某 KB 未删除文档数（接口2）。

        返回 {data_source_type: count}，例如 {"file": 12, "api": 3}。
        数据源列表按各自 access_type 命中对应计数；同一 KB 多个同类型数据源
        会共享该类型计数（按"来源方式"口径统计）。
        """
        stmt = (
            select(KnowledgeDocument.data_source_type, func.count())
            .where(
                *self._tenant_conditions(tenant_id),
                *self._soft_delete_conditions(),
                KnowledgeDocument.knowledge_base_id == knowledge_base_id,
                KnowledgeDocument.data_source_type.is_not(None),
            )
            .group_by(KnowledgeDocument.data_source_type)
        )
        rows = await self.session.execute(stmt)
        return {row[0]: row[1] for row in rows.all()}

    async def count_by_folder(self, tenant_id: str, knowledge_base_id: str) -> dict[str, int]:
        """按文件夹统计某 KB 未删除文档数（目录树 document_count）。

        返回 {folder_id: count}；未分类（folder_id IS NULL）归入 UNCLASSIFIED_SENTINEL。
        无文档的文件夹不出现（调用方按 0 兜底）。
        """
        stmt = (
            select(KnowledgeDocument.folder_id, func.count())
            .where(
                *self._tenant_conditions(tenant_id),
                *self._soft_delete_conditions(),
                KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            )
            .group_by(KnowledgeDocument.folder_id)
        )
        rows = await self.session.execute(stmt)
        result: dict[str, int] = {}
        for folder_id, cnt in rows.all():
            key = folder_id if folder_id is not None else UNCLASSIFIED_SENTINEL
            result[key] = int(cnt)
        return result

    async def get_by_ids(self, doc_ids: list[str], tenant_id: str) -> list[KnowledgeDocument]:
        """批量按 id 查询（带 tenant_id 与 is_deleted==0），用于 references 富化。

        D8：解析 doc_id 后必须经此方法校验归属；跨租户/不存在的 doc_id 一律剔除。
        """
        if not doc_ids:
            return []
        deduped = list(dict.fromkeys(doc_ids))
        conditions = [
            self._primary_key().in_(deduped),
            *self._tenant_conditions(tenant_id),
            *self._soft_delete_conditions(),
        ]
        result = await self.session.execute(
            select(KnowledgeDocument).where(*conditions)
        )
        return list(result.scalars())

    # [jonex] 阶段2：批量重置 KB 下文档为本体待抽取状态（对账被动重抽）
    async def reset_ontology_for_kb(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        *,
        document_ids: list[str] | None = None,
        only_ready: bool = True,
        only_outdated: bool = False,
        target_schema_version: int | None = None,
    ) -> dict:
        """把某 KB 下命中文档的本体状态重置为 pending，触发对账重抽（C→B 联动）。

        [jonex] 阶段2（扩展复用 P1-G）：
        - document_ids 非空 → 仅这些文档；
        - only_ready=True → 仅 status=READY 文档（默认，避免打断解析中文档）；
        - only_outdated=True → 仅 applied 版本为空或低于 target 的文档（P1-E）；
        - target_schema_version 非空 → 同时写入 ontology_target_schema_version。
        返回 {matched, reset, skipped}。
        """
        from sqlalchemy import ARRAY, String, func, literal, or_, select, update
        from sqlalchemy.dialects.postgresql import JSONB

        from ..ontology_write_state import ONTOLOGY_WRITE_STATE_KEYS

        conditions = [
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.is_deleted == 0,
        ]
        if only_ready:
            conditions.append(KnowledgeDocument.status == DocStatus.READY.value)
        if document_ids:
            conditions.append(KnowledgeDocument.id.in_(list(dict.fromkeys(document_ids))))
        if only_outdated and target_schema_version is not None:
            conditions.append(
                or_(
                    KnowledgeDocument.ontology_applied_schema_version.is_(None),
                    KnowledgeDocument.ontology_applied_schema_version < target_schema_version,
                )
            )

        # matched：命中条件的文档总数（用于与 reset 对比出 skipped）
        matched = (
            await self.session.execute(
                select(func.count()).select_from(KnowledgeDocument).where(*conditions)
            )
        ).scalar_one()

        values = {
            "ontology_status": OntologyStatus.PENDING.value,
            "ontology_retry_count": 0,
            "ontology_error": None,
            # [jonex] 整库重置 = 换任务：写图状态键一并清掉（jsonb - text[]）。
            # 必须用 ARRAY(String)：用 String 时 bind 类型是 VARCHAR，列表会被
            # 当字符串传，运行期才炸。实测渲染 extra_metadata - %(param_1)s。
            "extra_metadata": KnowledgeDocument.extra_metadata.op("-", return_type=JSONB)(
                literal(list(ONTOLOGY_WRITE_STATE_KEYS), type_=ARRAY(String))
            ),
        }
        if target_schema_version is not None:
            values["ontology_target_schema_version"] = target_schema_version

        result = await self.session.execute(
            update(KnowledgeDocument).where(*conditions).values(**values)
        )
        await self.session.flush()
        reset = result.rowcount or 0
        return {"matched": int(matched or 0), "reset": reset, "skipped": max(0, int(matched or 0) - reset)}


__all__ = ["KnowledgeDocumentRepository"]
