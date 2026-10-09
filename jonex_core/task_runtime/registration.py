#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""执行登记：认出「这个 `execution_id` 我接过没有」（§4.1 ①、参考文档 §4.2 `pre_claim`）。

**为什么需要它**：管理方**确实会重派**同一次执行。派活是「投递 + 回执」，出站**没够着
运行方**（连不上 / 读超时 / 响应不是 JSON —— 也就是它不知道你到底收没收到）时，它会在
同一个后台循环里**同步重发**，最多 3 次（`_dispatch_with_retry`，
`capabilities/task_center/services/scheduler_service.py`）。运行方要是认不出重复派活，
就会**重复干活** —— 重复建下游任务、重复扣配额。

契约只有一句话：**同一个 `execution_id` 重复到达，一律当「已受理」回成功，不重复干活。**

**两档，没有第三档**：

| 档 | 含义 | 运行方该做什么 |
| --- | --- | --- |
| `NEW` | 没接过 | 登记 + 干活 |
| `ALREADY_TAKEN` | 接过了 | 直接回成功 |

`STALE`（旧执行回来改数据）那一档**本轮不做** —— 它要运行方那张文档表的版本 CAS 一起
上，按加法再补。

**已知窗口**（本轮接受，由管理方超时巡检兜底）：「登记了、但下游还没建」之间进程崩掉时，
重启后管理方重派会拿到 `ALREADY_TAKEN`、直接回成功，可这一步其实没人在跑 ——
管理方会一路等到 `timeout_seconds`（内置步骤 25200 秒）才判死。补它需要「按执行编号
查下游」那一档，留给运行方那轮。

**键只取 `execution_id`**：它是管理方用 `uuid4()` 生成的，事实上全局唯一（生成点：
`capabilities/task_center/services/task_service.py` 的 `TaskService.claim_for_dispatch`，
「先抢占再派活」那一步里跟着 `claim_pending` 一起落库），所以不需要再拼租户 / 空间。
这也正是参考文档 §4.3 拿 `task-execution:{execution_id}` 当跨进程幂等键的前提。
"""

import logging
from enum import Enum
from typing import Dict, Protocol, runtime_checkable

from jonex_core.task_protocol.models import ExecutionDispatch

logger = logging.getLogger(__name__)


class ClaimOutcome(str, Enum):
    """`claim()` 的结论。**两档**（见模块 docstring）。"""

    NEW = "NEW"
    """没接过这次执行 —— 登记好了，去干活。"""

    ALREADY_TAKEN = "ALREADY_TAKEN"
    """接过这次执行 —— 别重复干活，**直接回成功**。

    **「已经受理过」「正在跑」「已经跑完」都归这一档**：登记写在干活**之前**，所以那次
    正在跑的调用的登记早就占了，重派撞上的正是它。回失败等于告诉管理方「这一步没人接」，
    它会判死一个其实正在跑的子任务（终态 `failed`、下游一律 hold）。
    """


@runtime_checkable
class ExecutionRegistry(Protocol):
    """执行登记端口。

    ⚠️ **`claim` 必须是原子的「查 + 占」**：先查有没有、没有就登记，这两步之间不能
    让另一个协程插进来，否则并发重派会双双拿到 `NEW`、双双干活。持久实现里这就是
    一次带条件的写入（`INSERT ... ON CONFLICT DO NOTHING` 或条件 `UPDATE`）。
    """

    #: 这个实现是不是落盘的（**重启与多副本之后登记还在**）。
    #:
    #: **它是端口的一部分，不是内存实现的私事** —— `RunnerBase.registry` 把这个实现
    #: 暴露出来，**由运行方自己在启动时自检**（`runner.py` 的 `registry` property：「给
    #: 自检用，比如启动时断言『我用的是不是持久实现』」）；SDK 不内置那句断言 ——
    #: 因为默认就是内存实现，内置成硬断言会让开箱即用直接跑不起来。
    #: 不声明的话，一个忘了带这个属性的持久实现会让运行方那句自检抛 `AttributeError` ——
    #: 明明装对了，看着却像装坏了。**内存实现必须显式写 `False`**，别靠"没有就是假"。
    persistent: bool

    async def claim(self, execution: ExecutionDispatch) -> ClaimOutcome:
        """查并占：没接过就登记并回 `NEW`，接过了回 `ALREADY_TAKEN`。"""
        ...

    async def release(self, execution: ExecutionDispatch) -> None:
        """撤销登记（幂等）。

        **必须调用的时机**：这次受理最终**没成** —— handler 明确拒绝、handler 抛异常、
        或者步骤名不认识。不撤销的话，这次明确失败会被永久记成「已受理」，
        管理方下次重派时直接回成功，**把一次真实的失败吞掉**。
        """
        ...


class InMemoryExecutionRegistry:
    """进程内的执行登记 —— **开发与单副本用的默认实现**。

    ⚠️ **它不持久**：进程重启即全丢，多副本之间也互相看不见。任务跑动辄几十分钟
    （`RAG_TASK_HARD_TIMEOUT_SEC` 默认 21600 秒），重启窗口是真实存在的 —— 那个窗口里
    重派会重复干活。**上生产必须换成落运行方自己库的持久实现。**

    构造时会打一条 warning 提醒这件事；不想要这条日志就在装配时显式传一个自己的
    registry（`RunnerBase(registry=...)`）。

    条数上限 `max_entries`（默认 10000，按插入序淘汰最旧的）是为了防长期运行时无界增长。
    被淘汰掉的旧执行再被重派会重新干活 —— 那是内存实现的固有局限，不是额外引入的问题。
    """

    #: 兑现 `ExecutionRegistry.persistent` —— 内存实现**必然**是 `False`（见类 docstring）。
    #: 运行方启动时自检的就是它（`runner.registry.persistent`）；**那句断言在运行方那边，
    #: 不在 SDK 里**（理由见端口上那条注释）。
    persistent = False

    def __init__(self, max_entries: int = 10000) -> None:
        self._taken: Dict[str, None] = {}
        self._max_entries = max_entries
        logger.warning(
            "InMemoryExecutionRegistry 生效中：执行登记只在进程内存里，"
            "重启与多副本都会失效 —— 生产环境请传一个持久实现。"
        )

    async def claim(self, execution: ExecutionDispatch) -> ClaimOutcome:
        key = execution.execution_id
        if key in self._taken:
            return ClaimOutcome.ALREADY_TAKEN
        # 下面两步之间没有 await，所以在一个事件循环里是原子的（真正的原子性由
        # 这是单线程事件循环保证；跨副本要靠持久实现的条件写）。
        self._taken[key] = None
        if len(self._taken) > self._max_entries:
            self._taken.pop(next(iter(self._taken)))
        return ClaimOutcome.NEW

    async def release(self, execution: ExecutionDispatch) -> None:
        self._taken.pop(execution.execution_id, None)


__all__ = [
    "ClaimOutcome",
    "ExecutionRegistry",
    "InMemoryExecutionRegistry",
]
