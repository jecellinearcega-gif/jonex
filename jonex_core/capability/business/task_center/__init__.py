#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务中心 Client 包（`business.task_center.v1`）。

接入方（运行方，如知识库）统一从这里拿 Client：

```python
from jonex_core.capability.business.task_center import (
    TASK_CENTER_RETRYABLE_CODES,
    get_task_center_client,
)

client = get_task_center_client()
resp = await client.init_task(tenant_id, definition_id, space_id, {"kb_id": "..."})
if not resp.success:
    # `success=False` 不带原因，要不要原样重发只看 `code`（§4.0）。
    if resp.code in TASK_CENTER_RETRYABLE_CODES:
        ...  # 过一会儿原样重发
```

**这个 client 一共九条**：§4.1 那**三个**由运行方实现（`execute` / `remove_task` /
`task_stopped`）+ §4.2 那**四个写**由运行方调用（`init_task` / `report_status` /
`retry_subtask` / `retry_subtasks`）+ §4.3 的**两个读**（`get_subtask` / `get_task`，
2026-09-30 开给运行方），§4.3 其余的内部函数一个都不在。
双向契约的说明见 `TaskCenterClient` 的 docstring，实现细节与返回契约见 `client.py`。
"""

from .client import (
    TASK_CENTER_CAPABILITY_ID,
    TASK_CENTER_RETRYABLE_CODES,
    LocalTaskCenterClient,
    MockTaskCenterClient,
    RemoteTaskCenterClient,
    TaskCenterClient,
    get_task_center_client,
)

__all__ = [
    "LocalTaskCenterClient",
    "MockTaskCenterClient",
    "RemoteTaskCenterClient",
    "TASK_CENTER_CAPABILITY_ID",
    "TASK_CENTER_RETRYABLE_CODES",
    "TaskCenterClient",
    "get_task_center_client",
]
