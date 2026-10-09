#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""`TaskRuntime`：运行方拿到的 SDK 门面（§4.2 四个方向）+ 进程单例入口。

```python
# 应用启动入口（每个进程只建一次）
from jonex_core.task_runtime import build_task_runtime
build_task_runtime(service_name="<运行方标识>")

# 业务代码里（不要随手 build，取就行）
from jonex_core.task_runtime import get_task_runtime
runtime = get_task_runtime()
result = await runtime.submit(TaskSubmission(
    definition_id="...", space_id="...", inputs={...},
    task_id="运行方自己生成的 id",   # 带上它 → 失败会自动重试，且重发幂等
))
```

**`submit` 的重试只在自带 `task_id` 时启用**（详见 `submit` 的 docstring）：自带 id 使
重发幂等 —— 重发撞上自己上一次建的那条会回 `REUSED`（含义是「上一次其实已经建成了，
只是响应没回来」），当场收工，而不是建出第二条。

**上报（`report_terminal`）则是一直在重试、不带任何开关**：重复上报在服务端是安全的
（收件箱 `subtask_reports` 不加唯一约束，重复那条会被「已是终态」那道闸吸收掉），
重发不会造成任何副作用。两者共用同一套预算：3 次尝试 / 退避 0.5s、1s —— 这套数字
**定义在协议层**（`task_protocol.results`），因为管理方的派活重发（§4.1 ①）用的是
同一套，而它不能反向依赖本模块。

**重试某一步（`retry_subtask`）也重发，但理由跟前两者都不同**：不是"重发幂等所以安全"，
而是**管理方对这条路自己就是幂等的** —— 上一次其实重试成了、只是响应没回来时，重发撞上
的是「那一步已经在队列里了」，管理方据此回成功而不是报「它还没结束」。所以调用方拿到
`RETRIED` 就理解成「这一步会再跑一次」，**不必分辨是不是这一次调用促成的**（§4.2 ③）。

**重试某几步（`retry_subtasks`）与单步是同一档**（同一个 disposition 词汇表、同一套
重试预算、同一种幂等），唯一多出来的是**顺序**：`node_ids` 给的顺序就是执行顺序
（第 i 步等第 i-1 步成功），管理方把它落成依赖边。要一次重跑几步、而这几步在编排里
**不互为直接上下游**时（例如「解析 + 编译」中间还隔着「入库」），分别调两次单步重试
会让它们同时开跑 —— 那正是这个口子存在的唯一理由（§4.2 ④）。

**两条上报路径，别混**（这是本模块最要紧的一句话）：

| 场景 | 用哪个 | 租户 / 定位字段从哪来 |
| --- | --- | --- |
| handler 内部（在 `acquire_execution` 作用域里） | `RunnerBase.report_success` / `report_failed` | 从 `ExecutionContext`（ContextVar）自动取 |
| 巡检 / 后台循环（**没有那一帧**） | `runtime.report_terminal(carrier, terminal)` | **必须显式传 `carrier`** |

巡检那条路走 ContextVar **必然抛 `ExecutionContextMissingError`** —— 它不是从
`acquire_execution` 里长出来的，那一帧压根不存在。所以 `report_terminal` 的第一个参数
是必填的凭据，签名上就不留"忘了传还能跑"的口子。

**`build_task_runtime` 与 `get_task_runtime` 的分工是刻意的**：每个进程只建一个 runtime、
在启动入口建好再注入各业务服务，**业务代码里不要随手 `build_task_runtime()`**。所以
`get_task_runtime()` 没建过时**抛错、不做懒建** —— 懒建就等于随手建，正是那条要求要避免的。
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

from jonex_core.capability.response import ApiResponse
from jonex_core.common.tenant import TenantContext, require_tenant
from jonex_core.task_protocol.errors import TaskProtocolError, TaskRuntimeNotBuiltError
from jonex_core.task_protocol.models import (
    ExecutionDispatch,
    TaskSubmission,
    check_output_refs,
    OUTPUT_REFS_MAX_BYTES,
    OUTPUT_REFS_MAX_ITEMS,
    OUTPUT_REF_KINDS,
    OUTPUT_REF_PARAMS,
)
from jonex_core.task_protocol.results import (
    RETRY_BACKOFF_SEC,
    RETRY_MAX_ATTEMPTS,
    SubmissionDisposition,
    SubmissionResult,
    SubtaskRetryDisposition,
    SubtaskRetryResult,
    TaskRef,
    TerminalSubmitDisposition,
    TerminalSubmitResult,
    retry_delay,
    submission_disposition,
    subtask_retry_disposition,
    terminal_disposition,
)
from jonex_core.task_runtime.client import LegacyControlPlaneAdapter
from jonex_core.task_runtime.context import ExecutionContext
from jonex_core.task_runtime.ports import ControlPlaneClient

logger = logging.getLogger(__name__)

#: 运行方只报得了这两种（§4.2 ①）—— `running` 在报文层面就关掉了。
_TERMINAL_SUCCESS = "success"
_TERMINAL_FAILED = "failed"

