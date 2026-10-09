#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""任务运行协议模型：跨进程报文的字段定义（§4.1 派活、§4.2 提交与响应）。

**这个模块谁也不依赖** —— 只有标准库、pydantic v1 与 `jonex_core.common.exceptions`。
不 import `capabilities.*`、不 import `jonex_core.capability.*`（含 `ApiResponse`）、
不 import logging。任务中心服务端、运行方 SDK、以后的可观测模块都能安全 import 它。

⚠️ **别拿"能 import 本模块"当作"能脱离 DB / Redis 运行"的凭据**：依赖
`jonex_core.common.exceptions` 意味着必然会连带 `jonex_core.common` 包的全部基础依赖
（那个包的 `__init__.py` 是饿汉式的，会一路拉起 database / cache / vector）。

**报文的定义只留这一份**：`capabilities/task_center/dtos/runner.py` 与
`capabilities/task_center/dtos/execution.py` 里的同名类是对这里的 re-export，
不是第二份定义 —— 两边各抄一份会让「任务中心用它造对象、运行方用它做 isinstance」
这种错位静默发生（都不报错，但语义已经错了）。
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

try:  # pydantic>=1.10 的 v1 兼容命名空间优先
    from pydantic.v1 import BaseModel, Field
except ImportError:  # pragma: no cover
    from pydantic import BaseModel, Field


class ExecutionEnvelope(BaseModel):
    """派活报文（`execute`）的字段定义（§4.1 ①）。

    一期「管理方 → 运行方」的出站报文只有两个：**派活**（`execute`，套这个壳）与
    **删整个任务**（`remove_task`，粒度是任务、用 `task_id` 一把清，定位不靠具体
    某一步，所以不套这个壳、单独一个 DTO）。

    **`step_code` 管分流、`node_id` 管寻址，两个都得给**：

    `step_code` 是「这个节点用的是哪个内置步骤」（如 `parse`），运行方按它分流到不同的
    执行模式；**同一个 `step_code` 可以出现在多个节点上**（`parse_1` / `parse_2` 都是
    `parse`），所以它做不了唯一键。

    `node_id`（编排里的节点，如 `parse_1`）是**「同一个 `step_code` 出现多次时，唯一能
    指认是哪一步」的那个东西**：`depends_on` / `next_steps` 连线、`@<node_id>.outputs.x`
    引用、以及运行方发起的「重试某一步」（§4.2 ③）都按它定位。所以它进了对外契约
    （§3.2.1、§4.1 ①）。

    ⚠️ **运行方把它当不透明标识**：原样存着、需要时原样带回（`retry_subtask` 的入参），
    **不要按 `parse_1` 这个形状去解析它**（别拆前缀、别假设末尾有编号）—— 形状是管理方
    的事，将来改了不会通知运行方。

    **为什么 `step_code` 必须显式给、不能省**：它推不出来。"运行方按 `inputs` 的形状自己
    判断"这条走不通——**入参形状相同的不同步骤是存在的**：`ingest`（把已解析的内容推
    进检索库）与 `compile`（编译出下游产物）入参都是「哪个对象 + 哪个归属」，形状一模
    一样，光看 `inputs` 分不开。所以由管理方显式给，而不是让运行方猜（§4.1 ①、§3.5）。
    """

    tenant_id: str = Field(description="租户 ID（运行方按租户隔离落数据时要用）")
    space_id: str = Field(description="空间 ID（任务所属空间；运行方按空间落数据时要用）")
    task_id: str = Field(description="任务实例 ID（tasks.id）")
    step_code: str = Field(
        description="这个节点用的是哪个内置步骤（= config.steps[].step_code，如 parse / "
                    "ingest / compile）；运行方按它分流到不同执行模式"
    )
    node_id: str = Field(
        description="这一步在编排里的节点 id（= config.steps[].node_id，如 parse_1）。"
                    "同一个 step_code 可能出现在多个节点上，所以「重试哪一步」只能按它"
                    "指认。⚠️ 当不透明标识用：原样存、原样带回，别解析它的形状"
    )
    execution_id: str = Field(description="管理方生成的受理凭证，上报时原样带回")

    class Config:
        extra = "ignore"


class ExecutionDispatch(ExecutionEnvelope):
    """派活报文：管理方调运行方 `execute(execution)` 的入参（§4.1 ①）。

    `execution_id` 由管理方在派活前生成好、随报文传下；运行方收下后原样记着，
    执行完用 `report_status` 带同一个 id 上报结果。`attempt` 不在这里——那是
    管理方自己的账，运行方不用管。
    """

    inputs: Dict[str, Any] = Field(
        default_factory=dict, description="已经填好的真实入参；没有入参就是空对象"
    )


