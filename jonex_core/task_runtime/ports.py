#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""SDK 的两个端口（§4.1、§4.2）。

**端口放这一层、不放 `task_protocol`，是本轮的显式决定**：端口要返回
`ApiResponse`，而 `ApiResponse` 住在 `jonex_core.capability.response` —— 协议层明令
不得 import `jonex_core.capability.*`（那个包的 `__init__.py` re-export 了能力注册中心，
一次 import 就把注册中心拉进协议层）。所以「需要 `ApiResponse` 的东西」一律下沉到这里。

`ControlPlaneClient` 主要是**单向**的：本期实现运行方 → 管理方的四条路（提交 / 上报 /
重试某一步 / 重试某几步），**外加两条读**（`get_subtask` / `get_task`，2026-09-30 加 ——
运行方要能问出「管理方那边定局没有」，否则只能死等自己的硬超时）。
**端口定义必须容得下后续的 command 通道**（暂停 / 取消 / 心跳 / 进度）——
条款出处：设计文档 [`2026-09-09-task-center-design.md` 附录 C.2](../../docs/superpowers/specs/2026-09-09-task-center-design.md) 第 3 节（原 `task-center-phase0-plan.md`，2026-09-29 并入该附录、原文件已删）：
「控制指令走独立通道，不混进执行凭据」。加新的控制方法时**只做加法**，不要改已有方法的签名。

⚠️ **本轮就是对那条「不改签名」的一次显式破例**（一次，不是常态）：`report_status`
加了一个必填的 `task_id`（交叉校验用，见 `TaskCenterClient.report_status` 的说明）。
它之所以能改：调用方只有运行方 SDK 自己（`TaskRuntime.report_terminal` 从执行上下文里
带出来，**运行方业务代码一行都不用动**），而任务中心还没上线、没有存量接入方要兼容。
后续再加控制方法（暂停 / 取消 / 心跳）**仍然照原样只做加法** —— 破例的理由不会一直成立。