# 重试预算**定义在协议层**（`jonex_core.task_protocol.results`）：管理方的派活重发
# （`capabilities/task_center/`）要用同一套数字，而它 import 本模块（运行方 SDK）方向
# 是反的。这里只留私有别名，让本模块内的用法一字不用改。
#
# ⚠️ **这几个名字必须继续挂在本模块上**：既有测试用
# `monkeypatch.setattr(runtime_module, "_RETRY_BACKOFF_SEC", (0, 0))` 把退避清零
# （`tests/unit/task_runtime/test_runtime_submit.py` 4 处、
# `test_runtime_runner_report.py` 3 处）。改名或删名 = 那 7 个用例全碎。
_RETRY_MAX_ATTEMPTS = RETRY_MAX_ATTEMPTS
_RETRY_BACKOFF_SEC = RETRY_BACKOFF_SEC


def _retry_delay(attempt: int) -> float:
    """第 `attempt` 次尝试失败后等多久。

    ⚠️ **不能直接 `_retry_delay = retry_delay`**：协议层那个函数读的是**它自己模块**的
    `RETRY_BACKOFF_SEC`，于是 `monkeypatch.setattr(runtime_module, "_RETRY_BACKOFF_SEC", ...)`
    这个 patch 面**是一个死名字** —— 看着改掉了退避，实际一秒没省，那 7 个用例全在真睡
    0.5s + 1s（曾经如此）。所以这里**必须自己读本模块的全局**。

    没被 patch 时（`_RETRY_BACKOFF_SEC` 还是协议层那一个对象）转发给协议层实现 ——
    **公式只有一份**，将来改退避算法不会两边分叉；被 patch 后就地取本模块的序列。
    """
    if _RETRY_BACKOFF_SEC is RETRY_BACKOFF_SEC:  # 没被 patch：走协议层那一份公式
        return retry_delay(attempt)
    return _RETRY_BACKOFF_SEC[min(attempt - 1, len(_RETRY_BACKOFF_SEC) - 1)]


def _now_ms() -> int:
    """13 位毫秒时间戳。管理方**只落库留痕**（`subtask_reports.ts` 一列）、不参与任何
    裁决（管理方不信运行方的钟）。"""
    return int(time.time() * 1000)


def _as_execution_context(carrier: Union[ExecutionContext, ExecutionDispatch]) -> ExecutionContext:
    """凭据既可以是 `ExecutionContext`，也可以是手头正好有的派活报文。

    ⚠️ **两者都不是就直接抛 `TaskProtocolError`**，不要放它进 `from_dispatch()` ——
    那样传 `None` / 传个裸 dict 得到的是 `AttributeError: 'NoneType' object has no
    attribute 'tenant_id'`：看不出是谁的错、也接不住（它绕过 SDK 的统一异常）。而
    「忘了传凭据」正是把 `carrier` 设成必填要防的那件事，报出来得是 SDK 自己的话。
    """
    if isinstance(carrier, ExecutionContext):
        return carrier
    if isinstance(carrier, ExecutionDispatch):
        return ExecutionContext.from_dispatch(carrier)
    raise TaskProtocolError(
        message=(
            "carrier 只能是 ExecutionContext 或 ExecutionDispatch，"
            f"收到 {type(carrier).__name__}"
        ),
        details={"carrier_type": type(carrier).__name__},
    )


#: `check_output_refs` 的 `reason_code` → 给运行方开发看的英文文案（SDK 不做 i18n）。
_OUTPUT_REF_REASON_TEXT: Dict[str, str] = {
    "not_array": "output_refs must be an array of objects (one dict per entry)",
    "too_large": (
        f"output_refs is too large (over {OUTPUT_REFS_MAX_BYTES} chars): "
        "it carries references only, never the artifact content"
    ),
    "too_many": f"output_refs has too many entries (max {OUTPUT_REFS_MAX_ITEMS})",
    "kind_missing": "every output_ref needs a 'kind'",
    "kind_invalid": f"'kind' must be one of {list(OUTPUT_REF_KINDS)}",
    "params_not_object": "'params' must be an object",
    "params_missing_key": (
        f"a required param is missing — see OUTPUT_REF_PARAMS for the params your "
        f"'kind' must carry ({OUTPUT_REF_PARAMS})"
    ),
    "params_bad_value": "'params' values must be non-empty scalars (str/int/float)",
}


def _check_output_refs(refs: Optional[List[Dict[str, Any]]]) -> None:
    """本地上报前先卡一道 `output_refs`（§4.2 ①）。

    **判据只有一份** —— 协议层的 `check_output_refs`（`jonex_core/task_protocol/models.py`），
    服务端 `_check_report_payload` 调的是**同一个函数**。这里只做两件事：出站前先炸
    （别让调用方白等两轮退避），以及把 `reason_code` 翻成一句能看懂的英文。

    规则见协议层那段 docstring：`kind` 写死枚举、`params` 按 `OUTPUT_REF_PARAMS` 里该 kind
    声明的键给。**引用里不带地址** —— 「该调哪个接口」是前端的映射，运行方碰不到，
    也就填不出外站地址 / `javascript:` / 目录穿越那一类东西。
    """
    problem = check_output_refs(refs)
    if problem is None:
        return
    reason, details = problem
    raise TaskProtocolError(
        message=f"{_OUTPUT_REF_REASON_TEXT.get(reason, 'invalid output_ref')} "
        f"(reason={reason}, details={details})",
        details={"reason": reason, **details},
    )