class RemoveTaskDispatch(BaseModel):
    """删整个任务的报文：管理方调运行方 `remove_task(task_id, ...)` 的入参（§4.1 ②）。

    粒度是**整个任务**，所以只要三个字段——运行方按 `task_id` 把该任务在自己侧的
    全部残留一次清干净（§4.1 ②）。管理方删任务时调的就是它（§4.3 `task.delete`）。
    """

    tenant_id: str = Field(description="租户 ID")
    space_id: str = Field(description="空间 ID")
    task_id: str = Field(description="任务实例 ID（tasks.id）—— 按它清掉整个任务的残留")

    class Config:
        extra = "ignore"


#: `output_refs`（产出「去哪儿看」的引用）的**报文大小上限**（按 `json.dumps` 后的字符数）。
#:
#: 引用是**展示用**的小数据（一个 `kind` + 一组参数），不是产物正文 —— 给它一个硬上限，
#: 是为了从机制上防止有人往上报里塞切片/正文：那种大 payload 会拖慢每次上报与重试。
#:
#: **两侧同一个常量**：SDK 构造 `TerminalReport` 时先本地拦一道（出站前就炸，别白等重试），
#: 服务端 `_check_report_payload` 再拦一道（它才是落库的守门人）。改这里就是改两边。
OUTPUT_REFS_MAX_BYTES = 4096

#: 一条上报最多带几个引用。
OUTPUT_REFS_MAX_ITEMS = 16

#: **`kind` → 这个 kind 的接口要哪些 `params`**（写死的枚举表，2026-10-09 用户拍板）。
#:
#: ⚠️ **`kind` 必须带「运行方」前缀** —— 格式 `{运行方 code}.{产物类型}`：任务中心是**多运行方**
#: 的（`runner_definitions` 里一条一个），裸 `chunks` 这种名字迟早和别人撞（谁都在切块）。
#: 前缀取那条运行方的 `code`（知识库那条就是 `knowledge_base`）。加新运行方 = 前缀换成它的 code。
#:
#: 引用里**不带 `url` / `method`** —— 「这个 kind 该调哪个接口」由**前端**那张映射决定，
#: 平台侧校不了它（报文里压根没有地址）。好处是**注入面归零**：运行方连地址都碰不到，
#: 填不出 `https://别的站` / `javascript:` / `//evil.com` / `..`，也填不出 `DELETE`。
#:
#: ⚠️ 加新 kind = **这里加一行 + 前端加一个映射**（两边靠这份表对齐，不像 url 那样能在报文里自证）。
#: `required` 里的键必须给、且非空；`optional` 给了也行。表外的键（含未知键）**放行**。
OUTPUT_REF_PARAMS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "knowledge_base.chunks": {"required": ("document_id", "knowledge_base_id"), "optional": ()},
    "knowledge_base.ontology": {"required": ("document_id", "knowledge_base_id"), "optional": ()},
    "knowledge_base.wiki": {"required": ("document_id", "knowledge_base_id"), "optional": ()},
}

#: `kind` 的写死枚举。**从 `OUTPUT_REF_PARAMS` 推导** —— 别再单独维护一份，两份必然漂移。
OUTPUT_REF_KINDS: Tuple[str, ...] = tuple(OUTPUT_REF_PARAMS)


