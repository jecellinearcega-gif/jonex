#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""运行方清单（平台内置，不做注册）。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §3.4、§九。

「有哪些运行方可用」由平台预置在 `task_center.runner_definitions` 里，**不是运行方自己
来注册**——编排步骤绑定的 `runner_code` 必须能在册，发版校验（§6.5 第 3 条）就照着本清单比。
一期在册的就一个：`knowledge_base`。

本模块只做「读清单 + 按 code 换 capability_id」这一件事，不碰状态、不写库；
派活怎么发出去是 `runner_adapter.py` 的事。
"""

from typing import Dict, List

from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate

from ..dtos.runner import RunnerItem
from ..models import RunnerDefinition
from ..repository.runner_repository import RunnerDefinitionRepository


class RunnerRegistry:
    """运行方清单：只读、平台内置。"""

    def __init__(self, session: AsyncSession):
        self._repository = RunnerDefinitionRepository(session)

    async def list_runners(self) -> List[RunnerItem]:
        """列全部在册运行方（§4.3 `runner.list`）。

        `id` 回**逻辑键 `code`**，不是 DB 主键 —— `runner.get` 只认逻辑键，两个接口
        的 `id` 必须是同一个东西，否则「list 里挑一条、拿它的 id 去 get 详情」会被拒。
        主键（`runner-knowledge-base` 之类）是内部实现细节，对外没有意义。
        """
        return [self._to_item(row) for row in await self._repository.list_runners()]

    @staticmethod
    def _to_item(row: RunnerDefinition) -> RunnerItem:
        """ORM 行 → DTO；`id` 覆写成逻辑键（口径与 `get_runner` 一致）。"""
        return RunnerItem(**{**row.to_dict(), "id": row.code})

    async def get_runner(self, code: str) -> RunnerItem:
        """按 code 取一个；**不在册直接拒**（§4.3 `runner.get`）。

        文档给这一格的处置是「**拒绝**（参数错）」—— 内置运行方清单是**全局只读**的，
        不涉及越权，所以不按「找不到」处理（越权才按"找不到"，那是有意不暴露"存在但
        不属于你"），直接给 `InvalidParameterError`。

        ⚠️ **别把这里改回返回 `None`**：上一个版本就是那样，`capability.py` 的
        `runner.get` 拿 `None` 一路透传出去，最后回的是 `success=true` + `data=null`
        —— 调用方以为查到了、拿到空对象。要「查不到给 None」的调用方用
        `get_by_code`（仓储层原本就是那个语义）。
        """
        row = await self._repository.get_by_code(code)
        if row is None:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.runner.not_found",
                    params={"code": code},
                    fallback=f"运行方 {code} 不在平台内置清单里",
                ),
                details={"code": code},
            )
        return self._to_item(row)

    async def require_runner(self, code: str) -> RunnerDefinition:
        """按 code 取一个；不在册直接拒（§4.3 `runner.get` 的同一条判据）。

        派活路上用这个：拿不到运行方就没法派，必须显式失败而不是静默跳过。

        **与 `get_runner` 同一口径**：同一张全局只读表、同一个 `row is None`，
        所以码与文案都跟着它走 —— `InvalidParameterError`（1001）+ `translate`。
        早先这里回的是 `ResourceNotFoundError`（1002）+ 裸中文字面量，两处后果：
        ① `X-Lang: en-US` 的调用方拿到中文（同文件 `get_runner` 同一句话能出英文）；
        ② 同一个判据两个码，「参数错」还是「找不到」按入口不同而不同。
        `get_runner` 的 docstring 已经写死了选 1001 的理由（清单全局只读、不涉及越权），
        那条理由对派活路**同样成立**，所以这里跟着它走。
        """
        row = await self._repository.get_by_code(code)
        if row is None:
            raise InvalidParameterError(
                message=translate(
                    "err.task_center.runner.not_found",
                    params={"code": code},
                    fallback=f"运行方 {code} 不在平台内置清单里",
                ),
                details={"code": code},
            )
        return row

    async def map_by_code(self) -> Dict[str, RunnerDefinition]:
        """按 code 建索引：发版校验要逐步骤比对运行方是否在册。"""
        return await self._repository.map_by_code()

    async def resolve_capability_id(self, code: str) -> str:
        """把步骤绑定的 `runner_code` 换成运行方对外的能力 ID。

        管理方靠这个 ID 找到对应 client 去派活（§4.1 ①「按步骤绑定的运行方选 client」）。
        """
        return (await self.require_runner(code)).capability_id


__all__ = ["RunnerRegistry"]