@dataclass(frozen=True)
class TerminalReport:
    """要上报的终态。用两个工厂造，别直接构造。

    §4.2 ① 对报文本身有硬要求：`status=success` 时**必带** `outputs`、`status=failed` 时
    **必带** `error_message`。缺了会被任务中心当场拒（`InvalidParameterError`，属
    **明确拒绝**那一档 —— 原样重发结果一样）。所以这两个约束在**本地**就用 `__post_init__`
    挡住：与其发出去换一个注定被拒的回包（还要白等两轮退避），不如在出站前就炸。

    这是**调用方自己写错了**，不是任务中心的裁决 —— 所以抛 `TaskProtocolError`，
    不伪装成 `TerminalSubmitDisposition.PERMANENT_REJECTED`（那会被当成业务结论写进死信）。

    ── 本地这道闸与服务端**逐条对齐**，只有两处故意更严 ──

    服务端判据在 `task_service.py` 的 `_check_report_payload`：`not isinstance(outputs,
    dict)` / `not error_message` / 非法 `status`。这里**逐条同口径** —— `outputs={}` 是
    **合法**的（内置的 `compile` 步 `output_schema` 就是 `{}`：空 = 真的没产出，见 §3.4），
    早先写成 `if not self.outputs:` 会把「无产出步骤报成功」这条唯一的正常路径堵死。

    故意更严的两条，别当成不一致：

    | 这里 | 服务端 | 为什么 |
    | --- | --- | --- |
    | `error_message` 拒纯空白 | `"   "` 照收 | 空白等于没写原因，而它是人看失败原因的唯一来源；当场炸比落库一条空白强 |
    | `status` 只认小写字面量 | 先 `.lower()` | 报文只有这一种正确写法（两个工厂）；多认几种大小写只会让「到底哪个对」变模糊 |
    """

    status: str
    outputs: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    ts: Optional[int] = None
    # 产出「去哪儿看」的引用（展示用，**不参与下游取参**）：一个数组，元素只有 `kind` + `params`
    # （形状是**定死的**：`kind` 带运行方前缀、∈ 协议层 `OUTPUT_REF_PARAMS`；`params` 带那个接口
    # 要的字段）。**不带地址** —— 「调哪个接口」是前端的映射，运行方碰不到，所以填不出外站地址 /
    # `javascript:` / 目录穿越 / `DELETE` 那一类东西。管理方只搬运、未知键放行。
    # 可选 —— 不传就是空（前端不显示「查看产出」入口）。
    output_refs: Optional[List[Dict[str, Any]]] = None

    def __post_init__(self) -> None:
        _check_output_refs(self.output_refs)
        if self.status == _TERMINAL_SUCCESS:
            # ⚠️ **判据与服务端逐字一致：`not isinstance(outputs, dict)`**，不是 `is None`。
            # 两处含义不同，别改回去：
            #   · `{}` **是合法产出**（空 = 真的没产出）—— 无产出步骤（内置的 `compile`）
            #     报成功时传的就是它，所以**不能**写成 `not self.outputs`；
            #   · 但 `["a"]` / `"x"` / `0` 这类**非 dict** 一律非法 —— 服务端
            #     （`task_service._check_report_payload`）判的就是 `isinstance`，本地从前
            #     只看 `is None`、把这一档漏过去了：handler 传个列表会被**本地放行**，
            #     到服务端才回 1001，客户端白等 1.5 秒重发 3 次才把结论交回调用方。
            if not isinstance(self.outputs, dict):
                raise TaskProtocolError(
                    message="上报成功必须带 outputs，且必须是个对象（dict）（§4.2 ①）",
                    details={"status": self.status, "outputs_type": type(self.outputs).__name__},
                )
        elif self.status == _TERMINAL_FAILED:
            if not self.error_message or not str(self.error_message).strip():
                raise TaskProtocolError(
                    message="上报失败必须带 error_message（§4.2 ①）",
                    details={"status": self.status},
                )
        else:
            # `running` 已经不是合法取值（§4.2 ①）：运行方拿到凭据时子任务必然已经在跑，
            # 再报一句 running 什么也改变不了。pending / timeout 由管理方自己置。
            raise TaskProtocolError(
                message=f"运行方只能上报 success / failed，收到 {self.status!r}（§4.2 ①）",
                details={"status": self.status},
            )

    @classmethod
    def success(
        cls,
        outputs: Dict[str, Any],
        ts: Optional[int] = None,
        output_refs: Optional[List[Dict[str, Any]]] = None,
    ) -> "TerminalReport":
        """跑完了。`outputs` 必填；`output_refs` 可选（产出「去哪儿看」，展示用）。"""
        return cls(
            status=_TERMINAL_SUCCESS, outputs=outputs, ts=ts, output_refs=output_refs
        )

    @classmethod
    def failed(
        cls,
        error_message: str,
        ts: Optional[int] = None,
        output_refs: Optional[List[Dict[str, Any]]] = None,
    ) -> "TerminalReport":
        """没跑成。`error_message` 必填 —— 它是人看失败原因的唯一来源。

        `output_refs` 可选：失败时也**可能**有东西可看（半成品、部分切片、错误详情页），
        由运行方自己决定给不给。
        """
        return cls(
            status=_TERMINAL_FAILED,
            error_message=error_message,
            ts=ts,
            output_refs=output_refs,
        )


