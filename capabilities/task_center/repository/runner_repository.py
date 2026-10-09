#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""运行方清单仓储（平台内置、只读、不带租户）。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.repository import BaseRepository

from ..models import RunnerDefinition


class RunnerDefinitionRepository(BaseRepository[RunnerDefinition]):
    """运行方清单仓储。"""

    model = RunnerDefinition

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_runners(self) -> list[RunnerDefinition]:
        stmt = select(RunnerDefinition).order_by(RunnerDefinition.code)
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def get_by_code(self, code: str) -> RunnerDefinition | None:
        stmt = select(RunnerDefinition).where(RunnerDefinition.code == code)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def exists(self, code: str) -> bool:
        return await self.get_by_code(code) is not None

    async def map_by_code(self) -> dict[str, RunnerDefinition]:
        """按 code 建索引：发版校验要逐步骤比对运行方是否在册。"""
        return {row.code: row for row in await self.list_runners()}


__all__ = ["RunnerDefinitionRepository"]