def check_output_refs(
    refs: Optional[List[Dict[str, Any]]],
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """校验 `output_refs`，返回 **`(reason_code, details)`**（`None` = 通过）。

    **判据只有这一份实现** —— SDK（`task_runtime.runtime._check_output_refs`）与服务端
    （`task_center.services.task_service._check_output_refs`）都调它，各自把 `reason_code`
    翻成自己的异常类型与文案（前者 `TaskProtocolError`，后者 `InvalidParameterError` + i18n）。
    两边各抄一遍必然漂移，而漂移的方向恰恰是「服务端收了一条 SDK 认为合法、前端却处理不了的引用」。

    规则（2026-10-09 定，用户拍板）：
      · 必须是「对象数组」，≤ `OUTPUT_REFS_MAX_ITEMS` 项，序列化后 ≤ `OUTPUT_REFS_MAX_BYTES`；
      · 每条**必填** `kind`（∈ `OUTPUT_REF_KINDS`）与 `params`（对象）；
      · `params` 里该 kind 声明的 `required` 键**必须给**、其值只允许**非空标量**；
        `optional` 里声明过的键，给了也同样按非空标量校验；
      · **未知键放行** —— 顶层与 `params` 内都是「只校验已知字段」，表外的键（运行方自己的
        扩展字段）**连值都不看**，嵌套对象 / 数组一样放行（总大小仍受
        `OUTPUT_REFS_MAX_BYTES` 约束）。
    """
    if refs is None:
        return None
    if not isinstance(refs, list) or any(not isinstance(item, dict) for item in refs):
        return ("not_array", {"output_refs_type": type(refs).__name__})
    if len(json.dumps(refs, ensure_ascii=False)) > OUTPUT_REFS_MAX_BYTES:
        return ("too_large", {"max": OUTPUT_REFS_MAX_BYTES})
    if len(refs) > OUTPUT_REFS_MAX_ITEMS:
        return ("too_many", {"max": OUTPUT_REFS_MAX_ITEMS, "got": len(refs)})

    for index, item in enumerate(refs):
        kind = item.get("kind")
        if not kind:
            return ("kind_missing", {"index": index})
        if kind not in OUTPUT_REF_PARAMS:
            return (
                "kind_invalid",
                {"index": index, "kind": kind, "valid": list(OUTPUT_REF_PARAMS)},
            )

        params = item.get("params")
        if not isinstance(params, dict):
            return ("params_not_object", {"index": index, "kind": kind})

        spec = OUTPUT_REF_PARAMS[kind]
        for key in spec["required"]:
            if key not in params:
                return ("params_missing_key", {"index": index, "kind": kind, "key": key})
        # ⚠️ **只校验该 kind 在表里声明过的键**（`required` + `optional`）；**表外的键
        #    （含未知键）整条不碰、值长什么样都放行** —— 这就是上面那张表、
        #    `check_output_refs` 的 docstring 与 `CLAUDE.md` 三处同口的那句「未知键放行」。
        #    别照「遍历 `params.items()`」去理解它：那样会把运行方自己的扩展字段
        #    （典型是嵌套对象）判成 `params_bad_value`，**整条终态上报都发不出去** ——
        #    而 `params` 总大小本来就被 `OUTPUT_REFS_MAX_BYTES` 卡着。
        for key in (*spec["required"], *spec.get("optional", ())):
            if key not in params:
                continue
            value = params[key]
            # 非空标量：`None` / 空串 / 容器都拒 —— 这些让「参数给了但等于没给」，
            # 而前端拿到空值只能拼出一个查不到的请求。
            if not isinstance(value, (str, int, float)) or (
                isinstance(value, str) and not value.strip()
            ):
                return ("params_bad_value", {"index": index, "kind": kind, "key": key})
    return None


class TaskStoppedDispatch(BaseModel):
    """「这条任务被用户停了」的报文：管理方**通知**运行方（§4.1 ③，2026-09-30 加）。

    ⚠️ **这是通知，不是命令** —— 管理方只是把「用户点了停止、这条任务已就地收成
    `failed`」这个**事实**告诉运行方，**停不停、怎么停，由运行方自己决定**：能停就停，
    停不掉（比如下游任务已经发出去了）就**只把接管的文档交还出去**，那也是合法的回应。

    为什么非要有它：`task.stop` 从前**只在任务中心内部办、不出站**（判死还没开始的那几步
    + 把任务与当次运行就地收口），后果是运行方**不知道任务已经终止**，继续盯着那份被接管的
    文档 —— 于是用户在文档页**重新解析 / 删除文档 / 整库重置**三个入口**全被拦**，要等那个
    还在跑的活自己结束（再等 30 分钟兜底）、或最长到 6 小时硬超时。用户看到的是「任务页说
    已停、文档页说还在处理中」。**2026-09-30 起收口之后补发这条通知**，就是来堵这个缺口的；
    它仍然是**尽力而为**的，所以运行方那侧还得有自己的兜底（例如定期读一次管理方状态）。

    粒度是**整个任务**（与 `RemoveTaskDispatch` 同）：`task.stop` 能停的只有**还没开始**
    的那几步，它们**从来没派出去过**、运行方那边没有任何残留需要指认 —— 真正要告诉它的
    是「这条任务整体停了，别再等它了」。所以只要三个字段。

    到达方式：**尽力而为** —— 管理方**单次发送、不重发**（超时见 `TASK_CENTER_NOTIFY_TIMEOUT`），
    失败只记一条 warning，**绝不让「停止」本身失败**（停止是本地的事，不该被一次网络报错
    拖住）。所以丢了也只是退回 30 分钟兜底那条老路，不会更差。

    **幂等是硬要求**：任务不存在 / 已经交还过 / 压根没接管过，运行方**都必须回成功** ——
    今天虽然不重发，但「重复调用不出错」是这条契约的前提（运维手工重放、或将来改成带重发）。
    """

    tenant_id: str = Field(description="租户 ID")
    space_id: str = Field(description="空间 ID")
    task_id: str = Field(description="任务实例 ID（tasks.id）—— 这条任务被用户停止了")

    class Config:
        extra = "ignore"


class TaskSubmission(BaseModel):
    """发起方（运行方）要提交的一条任务（§4.2 ② `init_task` 的入参）。

    **不含 `tenant_id`**：租户是身份、不是业务字段，由
    `TaskRuntime.submit(..., tenant_id=...)` 显式给，或回落 `TenantContext`。

    ⚠️ **提交路径不能只靠 `TenantContext`**：后台投递器（参考文档 Task 6 的 outbox
    dispatcher）不是请求路径，那一帧里没有上下文，回落会收出空租户、`require_tenant`
    抛 `TenantIsolationError`（3005）—— 而 3005 是「明确拒绝」，在后台会被判成
    永不重投的死信。**后台调用必须显式传 `tenant_id`。**

    **本版没有幂等键**：服务端 `init_task` 现在不做幂等去重（调两次建两条独立任务），
    所以既不接 `idempotency_key` 也不接 `labels` —— 接了也只能静默忽略，那会让
    「新建」这个结论变成谎话。等服务端幂等落地再按加法补。
    """

    definition_id: str = Field(description="用哪个编排定义")
    space_id: str = Field(description="这次运行落在哪个空间（必须是该版本自带的空间）")
    task_id: Optional[str] = Field(
        default=None,
        description="任务 id 由**发起方自己生成**时填进来；不传 = 管理方生成。"
                    "发起方要先把 id 落到自己的行上（不然只能等回包才知道写什么）就填它 —— "
                    "管理方收下即用；撞了已有任务回 `ResourceAlreadyExistsError`（1006），"
                    "对自带 id 的调用方那是「复用既有任务」（上一次其实建成了、只是响应没回来），"
                    "**不是拒绝** —— 详见 `SubmissionDisposition.REUSED`",
    )
    inputs: Dict[str, Any] = Field(
        default_factory=dict, description="任务级入参；没有就是空对象"
    )
    version_no: Optional[int] = Field(
        default=None, description="钉哪个版本；不传 = 该定义已发布的、版本号最大的那一版"
    )
    name: Optional[str] = Field(
        default=None, description="任务实例名；不传或只给空白 = 退回定义名（纯展示，不影响跑哪一版）"
    )
    operator_id: Optional[str] = Field(
        default=None,
        description="发起这次操作的人（user_id，不是用户名）；拿不到就留空 —— "
                    "空值是合法状态（触发器/内部调用本来就没有人），不是漏记",
    )

    class Config:
        extra = "ignore"


class TaskItemResponse(BaseModel):
    """任务实例（读的是 `tasks` 的最新快照）。"""

    id: str = Field(description="主键：任务的稳定身份，重跑也不换")
    tenant_id: Optional[str] = Field(default=None, description="租户")
    definition_id: str = Field(description="用的是哪个定义")
    definition_version_no: int = Field(description="用的是哪个版本号（钉死）")
    space_id: Optional[str] = Field(default=None, description="空间 ID（取自这次运行所用的那个版本）")
    task_name: Optional[str] = Field(default=None, description="任务实例名")
    trigger_id: Optional[str] = Field(default=None, description="是哪个触发触发的")
    inputs: Dict[str, Any] = Field(
        default_factory=dict, description="这次运行的任务级入参（收口后的实值）"
    )
    current_attempt: int = Field(default=1, description="当前跑到第几次运行（指针，首次=1）")
    status: str = Field(
        default="pending", description="pending / running / completed / failed"
    )
    error_message: Optional[str] = Field(default=None, description="失败原因")
    state_version: int = Field(default=0, description="每行版本号（防重复改）")
    created_by: Optional[str] = Field(
        default=None,
        description="建这条任务的人（user_id，不是用户名）；空 = 不是人发起的（触发器/内部调用）",
    )
    updated_by: Optional[str] = Field(
        default=None,
        description="最后一次「用户主动写」这条任务的人（user_id）；系统自动推进状态不写这一列",
    )
    created_at: Optional[str] = Field(default=None, description="创建时间")
    started_at: Optional[str] = Field(default=None, description="首次开始时间")
    finished_at: Optional[str] = Field(default=None, description="最新一次运行结束时间")
    updated_at: Optional[str] = Field(default=None, description="更新时间")

    class Config:
        extra = "ignore"


__all__ = [
    "ExecutionDispatch",
    "ExecutionEnvelope",
    "RemoveTaskDispatch",
    "TaskItemResponse",
    "TaskStoppedDispatch",
    "TaskSubmission",
]