class TaskRuntime:
    """SDK 门面。**不要自己 new** —— 经 `build_task_runtime()` / `get_task_runtime()`。
    """

    def __init__(
        self,
        *,
        service_name: str,
        control_plane: Optional[ControlPlaneClient] = None,
    ) -> None:
        if not service_name or not service_name.strip():
            raise TaskProtocolError(message="service_name 不能为空：它标识这个进程是哪个运行方")
        self._service_name = service_name.strip()
        # 不传就走 §4.4 的 `get_task_center_client()`（能力清单决定 LOCAL / REMOTE / MOCK）。
        # 包一层适配器：把「方向不对」的 `execute` / `remove_task` 挡在 SDK 视野之外。
        self._control_plane: ControlPlaneClient = (
            control_plane if control_plane is not None else LegacyControlPlaneAdapter()
        )

    @property
    def service_name(self) -> str:
        return self._service_name

    @property
    def control_plane(self) -> ControlPlaneClient:
        return self._control_plane

    # ── §4.2 ②：提交 ──

    async def submit(
        self,
        submission: TaskSubmission,
        *,
        tenant_id: Optional[str] = None,
    ) -> SubmissionResult:
        """起一条新任务（§4.2 ②）。**自带 `task_id` 时会在本方法内自动重试。**

        **租户**：`tenant_id` 显式传优先，没传取 `TenantContext`。**后台投递器那条路
        必须显式传** —— 它不是请求路径，那一帧里没有上下文。

        收不出合法租户时**抛 `TenantIsolationError`，不回 `REJECTED`**：这是调用方
        装配错了，不是任务中心的裁决。伪装成 `REJECTED` 会让人把「没租户」当成
        「服务端拒绝了这次提交」写进死信，真实的装配 bug 就此消失。

        ── 重试：**只有自带 `task_id` 才重试** ──

        | 这次提交 | 失败后重试吗 | 为什么 |
        | --- | --- | --- |
        | 带了 `task_id` | **重试**（首次 + 2 次，退避 0.5s / 1s） | 自带 id 使重发**幂等**：重发最多撞上自己上一次建的那条（回 `REUSED`），绝不会多建一条 |
        | 没带 `task_id` | **一次都不重试** | 服务端 `init_task` 不做幂等去重，重发就是**新建第二条任务** —— 一次网络抖动换一条重复任务 |

        **重试范围 = 除「成功」与「`REUSED`」之外的全部**：`RETRYABLE_UNKNOWN`、
        `REJECTED`（参数错误这类）都会重试。代价是参数错误也要等满退避才返回；换来的是
        不会因为服务端某一刻回了个奇怪的码，就放弃一次本可成功的提交。

        ── `REUSED`：**当成成功收工** ──

        它表示**上一次调用其实已经建成了，只是响应没回来** —— 这次重发撞上的正是自己
        上次建的那一条（`task_id` 是发起方自己生成的，所以「id 被占」只可能是这个成因）。
        因此它**当场返回、不再往下试**（再试只会一遍遍撞同一个 id），调用方也**不要换
        id 重来**（换 id 就是把同一条任务建第二遍，正是重试本要防的事）。

        成功时返回的 `task` 是**最小标识** `TaskRef`（只有 id + `definition_version_no`），
        不是完整快照 —— 理由见 `TaskRef` 的说明。
        """
        resolved = _resolve_tenant(tenant_id)
        # 只有自带 id 才重试：不带 id 时每次重发都可能建出一条新任务（服务端不做幂等
        # 去重），重试等于凭空多建；带了 id 则重发安全 —— 撞上就是 REUSED。
        max_attempts = _RETRY_MAX_ATTEMPTS if submission.task_id else 1
        last: Optional[SubmissionResult] = None

        for attempt in range(1, max_attempts + 1):
            response = await self._control_plane.init_task(
                resolved,
                submission.definition_id,
                submission.space_id,
                submission.inputs,
                version_no=submission.version_no,
                name=submission.name,
                operator_id=submission.operator_id,
                # 发起方自己生成的 id（不传就是 `None`，管理方自己生成）。透传，不在这里校验 ——
                # 「这个 id 能不能用」只有管理方能判（它的库、它的主键），撞了会回 1006。
                task_id=submission.task_id,
            )
            disposition = submission_disposition(response.success, response.code)

            if disposition is SubmissionDisposition.CREATED:
                # `ApiResponse.from_dict` **不做子模型校验**：`data` 是原样塞进去的裸 dict。
                # 所以这一步必须自己解析 —— 不解析的话调用方拿到的是 dict，而类型标注说它是
                # `TaskRef`，谁都不会报错，直到有人去取 `.id`。
                return SubmissionResult(
                    disposition=disposition,
                    task=TaskRef.from_payload(response.data),
                    message=response.message,
                )

            if disposition is SubmissionDisposition.REUSED:
                # 上一次其实建成了、只是响应没回来 —— 这正是重试要落到的终点。当场收工：
                # 再试只会一遍遍撞同一个 id，而且这一档服务端不回 data，没有可解析的东西。
                return SubmissionResult(
                    disposition=disposition, message=response.message
                )

            last = SubmissionResult(disposition=disposition, message=response.message)
            if attempt < max_attempts:
                delay = _retry_delay(attempt)
                logger.warning(
                    "任务提交未成功（第 %d/%d 次，code=%s），%.1fs 后重试：%s",
                    attempt,
                    max_attempts,
                    response.code,
                    delay,
                    response.message,
                )
                await asyncio.sleep(delay)

        # max_attempts >= 1，循环至少跑一次，`last` 必然已被赋值
        assert last is not None
        return last

    # ── §4.2 ①：上报终态（巡检 / 后台循环那条路） ──

    async def report_terminal(
        self,
        carrier: Union[ExecutionContext, ExecutionDispatch],
        terminal: TerminalReport,
    ) -> TerminalSubmitResult:
        """上报终态。**给没有执行上下文的调用方用**（巡检 / 后台循环）。

        handler 内部请用 `RunnerBase.report_success` / `report_failed` —— 那两个从
        ContextVar 取定位字段，不用手传。这里反过来：凭据必须显式传。

        无论走哪条路，最终都是**同一个 `report_status` 调用**（参考文档那条「最终都要走
        同一个 `report_terminal()`」），不存在两条上报实现。

        **返回 `DURABLY_ACCEPTED` 只代表「已受理」，不代表「已经落库」**：任务中心先把
        上报存进收件箱，真正的裁决由它的后台调度循环完成（§7.5 ②）。所以收到
        `DURABLY_ACCEPTED` 之后立刻查任务状态，可能还看不到变化。

        ── 重试：**不需要任何前提，一直都在做** ──

        重复上报在服务端是**安全的**：收件箱 `subtask_reports` **不加任何唯一约束**，
        同一张凭证重复到达会被「已是终态」那道闸吸收掉（§4.2 ①）。所以这里可以放心
        重发 —— 跟 `submit` 不一样，**上报没有「不带 id 就会多建一条」的风险**，
        也就没有开关。

        范围 = **除 `DURABLY_ACCEPTED` 之外的全部**：`RETRYABLE_UNKNOWN` 与
        `PERMANENT_REJECTED` 都会重试。预算与 `submit` 共用（3 次 / 退避 0.5s、1s）。

        **本地挡掉的非法报文不会进这个循环**：`TerminalReport.__post_init__` 把
        「success 缺 `outputs` 或 `outputs` 不是 dict / failed 缺 `error_message` /
        `status` 非法」在出站前就拦了（判据与服务端逐字同口径，2026-09-30 对齐 ——
        原先本地只看 `is None`、漏了「非 dict」那一档）。能走到重试的
        `PERMANENT_REJECTED`，是服务端另判出来的东西（比如租户 / 空间对不上）。
        """
        context = _as_execution_context(carrier)
        # 同一次上报的几次尝试共用一个事件时间：重发的是**同一个事件**，`ts` 不该跟着
        # 重发时刻漂移（它要落 `subtask_reports.ts`）。
        ts = terminal.ts if terminal.ts is not None else _now_ms()
        last: Optional[TerminalSubmitResult] = None

        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            response = await self._control_plane.report_status(
                context.tenant_id,
                context.space_id,
                context.execution_id,
                # `task_id` 从上下文里取（**不是**从 `carrier` 现摘）：管理方拿它跟
                # `space_id` 一起做交叉校验，运行方业务代码一行都不用动。
                context.task_id,
                terminal.status,
                terminal.outputs,
                terminal.error_message,
                ts,
                terminal.output_refs,
            )
            disposition = terminal_disposition(response.success, response.code)

            if disposition is TerminalSubmitDisposition.DURABLY_ACCEPTED:
                return TerminalSubmitResult(
                    disposition=disposition, message=response.message
                )

            last = TerminalSubmitResult(
                disposition=disposition, message=response.message
            )
            if attempt < _RETRY_MAX_ATTEMPTS:
                delay = _retry_delay(attempt)
                logger.warning(
                    "终态上报未被受理（第 %d/%d 次，code=%s），%.1fs 后重发：%s",
                    attempt,
                    _RETRY_MAX_ATTEMPTS,
                    response.code,
                    delay,
                    response.message,
                )
                await asyncio.sleep(delay)

        # _RETRY_MAX_ATTEMPTS >= 1，循环至少跑一次，`last` 必然已被赋值
        assert last is not None
        return last

    # ── §4.2 ③：重试某一步 ──

    async def retry_subtask(
        self,
        tenant_id: str,
        space_id: str,
        task_id: str,
        node_id: str,
        *,
        operator_id: Optional[str] = None,
    ) -> SubtaskRetryResult:
        """要求重跑某一步（§4.2 ③）。**没被受理会在本方法内自动重发。**

        **按 `node_id` 寻址**：同一个 `step_code` 可以在编排里出现多次（`parse_1` /
        `parse_2` 都是 `parse`），能唯一指认一步的只有 `node_id`。它是**不透明标识** ——
        原样传回去就行，别解析它的形状。

        **形参顺序与端口层（`ControlPlaneClient.retry_subtask`）、与设计文档 §4.2 ③
        完全一致**：`tenant_id` / `space_id` / `task_id` / `node_id` 四个位置参数，
        `operator_id` 关键字。这不是风格问题 —— 四个都是短 id 串，靠位置认人最容易传反，
        三处顺序只要不一致就是接入方第一次调用必踩的坑。

        **租户**：`tenant_id` 显式传优先，没传取 `TenantContext`（跟 `submit` 走的是
        同一个 `_resolve_tenant`，三层 client 也是同一套规则）。**不带请求上下文的调用
        （后台投递 / 定时那类）必须显式传** —— 那一帧里没有上下文，「没传」就等于收不出
        租户。收不出合法租户时**抛 `TenantIsolationError`、不回 `REJECTED`** —— 那是
        装配错了，不是任务中心的裁决（伪装成 `REJECTED` 会让人把装配 bug 当成业务结论）。

        **`operator_id`**：是谁点的这次重试，管理方落 `tasks.updated_by`，**纯记账**
        （不校验、不参与任何判定）。拿不到就传 `None` —— 空值是合法状态，不是漏记。

        ⚠️ **它没有默认值之外的保护**：三处签名（端口 `ControlPlaneClient` / 本门面 /
        `client.py` 的 `_TaskCenterActions`）都写作 `operator_id: Optional[str] = None`，
        **漏传不报错**，只会静默记成空值。契约要求它显式出现，**那是接入方的义务，不是
        运行时拦得下来的事**。所以：别指望「不传会炸」，也**别把它改成必填** —— 那会在
        不改契约的前提下破坏 SDK 的向后兼容。没有人的场景就明写 `operator_id=None`。

        ── 重试：**一直在做，且这里敢做是因为服务端幂等** ──

        范围 = **除 `RETRIED` 之外的全部**：`RETRYABLE_UNKNOWN`（超时 / 连不上 / 抢不到
        任务锁）与 `PERMANENT_REJECTED`（任务还没跑完、上游没成功这类）**都会重发** ——
        判据是 `code` 落在哪张表，不是"重发有没有意义"（同 `report_terminal`）。预算与
        另外四个方向共用：3 次尝试 / 退避 0.5s、1s。

        敢重发的前提是**管理方对这条路幂等**：上一次其实重试成了、只是响应没回来时，
        重发撞上的是「那一步已经在 `pending` / `running` 了」，管理方据此**回成功**而不是
        报「那一步还没结束」。没有这条，一次网络抖动就会让调用方看到"重试失败"、
        而实际上重试已经生效 —— 那是最坏的一种误报。

        ⚠️ **但服务端那道幂等档只覆盖「还在队列里」这一段窗口**：响应丢掉之后那一步若
        **已经重跑完并回到终态**、而任务本身还没终态（同一个任务里别的步骤还在跑），重发
        会撞「任务不是终态」那道前提、回 `1003` → 这里判成 `PERMANENT_REJECTED`。
        **所以拿到 `PERMANENT_REJECTED` 不等于「这一步没被重跑」** —— 要真相就按
        `task_id` 去查那一步的状态。别为了消掉这个误报去放宽服务端的判据：那会把一次
        **真正的新重试请求**吞掉（§4.2 ③）。
        """
        resolved = _resolve_tenant(tenant_id)
        last: Optional[SubtaskRetryResult] = None

        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            response = await self._control_plane.retry_subtask(
                resolved,
                space_id,
                task_id,
                node_id,
                operator_id=operator_id,
            )
            disposition = subtask_retry_disposition(response.success, response.code)

            if disposition is SubtaskRetryDisposition.RETRIED:
                return SubtaskRetryResult(
                    disposition=disposition, message=response.message
                )

            last = SubtaskRetryResult(
                disposition=disposition, message=response.message
            )
            if attempt < _RETRY_MAX_ATTEMPTS:
                delay = _retry_delay(attempt)
                logger.warning(
                    "子任务重试未被受理（第 %d/%d 次，code=%s），%.1fs 后重发：%s",
                    attempt,
                    _RETRY_MAX_ATTEMPTS,
                    response.code,
                    delay,
                    response.message,
                )
                await asyncio.sleep(delay)

        # _RETRY_MAX_ATTEMPTS >= 1，循环至少跑一次，`last` 必然已被赋值
        assert last is not None
        return last

    # ── §4.2 ④：重试某几步（按顺序）──

    async def retry_subtasks(
        self,
        tenant_id: str,
        space_id: str,
        task_id: str,
        node_ids: List[str],
        *,
        operator_id: Optional[str] = None,
    ) -> SubtaskRetryResult:
        """要求重跑某几步（§4.2 ④）。**没被受理会在本方法内自动重发。**

        **`node_ids` 的顺序就是执行顺序**：第 i 个节点要等第 i-1 个成功之后才被派活。
        这是它跟「连着调两次 `retry_subtask`」的全部区别 —— 那两次会把两步**同时**放回
        队列（编排里的依赖是直连的，要重跑的两步多半不互为直接上下游），一起跑起来。
        名字里那个「s」承载的就是这件事，**顺序不承载任何其他含义**（不是优先级、不是
        排序提示，就是先后）。

        **形参顺序与端口层（`ControlPlaneClient.retry_subtasks`）、与设计文档 §4.2 ④
        完全一致**：`tenant_id` / `space_id` / `task_id` / `node_ids` 四个位置参数，
        `operator_id` 关键字。理由与单步那处逐字相同：都是短 id 串，靠位置认人最容易
        传反，三处顺序不一致就是接入方第一次调用必踩的坑。

        **租户**：与 `retry_subtask` 同一个 `_resolve_tenant` —— 显式传优先、没传取
        `TenantContext`；不带请求上下文的调用必须显式传。收不出合法租户时**抛
        `TenantIsolationError`、不回 `REJECTED`**（那是装配错了，不是任务中心的裁决）。

        **`operator_id`**：是谁点的这次重跑，管理方落 `tasks.updated_by`，**纯记账**。
        拿不到就传 `None` —— 空值是合法状态，不是漏记。与单步那处同一条 ⚠️：三处签名
        都写作 `Optional[str] = None`，**漏传不报错**，只会静默记成空值。

        **返回值沿用 `SubtaskRetryResult`（同一个 disposition 词汇表）**：这一批**不引入
        任何新的失败形态** —— 能拒的理由还是那几种（任务没跑完 / 名单里某一步没结束 /
        批外上游没成功 / 参数坏），`(success, code)` 的三种落点与单步一一对应。再立一套
        并行的枚举就是把同一套判据抄三份、早晚分叉。

        ── 重试：**一直在做，且这里敢做是因为服务端幂等** ──

        范围、预算、退避与单步那处**逐字相同**（除 `RETRIED` 外全重发；3 次尝试；
        0.5s、1s）。敢重发的理由也一样，只是幂等档换成了批量版：上一次其实生效了、
        只是响应没回来时，**整批**都已经排上队，管理方据此回成功。

        ⚠️ 同样地：**拿到 `PERMANENT_REJECTED` 不等于「这几步没被重跑」** —— 响应丢掉
        之后那一批若已经跑完、而任务还没终态，重发会撞「任务不是终态」那道前提、回
        `1003` → 这里判成 `PERMANENT_REJECTED`。要真相就按 `task_id` 去查那几步的状态。

        ⚠️ **名单本身坏了也是 `PERMANENT_REJECTED`，而且照样重发 3 次**（空名单 / 有
        重复 / 顺序与依赖方向拧着来 → 1001）：判据是 `code` 落在哪张表，不是"重发有没有
        意义"（同 `report_terminal`）。所以调用方**别指望这里替它拦参数** —— 名单是它自己
        拼的，一次拼错就要多等两个退避。代价有界，且比"把一次瞬时失败判成终局"划算。
        """
        resolved = _resolve_tenant(tenant_id)
        last: Optional[SubtaskRetryResult] = None

        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            response = await self._control_plane.retry_subtasks(
                resolved,
                space_id,
                task_id,
                node_ids,
                operator_id=operator_id,
            )
            disposition = subtask_retry_disposition(response.success, response.code)

            if disposition is SubtaskRetryDisposition.RETRIED:
                return SubtaskRetryResult(
                    disposition=disposition, message=response.message
                )

            last = SubtaskRetryResult(
                disposition=disposition, message=response.message
            )
            if attempt < _RETRY_MAX_ATTEMPTS:
                delay = _retry_delay(attempt)
                logger.warning(
                    "子任务批量重试未被受理（第 %d/%d 次，code=%s），%.1fs 后重发：%s",
                    attempt,
                    _RETRY_MAX_ATTEMPTS,
                    response.code,
                    delay,
                    response.message,
                )
                await asyncio.sleep(delay)

        # _RETRY_MAX_ATTEMPTS >= 1，循环至少跑一次，`last` 必然已被赋值
        assert last is not None
        return last

    # ── 生命周期 ──

    # ── §4.3 读：某一步现在的状态（2026-09-30 开给运行方）──

    async def get_subtask(
        self,
        tenant_id: str,
        task_id: str,
        node_id: str,
        *,
        attempt: Optional[int] = None,
    ) -> ApiResponse[Dict[str, Any]]:
        """**读**某一步现在的状态（§4.3 `task.subtask_get`）。**纯只读。**

        运行方从前**没有任何通道**能读管理方的状态，只能靠自己的超时硬等 —— 典型问题是
        「我这一步是不是已经被管理方判死了」问不出来。典型用法：**放弃之前先问一句**
        （看门狗准备按自己的超时交还时先读一次；看到 `success` / `failed` / `timeout`
        就说明那边已经定局、不用再等）。

        ⚠️ **这里刻意不重试**（与四个写方向都不同）：它是个**查询**，读不到就是读不到 ——
        调用方（看门狗）本来就在自己的循环里，下一轮再读即可。在 SDK 里再叠一层重发，
        只会把「读一次」变成「卡 1.5 秒」。

        ⚠️ **租户必须显式传**（与写方向不同，这里**不回落** `TenantContext`）：读的调用点
        多在后台看门狗那种没有请求上下文的地方，回落只会拿到空值、报一个与真因无关的错。

        `data` 是**裸 dict**（形状同 `SubtaskItemResponse`），投影由调用方自己做 ——
        协议层 / 本层不得 import 能力层的 DTO（有测试钉住那道门禁），见端口层那条说明。
        """
        return await self._control_plane.get_subtask(
            tenant_id, task_id, node_id, attempt=attempt
        )

    # ── §4.3 读：整条任务现在的状态（2026-09-30 开给运行方）──

    async def get_task(
        self, tenant_id: str, task_id: str
    ) -> ApiResponse[Dict[str, Any]]:
        """**读**整条任务现在的状态（§4.3 `task.get`）。**纯只读。**

        与 `get_subtask`（读某一步）是一对，**但两者不能互相替代** —— 要判「这次编排
        是不是已经结束了」，**光看某一步是不够的**：

        ⚠️ **典型反例（这正是它的用途）**：用户把任务**停了**。`task.stop` 只判死
        **还没开始**的那些步骤，**正在跑的那一步原地不动**（运行方没有停止能力，杀了也
        白杀）—— 所以那一刻**那一步仍是 `running`**，而**这条任务**已经是 `failed` 了。
        只读步骤就永远看不出「停了」，只能死等自己的硬超时（最长 6 小时）。

        ⚠️ **这里刻意不重试**（与四个写方向都不同）：它是个**查询**，读不到就是读不到 ——
        调用方（看门狗）本来就在自己的循环里，下一轮再读即可。

        ⚠️ **租户必须显式传**（不回落 `TenantContext`）：读的调用点多在后台看门狗那种
        没有请求上下文的地方，回落只会拿到空值、报一个与真因无关的错。

        `data` 是**裸 dict**（形状同 `TaskItemResponse`），投影由调用方自己做 ——
        协议层 / 本层不得 import 能力层的 DTO。
        """
        return await self._control_plane.get_task(tenant_id, task_id)

    async def aclose(self) -> None:
        """释放控制面连接池。**探测式调用** —— 端口上故意不定义 `aclose`（见 `client.py`）。

        应用关停时调一次。之后不要再用这个实例。
        """
        aclose = getattr(self._control_plane, "aclose", None)
        if aclose is not None:
            await aclose()


