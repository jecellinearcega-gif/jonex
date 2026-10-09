#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""`ExecutionRegistry` 端口的持久实现（落 `knowledge_base.task_execution_registry`）。

端口与内存实现的契约写在 `jonex_core/task_runtime/registration.py`；本文件只负责
「把那一对 claim / release 落到 PG 上」。

**为什么知识库必须自己带一个持久实现**：SDK 默认给的 `InMemoryExecutionRegistry`
**进程重启即全丢、多副本之间互相看不见**。而任务中心**确实会重派**同一个
`execution_id`（出站没够着运行方时当轮同步重发，最多 3 次），任务又跑得动辄几十分钟
（`RAG_TASK_HARD_TIMEOUT_SEC` 默认 21600 秒）—— 重启窗口是真实存在的，落在窗口里的
重派会**重复建下游 atomic-rag 任务、重复扣配额、产出重复向量**。

所以知识库在 `KnowledgeBaseCapability.initialize()` 里**自检** `persistent`：
漏传 registry 不会报错、只会静默降级成内存实现，那种失败必须在启动时就被拦下。

## 为什么每次调用自己开一个 session

`claim` / `release` 由 `RunnerBase.execute` 在**任意请求路径**里调用，调用点上没有现成的
session 可传；而登记行的写入必须是**独立提交**的 —— 跟调用方那笔业务事务绑在一起的话，
业务事务回滚会把这笔登记一起抹掉，于是「已经受理过」变成假的，重派会重复干活。

每次调用开一个短会话、成功即提交（`get_db_session()` 的语义），代价是一次连接借还，
换的是「登记一旦写下就不会被别的事务反悔」。
"""

from typing import Callable, Optional

from jonex_core.common.database import get_db_session
from jonex_core.task_protocol.models import ExecutionDispatch
from jonex_core.task_runtime.registration import ClaimOutcome

from ..repository.execution_registry_repository import ExecutionRegistryRepository


class PostgresExecutionRegistry:
    """落 PG 的执行登记（`ExecutionRegistry` 协议的实现，**不是继承** —— 端口是 Protocol）。

    ⚠️ 端口是 `runtime_checkable` 的 `Protocol`，但**这里不要做 `isinstance` 判断**：
    `runtime_checkable` 对带非方法成员（`persistent` 是普通属性）的协议只检查方法名，
    属性查不出来；真正的契约校验走知识库启动时那句 `registry.persistent` 自检。
    """

    #: 兑现端口上的 `persistent`。知识库启动时自检的就是它
    #: （`capabilities/knowledge_base/capability.py` 的 `initialize()`）。
    persistent = True

    def __init__(self, session_factory: Optional[Callable] = None) -> None:
        #: 可注入，便于测试换成事务内会话（默认就是生产用的 `get_db_session`）。
        self._session_factory = session_factory or get_db_session

    async def claim(self, execution: ExecutionDispatch) -> ClaimOutcome:
        """查并占。**原子性由那条 `INSERT ... ON CONFLICT ... RETURNING` 保证**
        （见 `ExecutionRegistryRepository.claim`）。"""
        async with self._session_factory() as session:
            claimed = await ExecutionRegistryRepository(session).claim(execution)
        return ClaimOutcome.NEW if claimed else ClaimOutcome.ALREADY_TAKEN

    async def release(self, execution: ExecutionDispatch) -> None:
        """撤销登记（幂等）。

        ⚠️ **`ALREADY_TAKEN` 那一档不释放**：走那一档说明另一次调用正持有这次执行，
        释放等于把别人的登记拆了 —— 后到的重派会拿到 `NEW`、重复干活。
        `RunnerBase.execute` 只在「handler 抛异常 / 返回值形状错 / handler 明确拒」三条
        路上调它，那三条都在 `NEW` 分支里，所以这里不需要再判一次。
        """
        async with self._session_factory() as session:
            await ExecutionRegistryRepository(session).release(execution)


__all__ = ["PostgresExecutionRegistry"]
