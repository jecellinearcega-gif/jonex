#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""执行登记仓储（`knowledge_base.task_execution_registry`）——
`jonex_core.task_runtime.registration.ExecutionRegistry` 端口的持久实现所依赖的原子读写。

## 为什么不继承 `BaseRepository`

那张表**故意没有 `id`、也没有 `tenant_id`**：键就是 `execution_id`（任务中心用
`uuid4()` 生成、事实上全局唯一，端口的 docstring 已钉死这一点）。基类的
`_primary_key()` 取 `self.model.id`、`_tenant_conditions()` 按 `tenant_id` 过滤，
两个前提在这里都不成立 —— 继承过来只会得到一个「调了就抛 AttributeError」的基类。

## 为什么 `claim` 必须是**一条** SQL

`ExecutionRegistry.claim` 的契约是「原子的查 + 占」：查有没有、没有就登记，两步之间
不能让另一个协程插进来，否则并发重派会双双拿到 `NEW`、双双干活。SQLAlchemy 里分两句
写（先 `SELECT` 再 `INSERT`）中间是有 `await` 的，多副本下必错。所以压成一条
`INSERT ... ON CONFLICT DO UPDATE`，用 `RETURNING` 的行数当结论。
"""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.task_protocol.models import ExecutionDispatch

#: 原子领取。
#:
#: `ON CONFLICT ... WHERE released_at IS NOT NULL` 是这套东西的关键：
#:
#: - 没这行 → 冲突时**什么都不做**（返回 0 行）= 「别人正持有」= `ALREADY_TAKEN`
#: - 冲突且**已释放** → 重新占下 = 新的一次领取
#:
#: 「已释放的可再占」是**重发安全性的前提**：任务中心在 `DELIVERY_UNKNOWN` 下会原样
#: 重发（最多 3 次），重发时 `execution_id` 不变。要是把「登记过」一律判成
#: `ALREADY_TAKEN`，我们自己释放掉的那次失败就再也接不回来了 —— 重发全被自己人挡住，
#: 子任务一路卡到管理方超时判死。
_CLAIM_SQL = text(
    """
    INSERT INTO knowledge_base.task_execution_registry
        (execution_id, tenant_id, space_id, task_id, step_code,
         claimed_at, released_at, claim_count, created_at, updated_at)
    VALUES
        (:execution_id, :tenant_id, :space_id, :task_id, :step_code,
         :now, NULL, 1, :now, :now)
    ON CONFLICT (execution_id) DO UPDATE
       SET released_at = NULL,
           claimed_at  = :now,
           updated_at  = :now,
           claim_count = knowledge_base.task_execution_registry.claim_count + 1
     WHERE knowledge_base.task_execution_registry.released_at IS NOT NULL
    RETURNING execution_id
    """
)

#: 释放登记（幂等：重复调用只是把同样两列再写一次同样的值）。
#:
#: 只在**这次受理最终没成**时调（handler 明确拒绝 / 抛异常 / 步骤名不认识）——
#: 不撤销的话，一次真实失败会被永久记成「已受理」，管理方下次重派直接回成功，
#: 把失败吞掉。
_RELEASE_SQL = text(
    """
    UPDATE knowledge_base.task_execution_registry
       SET released_at = :now, updated_at = :now
     WHERE execution_id = :execution_id
    """
)


class ExecutionRegistryRepository:
    """`task_execution_registry` 的两条语句。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def claim(self, execution: ExecutionDispatch) -> bool:
        """领这次执行。`True` = 领到了（端口侧的 `NEW`），`False` = 别人正持有。

        ⚠️ 时间参数用 Python 的 `datetime.now()` 统一传进去（不是 SQL 里的
        `CURRENT_TIMESTAMP`）：本表的时间列虽然不参与任何判定，但同一个进程里
        两套时钟混着写迟早会有人拿它们作差。口径与 `TimestampMixin`（`datetime.now`）
        以及既有对账代码保持一致。
        """
        result = await self.session.execute(
            _CLAIM_SQL,
            {
                "execution_id": execution.execution_id,
                "tenant_id": execution.tenant_id or None,
                "space_id": execution.space_id or None,
                "task_id": execution.task_id or None,
                "step_code": execution.step_code or None,
                "now": datetime.now(),
            },
        )
        await self.session.flush()
        return result.first() is not None

    async def release(self, execution: ExecutionDispatch) -> None:
        """撤销登记。

        **幂等**：第二遍进来只是把 `released_at` 再写一次。这一点必须成立 ——
        `RunnerBase.execute` 在「handler 抛异常」与「显式拒」两条路上都会调它，
        而任务中心的重发会让同一条路走第二遍。
        """
        await self.session.execute(
            _RELEASE_SQL,
            {"execution_id": execution.execution_id, "now": datetime.now()},
        )
        await self.session.flush()


__all__ = ["ExecutionRegistryRepository"]
