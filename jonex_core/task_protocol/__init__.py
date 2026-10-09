#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务运行协议层：跨进程报文的定义、四类结果与错误码判定（§4.0、§4.1、§4.2）。

**这一层谁也不依赖**（只有标准库 + pydantic v1 + `jonex_core.common.exceptions`），
所以任务中心服务端、运行方 SDK、以后的可观测模块都能安全 import 它。

**门禁一条**：本包（含 `models` / `results` / `errors`）不得 import `capabilities.*`、
不得 import `jonex_core.capability.*`（含 `ApiResponse`）——
`tests/unit/task_protocol/test_protocol_imports.py` 用 `sys.modules` 在**子进程**里逐个
子模块钉住这条（包 / `models` / `results` / `errors` 四条）。
⚠️ **「本层不写日志」是设计约定、不是门禁**（当前代码里确实一个 `logging` 都没有）：
`sys.modules` 钉不住它 —— `jonex_core.common.exceptions` 那一条依赖本来就一路拉进
`jonex_core.common/__init__.py` 的全部基础依赖，其中就有 logging；再断言「没有 logging」
只会恒假。要改这条约定得连下面那句依赖说明一起改。

⚠️ 依赖 `jonex_core.common.exceptions` 会连带 `jonex_core.common` 包的全部基础依赖
（那个包的 `__init__.py` 是饿汉式的）—— **别拿"能 import 本包"当作"能脱离 DB/Redis
运行"的凭据**。门禁断言的是「没有 capabilities / jonex_core.capability」，不是
「没有 sqlalchemy / logging」。

用法：

```python
from jonex_core.task_protocol import (
    ExecutionDispatch,
    TaskSubmission,
    dispatch_disposition,
    submission_disposition,
    subtask_retry_disposition,
    terminal_disposition,
)

# 派活回执怎么读（管理方侧）
verdict = dispatch_disposition(resp.success, resp.code)
if verdict is DispatchDisposition.DELIVERY_UNKNOWN:
    ...  # 结果未知：别当明确失败判死
         # （例外：`DispatchResult.dead_after_retry` 那一类 —— 「对方那一侧持续不通」
         #  或「运行方自己明确回了失败码」，重发试满预算后判死；判据是那个字段，
         #  不是这个 disposition 本身）

# 上报回执怎么读（运行方侧）
verdict = terminal_disposition(resp.success, resp.code)

# 「要求重跑某一步」的回执怎么读（运行方侧，§4.2 ③）
verdict = subtask_retry_disposition(resp.success, resp.code)
```

「谁实现、谁调用」的方向见 `models` 里各报文的 docstring；SDK 门面在
`jonex_core.task_runtime`。
"""

from .errors import (
    ExecutionContextMissingError,
    TaskProtocolError,
    TaskRuntimeNotBuiltError,
    UnknownStepCodeError,
)
from .models import (
    ExecutionDispatch,
    ExecutionEnvelope,
    RemoveTaskDispatch,
    TaskStoppedDispatch,
    TaskItemResponse,
    TaskSubmission,
)
from .results import (
    CLASS_ALREADY_EXISTS,
    CLASS_OK,
    CLASS_REJECTED,
    CLASS_UNKNOWN,
    OUTBOUND_RETRY_BACKOFF_SEC,
    RETRY_BACKOFF_SEC,
    RETRY_MAX_ATTEMPTS,
    TASK_CENTER_ALREADY_EXISTS_CODES,
    TASK_CENTER_REJECTED_CODES,
    TASK_CENTER_RETRYABLE_CODES,
    DispatchDisposition,
    DispatchResult,
    SubmissionDisposition,
    SubmissionResult,
    SubtaskRetryDisposition,
    SubtaskRetryResult,
    TaskRef,
    TerminalSubmitDisposition,
    TerminalSubmitResult,
    classify_code,
    dispatch_disposition,
    retry_delay,
    submission_disposition,
    subtask_retry_disposition,
    terminal_disposition,
)

__all__ = [
    "CLASS_ALREADY_EXISTS",
    "CLASS_OK",
    "CLASS_REJECTED",
    "CLASS_UNKNOWN",
    "DispatchDisposition",
    "DispatchResult",
    "ExecutionContextMissingError",
    "ExecutionDispatch",
    "ExecutionEnvelope",
    "OUTBOUND_RETRY_BACKOFF_SEC",
    "RETRY_BACKOFF_SEC",
    "RETRY_MAX_ATTEMPTS",
    "RemoveTaskDispatch",
    "TaskStoppedDispatch",
    "SubmissionDisposition",
    "SubmissionResult",
    "SubtaskRetryDisposition",
    "SubtaskRetryResult",
    "TASK_CENTER_ALREADY_EXISTS_CODES",
    "TASK_CENTER_REJECTED_CODES",
    "TASK_CENTER_RETRYABLE_CODES",
    "TaskItemResponse",
    "TaskProtocolError",
    "TaskRef",
    "TaskRuntimeNotBuiltError",
    "TaskSubmission",
    "TerminalSubmitDisposition",
    "TerminalSubmitResult",
    "UnknownStepCodeError",
    "classify_code",
    "dispatch_disposition",
    "retry_delay",
    "submission_disposition",
    "subtask_retry_disposition",
    "terminal_disposition",
]
