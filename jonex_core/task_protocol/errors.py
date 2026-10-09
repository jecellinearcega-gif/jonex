#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""协议层的稳定异常（§4.1、§4.2）。

继承 `JonexException` 是为了**错误码体系不分叉** —— 接入方与顶层异常处理器拿到的是
同一套东西，不需要认一个新基类。`jonex_core/common/exceptions.py` 本身只依赖 `typing`，
引它不额外拉东西。

**码段取 11xx**（1xxx 通用错误段内的细分）：这几条都是**进程内的编程错误 / 装配错误**
—— 单例没建、上下文没绑、步骤没登记 —— 不是业务裁决。

⚠️ **但它们确实会落进「能不能原样重发」那套判定，而且落的是「未知」那一档**：
这两个宿主能力都是**先** `except JonexException` 原样回 `exc.code`、**再**才 `except
Exception` 兜 500（`capabilities/task_center/capability.py`、`capabilities/knowledge_base/
capability.py`），所以 1100~1103 会原样出现在回执里；而远端派活读的正是信封的 `code`
（`runner_adapter` 的 `dispatch_disposition`），三个码表里都没有它们 → `CLASS_UNKNOWN`
→ `DELIVERY_UNKNOWN` → **按「未知」重发满预算**。
（从前这里写的是「会被兜成 500、不会带进失败壳」—— **两句都不成立**，已按实况改准。）
⚠️ 最终处置（重发用尽后判死）与「明确拒绝」一致，差别只在**多试两次**；所以这是**文档级**的
坑：照旧说法去理解，会以为 11xx 与重发无关。
"""

from typing import Iterable, Optional

from jonex_core.common.exceptions import JonexException


class TaskProtocolError(JonexException):
    """任务运行协议错误基类。"""

    code = 1100
    status_code = 500
    default_message = "任务运行协议错误"


class TaskRuntimeNotBuiltError(TaskProtocolError):
    """`get_task_runtime()` 时这个进程还没建过 runtime。

    **不懒建是故意的**：参考文档要求「每个进程只建一个 runtime，在应用启动入口建好
    再注入各业务服务，业务里不要随手 `build_task_runtime()`」—— 懒建就等于随手建，
    正是那句话要避免的。所以这里抛错，而不是悄悄补一个。
    """

    code = 1101
    default_message = (
        "任务运行时尚未创建：请在应用启动入口调用 build_task_runtime()，"
        "不要在业务代码里随手建"
    )


class ExecutionContextMissingError(TaskProtocolError):
    """不在 `acquire_execution()` 的作用域里却来取执行上下文。

    典型成因有两个：① 在 `RunnerBase.execute` 之外调 `report_success` / `report_failed`；
    ② 在 `with`（而不是 `async with`）里用 `acquire_execution` —— 不过后者会在更早的
    地方就 `TypeError`（那是类式异步上下文管理器，故意不留静默空间）。

    后台巡检那条路**本来就没有这一帧**，它该走 `TaskRuntime.report_terminal(carrier, ...)`
    显式传凭据，而不是来取上下文。
    """

    code = 1102
    default_message = "当前不在执行上下文里：请在 acquire_execution() 作用域内调用，或显式传凭据"


class UnknownStepCodeError(TaskProtocolError):
    """收到了没登记过的 `step_code`。

    **显式拒绝，不静默跳过**（参考文档 §7 第 2 条点名要求）。`RunnerBase` 自己不抛它
    ——基类的未知步骤走 `ApiResponse.fail` 失败壳（那样管理方拿到的仍是规范回执）——
    这个异常是给**没继承 `RunnerBase`、只用 `acquire_execution` 自己分流**的运行方用的。
    """

    code = 1103
    default_message = "不认识的步骤"

    def __init__(
        self,
        step_code: str,
        known: Optional[Iterable[str]] = None,
    ) -> None:
        known_codes = sorted(known or ())
        message = f"不认识的步骤 {step_code}（只认 {known_codes}）"
        super().__init__(message=message, details={"step_code": step_code, "known": known_codes})


__all__ = [
    "ExecutionContextMissingError",
    "TaskProtocolError",
    "TaskRuntimeNotBuiltError",
    "UnknownStepCodeError",
]