def _resolve_tenant(tenant_id: Optional[str]) -> str:
    """显式入参优先，否则回落 `TenantContext`；都收不出来就抛 `TenantIsolationError`。

    这条回落链跟 §4.4 client 内部的 `_resolve_tenant` 是**同一套规则**，但这里要抛
    而不是回失败壳 —— 见 `submit` 的说明。
    """
    return require_tenant(tenant_id or TenantContext.get())


# ============================================================
# 进程单例
# ============================================================

_runtime: Optional[TaskRuntime] = None


def build_task_runtime(
    *,
    service_name: str,
    control_plane_client: Optional[ControlPlaneClient] = None,
) -> TaskRuntime:
    """造 runtime 并注册成进程单例 —— **在应用启动入口调，别在业务代码里调**。

    重复调用会覆盖上一个（后建的赢），不会报错：应用重启 / 测试里重建是正常用法。
    """
    global _runtime
    _runtime = TaskRuntime(service_name=service_name, control_plane=control_plane_client)
    logger.info(
        "任务运行时已创建：service_name=%s control_plane=%s",
        _runtime.service_name,
        type(_runtime.control_plane).__name__,
    )
    return _runtime


def get_task_runtime() -> TaskRuntime:
    """取进程单例；**没建过就抛 `TaskRuntimeNotBuiltError`，不做懒建**。

    懒建等于随手建（见模块 docstring），所以宁可在这里炸得清清楚楚。
    """
    if _runtime is None:
        raise TaskRuntimeNotBuiltError()
    return _runtime


__all__ = [
    "TaskRuntime",
    "TerminalReport",
    "build_task_runtime",
    "get_task_runtime",
]