**`retry_subtasks`（§4.2 ④）就是照那条规矩加的**：新方法、新参数，已有方法的签名一个
字没动，所以不需要再走一次破例。它存在的理由见那个方法的 docstring（要一次重跑几步、
而且**按顺序**，这是单步口子表达不了的）。
"""

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from jonex_core.capability.response import ApiResponse
from jonex_core.task_protocol.models import ExecutionDispatch


@runtime_checkable
class ControlPlaneClient(Protocol):
    """SDK 眼里的「任务中心」。

    运行方代码依赖这个端口、不依赖具体的 client 实现；测试可以注入假实现，
    不必连任务中心进程。签名与 `TaskCenterClient`（§4.4）的同名方法一致 ——
    `client.py` 里的适配器就是这么包过来的。
    """

    async def init_task(
        self,
        tenant_id: str,
        definition_id: str,
        space_id: str,
        inputs: Optional[Dict[str, Any]] = None,
        *,
        version_no: Optional[int] = None,
        name: Optional[str] = None,
        operator_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> ApiResponse[Dict[str, Any]]:
        """起一条新任务（§4.2 ②）。

        `task_id` 是**可选**的「发起方自己生成的 id」：给了就用它，不给由管理方生成。
        发起方要先把 id 落到自己的行上、又不想等管理方回包时才用得上。

        撞了已有任务回 `ResourceAlreadyExistsError`（1006）—— 对自带 id 的调用方那是
        「**复用既有任务**」（上一次其实建成了、只是响应没回来），不是拒绝。
        见 `SubmissionDisposition.REUSED`。

        ⚠️ **`data` 是裸 dict，不是 `TaskItemResponse` 对象**（返回标注按实情写）：
        `ApiResponse.from_dict` **不转子模型**（原样把 JSON 塞进 `data`），而
        `TaskRuntime.submit` 紧接着拿它去 `TaskRef.from_payload(...)` —— 那个函数只吃
        dict（`.get`）。**形状**是 `TaskItemResponse`（§八），SDK 只从中取
        `id` + `definition_version_no` 两项投影成 `TaskRef`。
        所以自己实现这个端口时**回 dict**：回一个真模型对象会让 `from_payload` 抛
        `AttributeError`（而且不是它 docstring 里说的那种受控的 `TaskProtocolError`）。
        """
        ...

    async def report_status(
        self,
        tenant_id: str,
        space_id: str,
        execution_id: str,
        task_id: str,
        status: str,
        outputs: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
        ts: Optional[int] = None,
        output_refs: Optional[List[Dict[str, Any]]] = None,
    ) -> ApiResponse[None]:
        """上报终态（§4.2 ①）。

        ⚠️ **参数顺序是承重的**（`ts` 在 `output_refs` 前面）：唯一调用点
        `TaskRuntime.report_terminal` 是**按位置**传的（`runtime.py`），而协议只查属性名、
        从不查签名 —— 顺序写反了不会有任何东西报错，只会把 `ts`(int) 塞进 `output_refs`、
        把 `output_refs`(list) 塞进 `ts`。`LegacyControlPlaneAdapter.report_status`
        （`task_runtime/client.py`）与真实现 `TaskCenterClient.report_status`
        （`capability/business/task_center/client.py`）都是这个顺序，改这里要三处一起改。

        **没被受理就重发**，且**不需要任何前提**：重复上报在服务端是安全的（收件箱不加
        唯一约束，重复那条会被「已是终态」那道闸吸收掉）。这件事由 `TaskRuntime.report_terminal`
        做掉，实现这个端口的人不用自己写重试循环。

        `task_id` **不参与定位**（定位仍是 `tenant_id` + `space_id` + `execution_id`），
        它是**交叉校验**：管理方在同步阶段核对「报文的 `task_id` / `space_id` 确实互相对
        得上」，对不上就当场拒（见 `TaskCenterClient.report_status`）。`TaskRuntime`
        从执行上下文里带出来，调用方不用自己拼。
        """
        ...

    async def retry_subtask(
        self,
        tenant_id: str,
        space_id: str,
        task_id: str,
        node_id: str,
        *,
        operator_id: Optional[str] = None,
    ) -> ApiResponse[None]:
        """要求重跑某一步（§4.2 ③）。`data` 为空 —— 成败看 `success` / `code`。

        **按 `node_id` 寻址，不按 `step_code`**：编排里同一个 `step_code` 可以出现多次
        （`parse_1` / `parse_2` 都是 `parse`），能唯一指认一步的只有 `node_id`。

        **成不成有前提**（都是明确拒绝，不是可重试）：任务得已经跑完、那一步也得已经结束、
        它的上游得已经成功。所以「重试」不是"随便再跑一次"，是**在一条已经收口的任务上
        单开一步**。

        **没被受理就重发**：管理方对这条路是幂等的（上一次其实重试成了、只是响应没回来
        → 一样回成功），这件事由 `TaskRuntime.retry_subtask` 做掉。
        """
        ...

    async def retry_subtasks(
        self,
        tenant_id: str,
        space_id: str,
        task_id: str,
        node_ids: List[str],
        *,
        operator_id: Optional[str] = None,
    ) -> ApiResponse[None]:
        """要求重跑某几步（§4.2 ④）。`data` 为空 —— 成败看 `success` / `code`。

        **`node_ids` 的顺序就是执行顺序**：第 i 个节点要等第 i-1 个成功之后才被派活。
        这是这个口子和「连着调两次 `retry_subtask`」的全部区别 —— 那两次是**同时**把两步
        放回队列（编排里的依赖是直连的，两步多半不互为直接上下游），会一起跑。

        **一次调用 = 一批 = 一个事务**：要么这几步全被重置回排队，要么一个字都没动。
        所以没有「一半成了」这种中间结果，调用方不用考虑部分成功。

        **前提与单步逐条相同**（任务已跑完、每一步都已结束、每一步「批外」的上游都已成功），
        只是对名单里**每一步**各查一遍。名单本身也有要求：不能空、不能重复、顺序不能跟
        编排的依赖方向拧着来（会成环 → 1001 明确拒绝）。

        **成不成的事，幂等也跟单步同一档**：上一次其实生效了、只是响应没回来 → 整批都
        已经排上队 → 一样回成功。这件事由 `TaskRuntime.retry_subtasks` 做掉，实现这个
        端口的人不用自己写重试循环。
        """
        ...

    async def get_task(
        self, tenant_id: str, task_id: str
    ) -> ApiResponse[Dict[str, Any]]:
        """读**整条任务**现在的状态（§4.3 `task.get`）。**纯只读。**

        与 `get_subtask` 是一对、**不能互相替代**：用户把任务停了时，`task.stop` 只判死
        还没开始的步骤，**正在跑的那一步仍是 `running`**，而**这条任务已经是 `failed`**
        —— 只看步骤永远看不出「停了」，只能死等自己的硬超时。所以运行方那条「放弃之前
        先问一句」的退路，**必须问任务**（或两条都问）。

        `data` 是裸 dict（形状同 `TaskItemResponse`），理由同 `get_subtask`。
        """
        ...

    async def get_subtask(
        self,
        tenant_id: str,
        task_id: str,
        node_id: str,
        *,
        attempt: Optional[int] = None,
    ) -> ApiResponse[Dict[str, Any]]:
        """**读**某一步现在的状态（§4.3 `task.subtask_get`，2026-09-30 开给运行方）。

        运行方从前**没有任何通道**能读管理方的状态（这里四个方法全是写口子），只能靠自己的
        超时硬等 —— 典型是「我这一步是不是已经被管理方判死了」这个问题问不出来。
        典型用法：**放弃之前先问一句**（看门狗准备按自己的超时交还时先读一次）。

        `data` 是**裸 dict**（形状同 `SubtaskItemResponse`），不是模型对象 ——
        协议层 / runtime 层不得 import `capabilities.*`（有测试钉住那道门禁），
        所以照 `init_task` 的先例回 dict、由调用方投影。`status` 是那一步的当前状态，
        看到 `success` / `failed` / `timeout` 就说明管理方那边它已经定局了。
        """


@runtime_checkable
class RunnerPort(Protocol):
    """运行方必须实现的**三个**方法（§4.1）。

    **这是结构契约，不是基类**：运行方继承 `RunnerBase`（`runner.py`）就自动满足它，
    但也可以自己实现这三个方法 —— `isinstance(runner, RunnerPort)` 一样为真。
    存在的意义是让「运行方必须实现这三个」从散文变成可断言的东西。
    """


    async def execute(self, execution: ExecutionDispatch) -> ApiResponse[None]:
        """跑某个节点的活（§4.1 ①）。`data` 为空。"""
        ...

    async def remove_task(
        self, tenant_id: str, space_id: str, task_id: str
    ) -> ApiResponse[None]:
        """按 `task_id` 清掉整个任务在本运行方侧的残留（§4.1 ②）。**必须幂等**。"""
        ...

    async def task_stopped(
        self, tenant_id: str, space_id: str, task_id: str
    ) -> ApiResponse[None]:
        """**通知**：这条任务被用户停了（§4.1 ③）。**停不停自己决定、必须幂等**。"""
        ...


__all__ = ["ControlPlaneClient", "RunnerPort"]
