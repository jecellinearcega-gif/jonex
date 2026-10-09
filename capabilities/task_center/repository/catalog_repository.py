#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""内置清单（任务 / 步骤）仓储。

平台预置、只读、不带租户，所以走 `*_shared` 系列（不参与租户隔离）。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.repository import BaseRepository

from ..models import TaskCatalog, TaskCatalogStep


class TaskCatalogRepository(BaseRepository[TaskCatalog]):
    """内置任务清单仓储。"""

    model = TaskCatalog

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_catalogs(self) -> list[TaskCatalog]:
        stmt = select(TaskCatalog).order_by(TaskCatalog.code)
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def get_catalog(self, task_catalog_code: str) -> TaskCatalog | None:
        """按**逻辑键 `code`** 取一行 —— **不是按主键**（`uq_tc_catalog_code` 保证唯一）。

        ⚠️ 这里曾经是 `get_by_id_shared(task_catalog_code)`，那条路是**死的**：主键 `id`
        是 `catalog-file-processing-ontology`，而全平台引用这个目录用的都是 `code`
        （`file_processing_ontology`）—— 入参名、内置步骤的 `task_catalog_code`、定义行的
        `task_catalog_code`、`COMMENT` 里写的「逻辑键」，无一例外。两个值都活不了：

        - 传 `code`（所有人都在传它）→ 按主键查不到 → `_require_catalog` 恒报「清单里没有」，
          `def.create` / `def.catalog_steps` 两个入口整体不可用；
        - 传主键 → 校验过得去，但下游 `list_by_catalog` 是按列值过滤、列里存的是 `code`
          → **恒返回空**，发版校验「步骤合法」一律打回。

        同族的 `RunnerDefinitionRepository.get_by_code` 本来就是按逻辑键查的
        （`flows/registry.py` 也按那个语义用），这里对齐它。
        """
        stmt = select(TaskCatalog).where(TaskCatalog.code == task_catalog_code)
        result = await self.session.execute(stmt)
        return result.scalars().first()


class TaskCatalogStepRepository(BaseRepository[TaskCatalogStep]):
    """内置步骤清单仓储。"""

    model = TaskCatalogStep

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_by_catalog(self, task_catalog_code: str) -> list[TaskCatalogStep]:
        stmt = (
            select(TaskCatalogStep)
            .where(TaskCatalogStep.task_catalog_code == task_catalog_code)
            .order_by(TaskCatalogStep.step_code)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def map_by_step_code(self, task_catalog_code: str) -> dict[str, TaskCatalogStep]:
        """按 step_code 建索引：发版校验要逐步骤比对，一次查完避免 N+1。"""
        return {row.step_code: row for row in await self.list_by_catalog(task_catalog_code)}


__all__ = ["TaskCatalogRepository", "TaskCatalogStepRepository"]
