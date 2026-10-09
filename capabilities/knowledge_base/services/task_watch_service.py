#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""30 秒看门狗：盯住每一步的下游跑完没有，跑完了就替它上报终态。

挂在既有对账循环（`KnowledgeBaseCapability._reconcile_loop`）的**尾部**，复用它的
30 秒节拍与 try/except —— **不新起线程、不另拉循环**。一轮 `sweep()` 扫一批接管行，
逐条判定、逐条处置。

## 为什么必须有它（两条都是硬需求，不是优化）

1. **「活干完了」这件事只有运行方知道**。任务中心看不见 atomic-rag（它连
   `capabilities/knowledge_base/` 都不直接依赖），派活出去就只等上报。而 handler 又不能
   自己上报 —— 它只是**把任务发出去**（真正的活要跑几十分钟，等在 handler 里会撞上派活
   侧 30 秒的读超时）。所以必须有第三个角色：既看得见 atomic-rag、又拿得到
   `execution_id` 的那一个。就是它。
2. **它是用户被拦住的解封点**。接管期间用户的「重新解析 / 重新抽取 / 重置 schema」一律
   返回冲突（§3.7，用户被明确要求「等」）。要是这条链卡住了没人收口，用户就被**永久**
   拦住。本服务的每个判死出口都会把接管行置 `released` —— 那一刻起文档交还给既有对账，
   用户就能操作了。

## 判定表（`_inspect` 的主干）

按接管行「走到哪了」分三段，每段各有自己的超时：

| 接管行现状 | 动作 |
| --- | --- |
| `state='submitting'`，未超 `TC_SUBMIT_RETRY_SEC` | 等（上传那次 submit 还在重试中） |
| `state='submitting'`，已超 | **重新 submit 同一个 `task_id`**（撞 1006 = 上次其实建成了，SDK 判 `REUSED`，收工） |
| `state='submitting'`，次数用尽或超 `TC_SUBMIT_DEADLINE_SEC` | 判死：文档 FAILED + 接管行 `released` |
| 无 `execution_id`，超 `TC_NO_DISPATCH_SEC` | 交还：任务中心一直没派活（含「两个按钮」武装好之后那次重跑没人派」） |
| 有 `execution_id`、无 `rag_task_id`，超 `TC_NO_DOWNSTREAM_SEC` | 判死：这一步收了活却没发出下游 |
| 有 `rag_task_id`，超 `TC_STEP_HARD_TIMEOUT_SEC` | 判死：整步硬超时 |
| `compile` 步，`rag_task_id` 已写 | 看 `ontology_status`：`READY` → 成功；`FAILED` → 失败；其余等 |
| 其余两步，`rag_task_id` 已写 | 问 atomic-rag：`completed` → 收口；`failed`/`cancelled` → 失败；`not_found`/取不到/在跑 → 等 |

## 两个按钮：本服务**一个字都不用管**

「重新解析 / 重新编译」是任务中心现成的「重跑某一步 / 某几步」（不新建任务），发起
不在这里（在用户请求里：`document_service.reparse_document` / `ontology_service.retry_extract`）。

⚠️ **这一节写下来是为了挡住一个曾经写过的设计**：早先的版本让本服务在解析步收口时
**替任务中心补调第二步**（把 `current_step` 推成 `compile`、下一拍再调一次「重跑
`compile_1`」）。那是在任务中心还没有批量口时的替代方案 —— 用户后来给任务中心加了
`retry_subtasks`（§4.2 ④，一次调用、按给的顺序重跑几步），于是**「重新解析」两次重跑
在用户请求里就一起发出去了**，先后由任务中心写进依赖（第 i 步等第 i-1 步成功）。

所以本服务现在只做**被动等待**：那两个按钮武装好之后（`rearm_for_step` 把行打回
`managed`、`current_step` 标成要重跑的那一步、清空在途痕迹），派活来了就按正常流程盯
（`begin_step` 会重新写上 `current_step` 与 `execution_id`），迟迟不来就走上表那条
「无 `execution_id` 超 `TC_NO_DISPATCH_SEC` → 交还」。**不要在这里恢复补调** ——
两边都调会让同一串步骤被重跑两遍。

**`not_found` 与「取不到」绝不判死**：前者可能是「任务行被清理了但产物还在」
（既有对账的查证链就是为这种情况准备的），后者可能只是一次 HTTP 抖动。两者都交给硬超时。

## 编排形状（本服务只认文档行上的粘性值）

Ontology 库一律进任务中心，**走哪条编排由提交时定下的 `ingest_backend` 决定**
（文档行的 `extra_metadata["ingest_backend"]`，`backend_for_document` 读它）：

| `backend` | 这次是哪种操作（`tc_intent`） | 步骤 | 「切片已入库」发生在哪一步 | 这一步的文档状态 |
| --- | --- | --- | --- | --- |
| `rag`（默认） | `upload` | `parse` → `compile` | **`parse`**（`full` 解析 + 多模态 + 推切片，一趟做完） | `parse` → `ready` |
| `rag` | `reparse` | 同上（重跑的是 `parse_1` + `compile_1`） | **`parse`**（`reparse_strict` 解析 + 删旧切片 + 推新切片） | `parse` → `ready` |

**`parse` 结束时切片已经可检索** —— 所以**置 `ready` 的判据是「这一步是不是
`parse`（或改版前遗留的 `ingest`）」**，`tc_intent` 那一列只用于运行方**选引擎档位**
（`full` / `reparse_strict`），本服务不读它。⚠️ 判据仍然**不能按 `current_step` 的字面量
猜**：算错就是把搜索在一个空库上放行，或者让 `ready` 永远不来（这一步的收口只此一处）。

⚠️ 这个形状是 2026-09-29 改的（原来 `rag` 链是三步 `parse → ingest → compile`，
`parse` 只解析）：`parse` 现在自己推切片，`ingest` 只剩**改版前遗留的在途任务**会走到
（它们的子任务已经固化在库里），所以那一步照样置 `ready`。

**问谁的状态也是它决定的**，但那一层在 `_fetch_task_status` 里（`reconciliation_service`
已按同一个粘性值分派到 atomic-rag），本服务把文档行原样喂给它就行，不重复判一次。

## 三条纪律

- **判 `compile` 看 `ontology_status`，不看 atomic-rag 的任务状态**。写 Neo4j 的唯一入口
  是既有对账的 EXTRACTING 分支，它会**自己重抽**（最多 `MAX_ONTOLOGY_RETRIES` 次、每次都
  换一个新的 `rag_task_id`）。拿接管行上那个（会过期的）任务号判，会在对账还在重抽时就
  报失败。
- **每一步的终态上报都带 `document_id`**。编排里下游两步的 `bind_inputs` 写的是
  `@<上一步>.outputs.document_id`（见 `008_task_center.sql` 的定义段）—— 漏了它下游拿到
  空值，最后一步查不到文档。这是这条链最容易踩的坑。
- **一次 `get_db_session()` 里改完**接管行与文档行。两份状态必须一致（模型文件的模块
  docstring 写了同一条纪律）：分两次提交的话，中间崩一下就留下「接管行说这一步完了、
  文档行还停在旧状态」。
- **先上报、后收口**。收口那次 UPDATE 会把在途痕迹一起清空、`compile` 还把接管行置
  `released` —— 那之后这一行**再也不进本服务的视野**。而 `report_terminal` **不抛异常**
  （重试 3 次后把 disposition 交回来），先收口就等于把「一次没被受理的上报」变成
  「一次永久丢失的成功」。反过来的代价只是重复上报：收件箱 `subtask_reports` 没有唯一
  约束，重复到达的那条由后台消化的「已是终态」闸吸收 —— 所以这个方向是**自愈的**
  （见 `_finish_step`）。

## 阈值

全部走 `os.getenv`（先例：`RAG_TASK_HARD_TIMEOUT_SEC` / `ONTOLOGY_MAX_INFLIGHT` /
`RECONCILE_SUBMIT_GRACE_SEC`），登记在 `deploy/.env.example`。**硬超时取 21600 秒而不是
编排里的 25200**：差的 3600 秒留给「上报 3 次重试 + 状态落库」，让掌握真相的这一方先说。
"""

import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

from jonex_core.common.database import get_db_session
from jonex_core.task_protocol.results import TerminalSubmitDisposition
from jonex_core.task_runtime import ExecutionContext, TerminalReport

from ..models import DocStatus, OntologyStatus
from ..models.task_center import TaskCenterDocState
from ..repository.document_repository import KnowledgeDocumentRepository
from ..repository.task_center_repository import TaskCenterDocumentRepository
from . import ingest_backend
from .reconciliation_service import _fetch_task_status
from .task_runner_service import (
    ONTOLOGY_DEFINITION_ID,
    # 交还接管行之前先把在途任务号交给文档行（与 `task_stopped` 共用同一份实现）。
    hand_over_in_flight_task_id,
    # `step_code` → 节点名（`parse` → `parse_1`）：`task.subtask_get` 按 `node_id` 寻址，
    # 而接管行上存的是 `step_code` —— 两边的映射只有 `_NODE_BY_STEP` 一处出处。
    node_id_for_step,
    submission_accepted,
    submit_ontology_task,
)
# `_KB_TYPE_OPENKB` / `_KB_TYPE_JONEX_WIKI` 是运行方那侧的私有常量，这里按名取用
# （与 `_fetch_task_status` 同一做法）：两个模块必须对「这两类 wiki 库的取值是什么」
# 有**同一个**事实来源。
from .task_runner_service import _KB_TYPE_JONEX_WIKI, _KB_TYPE_OPENKB

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    """读一个正整数阈值；缺失 / 脏值 / 非正数一律回落默认值。

    与 `RAG_TASK_HARD_TIMEOUT_SEC` 那几处的读法同构：**配置写坏不该让看门狗失灵** ——
    它失灵的表现是所有在途文档一律判死，比「用了默认值」严重得多。
    """
    try:
        value = int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


#: 补交之间的最小间隔。上传那次 `submit` 自带 3 次重试（0.5s / 1s），所以 30 秒足够
#: 让它先跑完 —— 看门狗只处理「它彻底失败了」的情况。
TC_SUBMIT_RETRY_SEC = _env_int("TC_SUBMIT_RETRY_SEC", 30)
#: 补交次数上限。到顶就不试了（再试也是同一个错）。
TC_SUBMIT_MAX_ATTEMPTS = _env_int("TC_SUBMIT_MAX_ATTEMPTS", 10)
#: 从接管行建立算起的补交窗口。超了 → 判死、交还给既有对账。
TC_SUBMIT_DEADLINE_SEC = _env_int("TC_SUBMIT_DEADLINE_SEC", 1800)
#: 「任务已提交、任务中心一直没派活」的容忍上限。默认 30 分钟 —— 派活是 30 秒一轮的
#: 后台循环，正常几秒内就到；半小时没动静说明它那侧卡住了（并发到顶 / 循环挂了）。
TC_NO_DISPATCH_SEC = _env_int("TC_NO_DISPATCH_SEC", 1800)
#: 「收了活、却没发出下游」的容忍上限。正常的这个窗口只有一次 HTTP 调用的长度
#: （毫秒级），10 分钟只可能是进程死在中间了。
TC_NO_DOWNSTREAM_SEC = _env_int("TC_NO_DOWNSTREAM_SEC", 600)
#: 单步硬超时，**必须小于编排里的 `timeout_seconds`（25200）**：让掌握真相的这方先下判断。
TC_STEP_HARD_TIMEOUT_SEC = _env_int("TC_STEP_HARD_TIMEOUT_SEC", 21600)

#: `output_refs` 里 `kind` 的**运行方前缀** = 本运行方在 `task_center.runner_definitions`
#: 里的 `code`。任务中心是**多运行方**的，`kind` 不带前缀迟早和别人撞（谁都在切块）——
#: 协议层那张枚举表（`OUTPUT_REF_PARAMS`）就是按 `{前缀}.{产物类型}` 编的。
_REF_KIND_PREFIX = "knowledge_base"

#: 拿不到真因时的兜底文案：把读者指回文档行的 `ontology_error`。
#: 那个字段确实该有答案 —— 只有当对账那侧也一个字都没读回来时才会落到这里。
_COMPILE_FAILED_POINTER = "知识编译失败（本体抽取未成功，详见文档的本体错误信息）"


def _compile_failure_reason(ontology_error: str | None) -> str:
    """`compile` 步失败的收口文案：**把文档行上的真因抄进来**。

    原来只写死一句「详见文档的本体错误信息」—— 那是个**悬空引用**：真因确实该写在
    `ontology_error` 上，可当它没写上时（2026-10-09 事故：那一刻字段里躺的是对账刚
    覆盖的「本体重试次数已达上限 (3/3)」），这句话就把人指到一个没有答案的地方，
    而且看的人没理由怀疑它。

    真因在手就抄进来：任务中心那侧看到的就是原因本身，不必再去另一个字段上找一圈、
    再发现里面是别的东西。真因拿不到才退回那句指路文案。
    """
    detail = (ontology_error or "").strip()
    if not detail:
        return _COMPILE_FAILED_POINTER
    return f"知识编译失败（本体抽取未成功）：{detail}"


class TaskWatchService:
    """接管行的看门狗。无状态，可随意实例化（与 `DocumentService` 同风格）。"""

    # ========================================================
    # 入口
    # ========================================================

    async def sweep(self, limit: int = 50) -> Dict[str, int]:
        """扫一批在途接管行，逐条判定并处置。返回各档计数（只进日志）。"""
        rows = await self._load_active(limit)
        stats: Dict[str, int] = {}
        for row in rows:
            try:
                action = await self._inspect(row)
            except Exception:  # noqa: BLE001
                # **一条坏行不能拖垮整轮**：这一批里还有别人的文档在等收口。
                # 外层循环的 try/except 只兜得住「整轮抛」，兜不住「第 3 条抛了、
                # 后面 47 条一条没跑」。
                logger.exception(
                    "看门狗处理接管行失败（跳过本条）: doc=%s task=%s",
                    row.get("document_id"), row.get("task_id"),
                )
                action = "error"
            stats[action] = stats.get(action, 0) + 1
        return stats

    async def _load_active(self, limit: int) -> list[Dict[str, Any]]:
        """把 `state IN ('submitting','managed')` 的行读成普通 dict。

        读成 dict 而不是留着 ORM 对象：后面要跨好几段事务，会话一关对象就 detached，
        到那时再访问属性会炸（或在异步里触发一次隐式 IO）。**在会话内把要用的列全部
        取出来**，后面全程只用值。
        """
        async with get_db_session() as session:
            rows = await TaskCenterDocumentRepository(session).list_active(limit=limit)
            return [
                {
                    "document_id": r.document_id,
                    "tenant_id": r.tenant_id,
                    "knowledge_base_id": r.knowledge_base_id,
                    "space_id": r.space_id,
                    "task_id": r.task_id,
                    "operator_id": r.operator_id,
                    "state": r.state,
                    "current_step": r.current_step,
                    "execution_id": r.execution_id,
                    "handled_execution_id": r.handled_execution_id,
                    "rag_task_id": r.rag_task_id,
                    "step_started_at": r.step_started_at,
                    "submit_attempts": r.submit_attempts or 0,
                    "created_at": r.created_at,
                    "updated_at": r.updated_at,
                }
                for r in rows
            ]

    async def _doc_facts(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """一次读出后面要用的**文档行事实**：在不在、走哪条编排、叫什么名字、哪类库。

        做成一个口子、而不是各读一遍：补交那条路同时要「粘性值」（决定建到哪条编排上）、
        「文档名」（当任务名）与「库类型」（与粘性值一起选编排，见
        `definition_for_kb_type`）。分几次读就是同一个 `get_by_id` 调几遍，而几遍之间
        文档可能被删掉 —— 那会拿到互相矛盾的事实（A 的后端 + B 的名字）。

        `backend` 是文档行上的**粘性值**，**只读它、不读 env**（`ingest_backend` 模块
        docstring 的同一条纪律：读 env 的话开关一翻转就把在途文档判去问错的后端）。
        文档行缺失 / 值域外一律回落 `rag` —— `backend_for_document` 的既定语义，
        这里不另判一份。

        `kb_type` 照既有对账的口径取：**优先读文档行上的快照（`extra_metadata.kb_type`）、
        读不到才查 `knowledge_info`**（那一列是上传时顺手记的，老文档没有）。
        """
        async with get_db_session() as session:
            doc = await KnowledgeDocumentRepository(session).get_by_id(
                row["document_id"], row["tenant_id"]
            )
            if doc is None:
                return {"exists": False, "backend": "rag", "file_name": None,
                        "kb_type": None}
            kb_type = (getattr(doc, "extra_metadata", None) or {}).get("kb_type")
            if not kb_type:
                from .kb_type_service import get_kb_type

                kb_type = await get_kb_type(row["tenant_id"], row["knowledge_base_id"])
        return {
            "exists": True,
            "backend": ingest_backend.backend_for_document(doc),
            "file_name": getattr(doc, "file_name", None),
            "kb_type": kb_type,
        }

    # ========================================================
    # 判定
    # ========================================================

    async def _master_already_settled(self, row: Dict[str, Any]) -> bool:
        """问管理方：**这次编排已经结束了吗**？（§4.3 `task.subtask_get` / `task.get`）

        为什么需要它：运行方从前**没有任何通道**能读管理方的状态（四个方法全是写口子），
        所以「我是不是已经被判死了」这个问题**问不出来**，只能死等自己的硬超时（最长 6 小时）。

        ⚠️ **必须问两样，只问步骤是不够的**（这是这条退路的关键）：

        - **先问那一步**（`task.subtask_get`）：它已是 `success` / `failed` / `timeout`
          → 定局 ✓；
        - **不是终态时再问这条任务**（`task.get`）：任务已是 `completed` / `failed`
          → 也定局 ✓。

        只看步骤会漏掉**最常见的那一格**：用户把任务**停了** —— `task.stop` 只判死
        **还没开始**的步骤，**正在跑的那一步原地不动**（运行方没有停止能力，杀了也白杀），
        所以那一刻**步骤仍是 `running`、而任务已经是 `failed`**。只读步骤就永远看不出
        「停了」，只剩 6 小时硬超时 —— 那正是「停止之后文档还被锁着」这件事本身。

        ⚠️ **失败一律回 `False`**（读不到、租户不对、管理方没起来……）：这是一次**只读的
        兜底探查**，不该因为它失败而改变任何既有行为 —— 问了没问到，退回按自己的超时判。

        ⚠️ **两边都问会多一次调用**，但只在「步龄已过 `TC_NO_DOWNSTREAM_SEC`」时发生
        （见 `_inspect`），而且第一条只在「那一步不是终态」时才接着问第二条。

        ⚠️ 节点名走 `node_id_for_step`（`step_code` → `parse_1` 那种），**不是** `step_code`：
        管理方按 `node_id` 寻址。⚠️ 那张表**只列 `parse` / `compile`**（为「重新解析 /
        重新编译」两个按钮收窄的），所以改版前遗留的 `ingest` 步在这里会抛 —— 被
        `except` 吞成「不知道」（**不回 True**，保守），退回硬超时那条老路。
        """
        from jonex_core.task_runtime import get_task_runtime

        try:
            # ⚠️ **`get_task_runtime()` 也要在 try 里**：运行时没装配时它会抛
            # `TaskRuntimeNotBuiltError` —— 那同样该按「探查不了」处置（回 False），
            # 不该让它冒到巡检循环里去（开发期真漏过一次，被本文件的用例逮到）。
            runtime = get_task_runtime()
        except Exception as exc:  # noqa: BLE001 —— 探查失败不改行为，见 docstring
            self._warn_probe_failed(row, exc)
            return False

        # ① 先问**那一步**（省一次调用）—— ⚠️ **它必须是单独一层 try**：
        #    `node_id_for_step` 那张表只列 `parse` / `compile`（为「重新解析 / 重新编译」
        #    两个按钮收窄的），改版前遗留的 `ingest` 步在这里就会抛 —— **绝不能让那次
        #    异常把下面第 ② 问一起带走**：遗留的 `ingest` 步恰恰是最容易长期挂着的那批，
        #    而第 ② 问是「用户停了任务」那一格的**唯一**出口（停了任务时步骤仍是
        #    `running`，只有任务那一问看得出来）。
        node_id = ""
        try:
            node_id = node_id_for_step(row["current_step"] or "")
        except Exception as exc:  # noqa: BLE001
            self._warn_probe_failed(row, exc)
        if node_id:
            try:
                step = await runtime.get_subtask(row["tenant_id"], row["task_id"], node_id)
            except Exception as exc:  # noqa: BLE001
                self._warn_probe_failed(row, exc)
                step = None
            if step is not None and step.success and isinstance(step.data, dict):
                if str(step.data.get("status") or "") in ("success", "failed", "timeout"):
                    return True

        # ② 不是终态（或那一步压根问不到）→ **再问整条任务**。
        try:
            task = await runtime.get_task(row["tenant_id"], row["task_id"])
        except Exception as exc:  # noqa: BLE001
            self._warn_probe_failed(row, exc)
            return False
        if task.success and isinstance(task.data, dict):
            if str(task.data.get("status") or "") in ("completed", "failed"):
                return True
        return False

    @staticmethod
    def _warn_probe_failed(row: Dict[str, Any], exc: BaseException) -> None:
        """探查失败**一定要留痕**：能抛到那里的**不是**传送层失败（那种是回
        `success=false` 的壳），而是「我们自己写错了」（映射表没有这一步、运行时没装配……）。

        开发期真踩过两次：`node_id_for_step` 忘了导入 → `NameError`；`TaskRuntime` 少了
        `get_task` → `AttributeError` —— 两回都被「失败一律回 False」的兜底吞成「读不到」，
        特性**静默失效**、行为上只表现为「没生效」。

        **用 warning 而不是 debug**：默认 `LOG_LEVEL=INFO`，debug 等于没记。
        """
        logger.warning(
            "任务中心读状态出错（仅影响这条兜底，不影响本步判定）: task=%s step=%s: %s",
            row.get("task_id"),
            row.get("current_step"),
            exc,
        )

    async def _inspect(self, row: Dict[str, Any]) -> str:
        """一条接管行现在该干什么。返回一个档位名（只用于计数与日志）。"""
        if row["state"] == TaskCenterDocState.SUBMITTING.value:
            return await self._supervise_submission(row)

        execution_id = row["execution_id"] or ""
        if not execution_id:
            # 上一步已收口、正在等下一步派活。计时基准是 `updated_at` ——
            # `mark_step_handled` 刚写过它，所以这个钟从「上一步完成」开始走。
            # 「两个按钮」那条路也落在这一档（`rearm_for_step` 武装好、等任务中心派活），
            # 同一个钟、同一个出口 —— 模块 docstring 那一节讲了为什么不另开一档。
            elapsed = self._elapsed(row["updated_at"])
            if elapsed > TC_NO_DISPATCH_SEC:
                await self._abandon(
                    row,
                    f"任务中心超过 {TC_NO_DISPATCH_SEC} 秒未派活（任务 {row['task_id']}）",
                )
                return "no_dispatch_timeout"
            return "waiting_dispatch"

        step_started_at = row["step_started_at"] or row["updated_at"]
        elapsed = self._elapsed(step_started_at)
        # `compile` 步判死**不动文档状态**：那一刻文档通常已经是 `READY`（切片已入库、
        # 检索可用，缺的只是图），把它标成 FAILED 等于让一次「本体没抽出来」或者
        # 「我们自己没盯住」把一份能检索的文档整个打成坏的 —— 与 `_inspect_compile`
        # 对本体失败的口径（本体失败只落 `ontology_status`，文档仍是 READY）同一条理由。
        # 另两步相反：文档还停在 `PARSING`，这一步判死就意味着它永远不会变成可检索的，
        # 必须落 FAILED。
        fail_document = (row["current_step"] or "") != "compile"
        if elapsed > TC_STEP_HARD_TIMEOUT_SEC:
            # 硬超时对**所有**在途形态一视同仁：只要这一步起了、超了上限，就收口。
            await self._fail_step(
                row,
                f"步骤 {row['current_step']} 超过 {TC_STEP_HARD_TIMEOUT_SEC} 秒未完成",
                fail_document=fail_document,
            )
            return "step_hard_timeout"

        # ⚠️ **在长等之前先问一句管理方**（§4.3 `task.subtask_get`，2026-09-30 加）：
        #    我这一步是不是**已经被它判死了**？是的话就没必要再轮询 downstream ——
        #    典型是「用户把任务停了」：管理方那边这一步已是终态，而那个在跑的 atomic-rag
        #    任务杀不掉、会一直跑到它自己结束，文档就一直被锁着。
        #
        #    门限复用 `TC_NO_DOWNSTREAM_SEC`（10 分钟）：**刚起的步不查** —— 那时候它
        #    本来就在正常跑，查了也是白问一次；过了 10 分钟还没动静才值得每个巡检周期
        #    问一次（一次只读调用，比让用户干等 6 小时便宜得多）。
        if elapsed > TC_NO_DOWNSTREAM_SEC and await self._master_already_settled(row):
            # 管理方那边**这次编排已经结束**（这一步已终态，或整条任务已终态 —— 后者正是
            # 「用户把任务停了、而那一步仍在跑」那一格）。再等下去毫无意义：那一步要么
            # 跑完上报（管理方已是终态、会被丢掉），要么拖到 6 小时硬超时。
            #
            # ⚠️ **已知差异（记档，不改）**：这一支走 `_abandon` —— **只交还接管行、
            # 不动文档状态**；而下面「收了活却没发出下游」那一支走 `_fail_step`（文档落
            # `FAILED` + 补一次失败上报）。**两条会命中同一格**（管理方已判终态、而这里也
            # 还没发下游），先命中的是**这一条**。后果：文档留在 `PARSING` 由既有对账接手
            # —— 它会 **re-insert（重复解析一次）** 而不是干脆落 FAILED。
            # **现役编排够不到这一格**：进得来要「步龄超 `TC_NO_DOWNSTREAM_SEC`（10 分钟）
            # 且管理方已终态」，而平台四份编排的 `timeout_seconds` 都是 25200；只有把它调到
            # 10 分钟以下的自定义编排才会撞上。真撞上了再决定「交还」还是「落 FAILED」。
            await self._abandon(row, "管理方那边这次编排已经结束，不再等它")
            return "master_settled"

        if not (row["rag_task_id"] or ""):
            if row["current_step"] == "compile":
                queued = await self._inspect_queued_wiki_compile(row)
                if queued is not None:
                    return queued
            # 收到了派活、但没把下游任务号写回来。正常只有一次 HTTP 调用的宽度。
            if elapsed > TC_NO_DOWNSTREAM_SEC:
                await self._fail_step(
                    row,
                    f"步骤 {row['current_step']} 已受理但未发出下游任务"
                    f"（超过 {TC_NO_DOWNSTREAM_SEC} 秒）",
                    fail_document=fail_document,
                )
                return "no_downstream_timeout"
            return "waiting_downstream"

        step_code = row["current_step"] or ""
        if step_code == "compile":
            return await self._inspect_compile(row)
        return await self._inspect_rag_task(row, step_code)

    async def _inspect_queued_wiki_compile(self, row: Dict[str, Any]) -> Optional[str]:
        """A fenced Wiki request may wait for other parses without a fake task id."""
        async with get_db_session() as session:
            doc = await KnowledgeDocumentRepository(session).get_by_id(
                row["document_id"], row["tenant_id"],
            )
            if doc is None:
                return None
            metadata = doc.extra_metadata or {}
            if metadata.get("jonex_wiki_compile_execution_id") != row["execution_id"]:
                return None
            status = (doc.llm_wiki_compile_status or "").lower()
            error = doc.llm_wiki_compile_error
            if status == "stale":
                return "waiting_upload_window"
            task_id = doc.llm_wiki_task_id
            if status == "failed":
                pass  # Missing artifacts/submission failures also have real terminal states.
            elif not task_id:
                return None
            else:
                written = await TaskCenterDocumentRepository(session).set_rag_task_id(
                    row["document_id"], execution_id=row["execution_id"], rag_task_id=task_id,
                )
                if not written:
                    return "waiting_dispatch"
        return await self._inspect_wiki_compile(row, status, error)

    async def _supervise_submission(self, row: Dict[str, Any]) -> str:
        """`state='submitting'`：补交，或者放弃。

        补交**用同一个 `task_id`**，这是整条路能重试的前提：任务中心的服务端 `init_task`
        不做幂等去重（不带 id 重发 = 新建第二条任务），但自带 id 时 id 就是幂等键 ——
        撞上已存在的 id 会回 1006，SDK 判成 `SubmissionDisposition.REUSED`（**当成功收工**），
        不会多建一条、也不会让调用方换 id 重来。
        """
        attempts = row["submit_attempts"]
        age = self._elapsed(row["created_at"])
        if attempts >= TC_SUBMIT_MAX_ATTEMPTS or age > TC_SUBMIT_DEADLINE_SEC:
            # 代价说清楚：这一份文档**没被任务中心接管成功**，交给既有对账收敛
            # （PARSING 由 patrol 兜、或用户重传）。不在这里反复重试到天荒地老 ——
            # 「任务提交不上」多半是配置问题（能力清单没声明 `business.task_center.v1`），
            # 那种错误重试一万次也是同一句话。
            await self._fail_document(
                row,
                f"任务中心任务提交失败（已尝试 {attempts} 次、历时 {int(age)} 秒）",
                job="任务提交失败",
            )
            return "submit_abandoned"

        if self._elapsed(row["updated_at"]) < TC_SUBMIT_RETRY_SEC:
            return "waiting_submit"

        try:
            # 一次读出两件事实（同一口径，见 `_doc_facts`）：补交落到**哪条编排**上、
            # 以及任务叫什么名字。
            facts = await self._doc_facts(row)
            if not facts["exists"]:
                # 文档查不到（软删也算）—— 这一行没有落点了。**不短路的话**：`kb_type` 是
                # `None`，喂给 `definition_for_kb_type` 会对域外值抛错，被下面那个 `except`
                # 吞成「补交失败」、每 `TC_SUBMIT_RETRY_SEC` 重来一次，一直空转到 30 分钟上限
                # 才 `_fail_document`（那时文档早没了，等于只 release）。与
                # `_inspect_rag_task` / `_inspect_compile` 的 `doc is None` 那两处同形：交还。
                async with get_db_session() as session:
                    await TaskCenterDocumentRepository(session).mark_released(
                        row["document_id"], error_message="文档已删除，接管中止"
                    )
                return "doc_gone"
            result = await submit_ontology_task(
                tenant_id=row["tenant_id"],
                space_id=row["space_id"],
                task_id=row["task_id"],
                document_id=row["document_id"],
                knowledge_base_id=row["knowledge_base_id"],
                # 补交必须落到**同一条**编排上：上传那一刻把它写进了文档行的粘性值，
                # 补交照着读回来。读 env 的话，开关在这期间翻转过就会「用 A 的粘性值
                # 收口、却把任务建到 B 的定义上」—— 两边各自都自洽，合起来是坏的。
                # `kb_type` 同理（它决定 lightrag 那两类库之外还有没有别的链）。
                kb_type=facts["kb_type"],
                backend=facts["backend"],
                # 任务名 = 文档名（用户定的）。任务中心那边只在**任务诞生那一刻**读它，
                # 而补交这条路正是「任务还没建上」的情形 —— 所以这里必须带上，否则
                # 走补交建出来的任务在列表里显示的是编排定义名。
                # 文档查不到时 `file_name` 是 None，任务中心照既定语义回落成定义名。
                task_name=facts["file_name"],
                operator_id=row["operator_id"],
            )
        except Exception as exc:  # noqa: BLE001
            # 连 `REUSED` 都没拿到就说明这次补交没成。留痕、计数，下一轮再来。
            await self._bump_attempt(row, f"补交任务失败：{exc}")
            return "submit_retry_failed"

        # 判据收口在 `submission_accepted`（与上传路径共用一处）—— `CREATED` 与
        # `REUSED` 都算成，判错方向在这里代价最大。
        if submission_accepted(result):
            async with get_db_session() as session:
                await TaskCenterDocumentRepository(session).mark_managed(
                    row["document_id"],
                    tenant_id=row["tenant_id"],
                    task_id=row["task_id"],
                )
            logger.info(
                "看门狗补交成功: doc=%s task=%s disposition=%s",
                row["document_id"], row["task_id"], result.disposition.value,
            )
            return "submit_recovered"

        await self._bump_attempt(row, result.message or "任务中心未受理")
        return "submit_retry_failed"

    async def _inspect_rag_task(self, row: Dict[str, Any], step_code: str) -> str:
        """`parse` / `ingest` 两步：问 atomic-rag 跑完没有。

        ⚠️ **必须把文档行一起取出来喂给 `_fetch_task_status`** —— 它按行上的粘性值
        （`extra_metadata.ingest_backend`）决定问哪个后端，那个参数没有默认值就是为了
        逼调用点显式表态（见它的 docstring）。
        """
        async with get_db_session() as session:
            doc = await KnowledgeDocumentRepository(session).get_by_id(
                row["document_id"], row["tenant_id"]
            )
            if doc is None:
                # 文档被删了（软删也算）。这一行没有落点，直接交还。
                await TaskCenterDocumentRepository(session).mark_released(
                    row["document_id"], error_message="文档已删除，接管中止"
                )
                return "doc_gone"
            status_info = await _fetch_task_status(
                row["tenant_id"], row["knowledge_base_id"], row["rag_task_id"],
                doc_id=row["document_id"], doc=doc,
            )

        if status_info is None:
            return "waiting_status_unknown"

        rag_status = (status_info.get("status") or status_info.get("state") or "").lower()
        if rag_status in ("processing", "pending", "queued", "running"):
            return "waiting_running"
        if rag_status == "completed":
            return await self._finish_step(row, step_code, status_info)
        if rag_status in ("failed", "cancelled", "canceled", "error"):
            reason = status_info.get("error_message") or status_info.get("error") or rag_status
            await self._fail_step(row, f"{step_code} 步骤下游任务失败：{reason}")
            return "step_failed"
        # `not_found` / 空 / 没见过的状态词：**绝不判死**，交硬超时。
        return "waiting_unknown_status"

    async def _inspect_compile(self, row: Dict[str, Any]) -> str:
        """`compile` 步：**按库类型**看两个完全不同的终态来源，都不看 atomic-rag 的任务号。

        | 库 | 看哪一列 | 谁把它推到终态 |
        | --- | --- | --- |
        | `lightrag`（Ontology） | `ontology_status` | 既有对账的 EXTRACTING 分支（`_handle_completed`，写 Neo4j 的唯一入口） |
        | `openkb`（Ontology Wiki） | `llm_wiki_compile_status` | 既有 30 秒巡检 `patrol_openkb_compile` |
        | `jonex_wiki`（Jonex Wiki） | `llm_wiki_compile_status`（**同一个列**） | 既有 30 秒巡检 `patrol_jonex_wiki_compile` |

        为什么两次都不看 atomic-rag 的任务号（第二个原因也是硬原因）：

        1. **收尾不在我们手里**。lightrag 那一步写 Neo4j 的唯一入口是既有对账的
           EXTRACTING 分支（`_retry_ontology_one` → `_handle_completed`）；两类 wiki 库
           的编译终态唯一回写者是各自的 30 秒巡检。我们只是等它们。
        2. **接管行上的那个下游任务号会过期**。对账在抽取失败时会**自己重抽**（最多
           `MAX_ONTOLOGY_RETRIES` 次），每次都换一个新的 `rag_task_id` 写回文档行 ——
           拿我们记的那个旧号去问，会在对账还在重抽时报「失败」，把一次正常的重抽
           判成终局。

        ⚠️ **两类 wiki 库的实际超时都比这里短**：各自的编译巡检默认 1800 秒（30 分钟）
        到点**直接判死**（`OPENKB_COMPILE_TIMEOUT_SEC` / `JONEX_WIKI_COMPILE_TIMEOUT_SEC`），
        而这一步的硬超时是 `TC_STEP_HARD_TIMEOUT_SEC`（21600）。也就是说实际有效超时是
        巡检那个 30 分钟，链上这个 6 小时走不到 —— 这是刻意的（保持既有产品行为）。
        """
        async with get_db_session() as session:
            doc = await KnowledgeDocumentRepository(session).get_by_id(
                row["document_id"], row["tenant_id"]
            )
            if doc is None:
                await TaskCenterDocumentRepository(session).mark_released(
                    row["document_id"], error_message="文档已删除，接管中止"
                )
                return "doc_gone"
            ont_status = (doc.ontology_status or "").lower()
            ont_error = getattr(doc, "ontology_error", None)
            kb_type = (getattr(doc, "extra_metadata", None) or {}).get("kb_type")
            wiki_status = (getattr(doc, "llm_wiki_compile_status", None) or "").lower()
            wiki_error = getattr(doc, "llm_wiki_compile_error", None)

        if not kb_type:
            # 老文档行上没有这份快照 —— 查权威源（与 `_doc_facts` 同一口径）。
            #
            # ⚠️ **这里必须用严格版（查不到回 `None`），不能用 `get_kb_type`**：那个函数
            # 查不到会回落 `lightrag`，而两类 wiki 库的 `ontology_status` 被上传路径写成
            # `READY` —— 猜成 lightrag 就会落进下面那条分支、把「编译刚发起」判成
            # 「编译成功」（任务中心显示这一步成了、文档标 compiled，而 Wiki 根本没有）。
            # 拿不到权威值就**不判**（见下面那个分支）。
            from .kb_type_service import get_kb_type_or_none

            kb_type = await get_kb_type_or_none(
                row["tenant_id"], row["knowledge_base_id"]
            )
        if not kb_type:
            # 两处都拿不到（文档行没快照 **且** 库表里查不到这个 KB —— 被删了 / 跨租户读）。
            # **不猜、也不判死**：猜错会把 wiki 判成假成功，判死会把一份可能好好的文档标坏。
            # 保持「等」—— 由这一步的硬超时（`TC_STEP_HARD_TIMEOUT_SEC`）兜底，
            # 期间库里那份记录被补回来/修好，下一轮就能正常判。
            logger.warning(
                "[TaskCenter] 判不出文档 %s 属于哪类库（文档行无 kb_type 快照、库表里查不到 "
                "KB %s），本步既不判成功也不判失败、等下一轮（任务 %s）",
                row["document_id"],
                row["knowledge_base_id"],
                row.get("task_id"),
            )
            return "waiting_kb_type_unknown"

        if kb_type in (_KB_TYPE_OPENKB, _KB_TYPE_JONEX_WIKI):
            # ⚠️ **两类 wiki 库都必须在这里分流**，漏了 jonex_wiki 会落到下面 lightrag 的
            # `ontology_status` 分支 —— 而 wiki 类文档的 `ontology_status` 被上传路径写成
            # `READY`（`document_service._upload_via_task_center`），于是**编译刚发起就被
            # 判「成功」**：任务中心显示这一步成了、文档标 compiled，而 Wiki 根本没生成。
            return await self._inspect_wiki_compile(row, wiki_status, wiki_error)

        if ont_status == OntologyStatus.READY.value:
            # 图已经写完（或本来就无本体内容）。这一步真成了。
            await self._finish_step(row, "compile", {})
            return "step_ok"
        if ont_status == OntologyStatus.FAILED.value:
            # ⚠️ **只上报失败、不动 `doc.status`** —— 这是既有对账的既定口径：本体失败
            # 只落 `ontology_status=FAILED` + `ontology_error`，文档本身仍是 `READY`
            # （切片已入库、检索可用，缺的只是图）。这里要是顺手把文档置 FAILED，就等于
            # 让一次「本体没抽出来」把一份能检索的文档整个标成坏的。
            await self._fail_step(
                row, _compile_failure_reason(ont_error),
                fail_document=False,
            )
            return "step_failed"
        # `pending` / `extracting` / 空：对账还在跑。等 —— 硬超时兜在外层。
        return "waiting_ontology"

    async def _inspect_wiki_compile(
        self, row: Dict[str, Any], wiki_status: str, wiki_error: Optional[str]
    ) -> str:
        """两类 wiki 库的 `compile` 步：看 `llm_wiki_compile_status`。

        终态由各自那条 30 秒巡检回写（openkb → `patrol_openkb_compile`、jonex_wiki →
        `patrol_jonex_wiki_compile`）—— 但**列是同一个**，所以这里两条链共用一段。
        """
        if wiki_status == "compiled":
            # Wiki 已经编译完（`compiled_at` 也写了）。这一步真成了。
            await self._finish_step(row, "compile", {})
            return "step_ok"
        if wiki_status == "failed":
            # ⚠️ **只上报失败、不动 `doc.status`** —— 与 lightrag 那条同一条理由：
            # wiki 类文档在解析收口时已经是 `READY`（产物落盘、可检索），而编译失败
            # 缺的只是 Wiki；把文档整个标成 FAILED 会把一份可用的文档打成坏的。
            await self._fail_step(
                row,
                f"Wiki 编译失败：{wiki_error or '详见文档的编译错误信息'}",
                fail_document=False,
            )
            return "step_failed"
        # 空 / `stale` / `compiling`：编译还没落终态。等 —— 判死由既有巡检负责
        # （它是编译终态的**唯一**回写者，见模块 docstring 那条纪律）。
        return "waiting_wiki_compile"

    # ========================================================
    # 处置
    # ========================================================

    async def _finish_step(
        self, row: Dict[str, Any], step_code: str, status_info: Dict[str, Any]
    ) -> str:
        """这一步成了：**先上报** → 落文档状态 → 记账（收口）。

        顺序不能反 —— 理由见下面那段注释，以及模块 docstring 的第四条纪律。
        **一次会话里改完**接管行与文档行（模型文件模块 docstring 的同一条纪律）。
        """
        document_id = row["document_id"]
        execution_id = row["execution_id"]
        is_last = step_code == "compile"

        # ── 先上报，**收口必须等它被持久受理** ──
        # 收口那次 UPDATE 会把 `execution_id` / `rag_task_id` / `step_started_at` 一起
        # 清掉（`is_last` 时还把 `state` 置 `released`）—— 那之后这一行**再也进不了
        # 看门狗视野**（不在 `ACTIVE_STATES` 里）。而 `report_terminal` **不抛异常**：
        # 它重试 3 次之后把 `TerminalSubmitResult` 交回来，没判 `DURABLY_ACCEPTED` 就只是
        # 「我发过」、不是「对方收到了」。先收口的话，这一次真实成功就**永久丢失**：
        # 管理方那侧子任务一直挂在 `running`，等满编排超时被判死，`_aggregate` 把整条任务
        # 收成 failed —— 而文档其实已经 READY，没有任何人会补这次上报。
        # 反过来（先上报）最坏只是**重复上报**：收件箱 `subtask_reports` 没有唯一约束，
        # 重复到达的那条由后台消化的「已是终态」闸吸收。所以这个方向是自愈的 ——
        # 这一轮没报上去，下一轮看门狗看到的还是同一个 `rag_task_id`，再走一遍这里。
        if not await self._report_success(row, document_id):
            # **没收口、文档状态一个字也没动**，下一轮拿同一行再来。
            logger.warning(
                "看门狗上报未被受理，本步不收口、下一轮重试: doc=%s step=%s execution=%s",
                document_id, step_code, execution_id,
            )
            return "report_not_accepted"

        async with get_db_session() as session:
            tc_repo = TaskCenterDocumentRepository(session)
            doc_repo = KnowledgeDocumentRepository(session)

            doc = await doc_repo.get_by_id(document_id, row["tenant_id"])

            # ── 哪一步把文档推到 `READY`：**这一步的产物已经就位** ──
            # 对 lightrag 是「切片进了 LightRAG 的那一刻」，对 openkb / jonex_wiki 是
            # 「解析产物落盘、可以编译了」—— 判据落在同一个步骤代号上（见模块 docstring
            # 那张表）：
            #   · rag 链上传：`full` 解析 + 多模态 + 推切片，一趟做完；
            #   · rag 链重新解析：`reparse_strict` 解析 + 删旧切片 + 推新切片；
            #   · **openkb 链**：`parse_only` 只落 `content.md`（不推切片、不进 LightRAG）。
            #     置 `READY` 在这里的含义是「**解析产物就绪、可以编译了**」—— openkb 的
            #     编译相位另由 `llm_wiki_compile_status` 表达（前端 `docPhase` 就是这么读的）。
            # `ingest`（`push_only`）也推切片 —— 现役编排里已经没有它，留着是让**改版前
            # 遗留的在途任务**照样收得了口（它们的子任务固化在库里）。
            # ⚠️ 判据**不看 `tc_intent`**（它只归运行方选引擎档位用），也不能按
            # `current_step` 的字面量猜：判错就是把搜索在一个空库上放行，或者让 `ready`
            # 永远不来 —— 这一步的收口只有这里一处。
            doc_ready_after_step = step_code in ("parse", "ingest")
            if doc_ready_after_step:
                # 对外**此刻就可检索**（本体还没抽，前端显示「待抽取」）。这正是老链路的
                # READY 口径，不新造状态（§3.7 砍掉 `DocStatus.PARSED` 的同一条理由：
                # 任务中心链原样映射到既有的 PARSING → READY，不引入新状态去喂既有守卫）。
                if doc is not None:
                    # 下游的产出里带了推入的切片 id 就顺手回写。**三个候选字段名都试**
                    # （历史接口名不统一）：
                    #   · atomic-rag 的状态接口给的是 **`lightrag_doc_ids`**
                    #     （`atomic-rag-server-v2.py::handle_get_task_status`）—— 注意它
                    #     既没有 `doc_ids` 也没有 `document_ids`；漏了这一个，这一列会
                    #     静默地永远是空的（引擎在 `reparse_strict` 里会**自己按
                    #     `document_id` 现查**旧切片，所以一直没症状 —— `_snapshot_old_doc_ids`），
                    #     但这一列就名不副实了，将来靠它的逻辑（删除、排障、优化）会踩空。
                    # 没有也不必补：删除侧有 `find_rag_doc_ids` 兜底，且注释明说这一列
                    # 「可能为空」（`document_service.py` 的删除段）。
                    doc_ids = (
                        status_info.get("lightrag_doc_ids")
                        or status_info.get("doc_ids")
                        or status_info.get("document_ids")
                    )
                    await doc_repo.set_status(
                        doc, DocStatus.READY, rag_doc_ids=doc_ids or None,
                    )

            await tc_repo.mark_step_handled(
                document_id, execution_id=execution_id, release=is_last,
            )

        logger.info(
            "看门狗收口成功: doc=%s step=%s execution=%s%s",
            document_id, step_code, execution_id, "（接管完成，已交还）" if is_last else "",
        )
        return "step_ok"

    async def _fail_step(
        self, row: Dict[str, Any], reason: str, *, fail_document: bool = True
    ) -> None:
        """这一步没成：上报失败 → 记账 → 交还接管。

        `fail_document=False` 给 `compile` 步用（本体失败不动文档状态，见 `_inspect_compile`）。
        """
        document_id = row["document_id"]
        try:
            await self._report_failure(row, reason)
        except Exception:  # noqa: BLE001
            # 上报不上去（任务中心那侧连不上、或已被判终态）**不能拦住交还** ——
            # 交还是用户的解封点，比「让任务中心知道」重要得多。上报本身在 SDK 里
            # 已经重试过 3 次，这里的失败是真的没辙。
            logger.warning(
                "看门狗上报失败不成功（仍继续交还）: doc=%s execution=%s",
                document_id, row["execution_id"],
            )

        async with get_db_session() as session:
            if fail_document:
                doc = await KnowledgeDocumentRepository(session).get_by_id(
                    document_id, row["tenant_id"]
                )
                if doc is not None:
                    await KnowledgeDocumentRepository(session).set_status(
                        doc, DocStatus.FAILED, error_message=reason[:2000],
                    )
            await TaskCenterDocumentRepository(session).mark_released(
                document_id, error_message=reason[:2000],
            )
        logger.warning(
            "看门狗判死: doc=%s step=%s execution=%s reason=%s",
            document_id, row.get("current_step"), row.get("execution_id"), reason,
        )

    async def _fail_document(self, row: Dict[str, Any], reason: str, *, job: str) -> None:
        """还没进链就失败的处置（目前只有「提交不上」一档，没有 execution 可上报）。"""
        async with get_db_session() as session:
            doc = await KnowledgeDocumentRepository(session).get_by_id(
                row["document_id"], row["tenant_id"]
            )
            if doc is not None:
                await KnowledgeDocumentRepository(session).set_status(
                    doc, DocStatus.FAILED, error_message=reason[:2000],
                )
            await TaskCenterDocumentRepository(session).mark_released(
                row["document_id"], error_message=f"{job}：{reason}"[:2000],
            )
        logger.warning("看门狗放弃: doc=%s task=%s reason=%s", row["document_id"], row["task_id"], reason)

    async def _abandon(self, row: Dict[str, Any], reason: str) -> None:
        """「有 execution_id 但上一步早已收口、下一步迟迟不来」时的交还。

        ⚠️ **交还前先把在途任务号交给文档行**（同 `task_stopped` 那处，共用一份实现）：
        交还之后这一行就出看门狗视野、这份文档回到既有对账手里，而任务中心链的文档行上
        **没有** `rag_task_id` —— 不交出去的话，对账会按「没有任务号」连判 3 拍把它判死
        （「RAG 任务确认丢失…请重新上传」），跑过 SOFT 还会再插一份解析、产出重复向量。
        理由与两条边界见 `hand_over_in_flight_task_id` 的 docstring。

        ⚠️ **交还前还要替在途那一步报一句失败**（2026-09-30 补，与 `task_stopped` 逐字同一条
        理由）：交还之后这一行**也出了管理方的结算视野** —— 不报的话，任务中心那侧那一步
        一直挂在 `running`，占着 `count_running_by_step_code` 的**全平台共用名额**
        （`parse` 一共 10 个，跨库跨租户），最长到编排超时（预置 25200 秒 ≈ 7 小时）才被
        超时巡检收掉；这期间用户想「重新解析 / 重新编译」还会被管理方拒（那一步没结束）。
        走到这一支的典型成因正是「`task_stopped` 那条通知**丢了**」（单发、不重发、失败只
        记 warning）—— 两条路必须给同一个结局，否则**通知丢没丢**决定了平台里留不留僵尸。

        ⚠️ **上报绝不能拦住交还**（与 `_fail_step` 同一条纪律）：失败只记 warning。
        """
        if row.get("execution_id"):
            try:
                await self._report_failure(row, reason)
            except Exception as exc:  # noqa: BLE001 —— 见上：上报失败不影响交还
                logger.warning(
                    "看门狗交还前补上报失败（不影响交还）: doc=%s task=%s: %s",
                    row["document_id"], row["task_id"], exc,
                )
        async with get_db_session() as session:
            await hand_over_in_flight_task_id(
                session,
                document_id=row["document_id"],
                tenant_id=row["tenant_id"],
                rag_task_id=row.get("rag_task_id"),
                step_code=row.get("current_step"),
            )
            await TaskCenterDocumentRepository(session).mark_released(
                row["document_id"], error_message=reason[:2000],
            )
        logger.warning("看门狗交还: doc=%s task=%s reason=%s", row["document_id"], row["task_id"], reason)

    async def _bump_attempt(self, row: Dict[str, Any], error_message: str) -> None:
        async with get_db_session() as session:
            await TaskCenterDocumentRepository(session).bump_submit_attempt(
                row["document_id"], error_message,
            )

    # ========================================================
    # 上报
    # ========================================================

    def _carrier(self, row: Dict[str, Any]) -> ExecutionContext:
        """上报凭据。**显式传**，不从 `ContextVar` 取 —— 看门狗跑在后台循环里，
        那一帧根本没有执行上下文（`report_terminal` 的 docstring 讲的就是这件事）。"""
        return ExecutionContext(
            tenant_id=row["tenant_id"],
            space_id=row["space_id"],
            task_id=row["task_id"],
            step_code=row["current_step"] or "",
            execution_id=row["execution_id"] or "",
        )

    async def _report_success(self, row: Dict[str, Any], document_id: str) -> bool:
        """上报成功。返回值 = 任务中心**持久受理**了没有（`DURABLY_ACCEPTED`）。

        ⚠️ 返回值必须被调用方用起来：`report_terminal` **不抛异常** —— 它重试 3 次之后
        把 `TerminalSubmitResult` 交回来，其余两档（`RETRYABLE_UNKNOWN` /
        `PERMANENT_REJECTED`）都只意味着「发过」。把「发过」当成「对方收到了」而放心
        收口，就是 `_finish_step` 里那条永久丢失的来路。

        `outputs` **必须带 `document_id`**：下游两步的 `bind_inputs` 写的是
        `@<上一步>.outputs.document_id`（`008_task_center.sql` 的编排定义段）。漏了它，
        下一步拿到空值、最后一步查不到文档。

        `compile` 步按定义它的 `output_schema` 是 `{}`（那两步才是
        `{"document_id": ...}`）—— 报空对象是**对的**，不是漏传：它是链路终点，没有下游。
        """
        from jonex_core.task_runtime import get_task_runtime

        outputs = {} if row["current_step"] == "compile" else {"document_id": document_id}
        result = await get_task_runtime().report_terminal(
            self._carrier(row),
            TerminalReport.success(
                outputs, output_refs=await self._artifact_refs(row, document_id)
            ),
        )
        return result.disposition is TerminalSubmitDisposition.DURABLY_ACCEPTED

    async def _artifact_refs(self, row: Dict[str, Any], document_id: str) -> list:
        """产出「**去哪儿看**」的引用（§4.2 ① 的 `output_refs`）。

        **由运行方填**：只有我们知道自己的产物是什么、看它要哪几个参数。管理方只搬运
        （落到 `subtasks.output_refs`、控制台读接口原样回给前端）。

        ⚠️ **引用里只有 `kind` + `params`**（2026-10-09 用户拍板）—— **不带地址**：
        「这个 kind 该调哪个接口」是**前端**那张映射的事，运行方碰不到 url，
        所以填不出外站地址 / `javascript:` / 目录穿越 / `DELETE` 那一类东西（注入面归零）。
        ⚠️ `kind` **带运行方前缀**（`_REF_KIND_PREFIX`）—— 任务中心是**多运行方**的，
        裸 `chunks` 这种名字迟早和别人撞（谁都在切块）；前缀就是本运行方在
        `runner_definitions` 里的 `code`。
        判据在协议层 `check_output_refs`（`jonex_core/task_protocol/models.py`），
        SDK 与服务端共用同一份；**每个 kind 要哪些 params 也写死在那张表里**
        （`OUTPUT_REF_PARAMS`）。

        `params` 给的是**我们这边接口要的字段** —— 前端拿它去调**我们**的接口看详情，
        **权限也归这边判**（谁能看这份文档谁才能看它的产物）—— 所以那条给前端**不判权**的
        任务列表接口里只放引用、不放正文。

        三种终态各一处：
          · `parse` / 遗留 `ingest` → 切片列表（解析的产物就是切片）；
          · `compile` 且是 Ontology 库（lightrag）→ 本体编译结果；
          · `compile` 且是 wiki 类库（openkb / jonex_wiki）→ Wiki 页面树（它们没有本体）。

        ⚠️ 两类 wiki 库**共用同一个 kind**（`knowledge_base.wiki`）—— **不开新 kind**：
        这个 kind 只回答「去哪儿看」，而 `/parse-results/wiki-contents` 那个端点内部已按
        `kb_type` 分派（`parse_result_service.get_wiki_contents`），前端不需要、也无法从
        kind 上区分这两类库。开新 kind 的代价是协议层那张表 + 前端映射**必须一起改**。
        """
        kb = row.get("knowledge_base_id") or ""
        step = row.get("current_step") or ""
        params = {"document_id": document_id, "knowledge_base_id": kb}
        if step in ("parse", "ingest"):
            return [{"kind": f"{_REF_KIND_PREFIX}.chunks", "params": params}]
        if step == "compile":
            # ⚠️ 这条「先读接管行上的粘性值」的快路径**当前恒不命中**：`_load_active`
            # 造出来的 dict 里没有 `extra_metadata` 这一项，`task_center_documents` 表上
            # 也没有这一列 —— 所以每次都落到下面那次 `get_kb_type` 查询上。
            # 留着它是**防御性**的（将来 `_load_active` 真把库类型带过来就自动生效），
            # 代价只是每次 compile 收口多一次查询；要拿掉它得连着改测试里那条
            # 「走粘性值、不碰库」的用例，别只改一半。
            kb_type = (row.get("extra_metadata") or {}).get("kb_type")
            if not kb_type:
                from .kb_type_service import get_kb_type

                kb_type = await get_kb_type(row["tenant_id"], kb)
            if kb_type in (_KB_TYPE_OPENKB, _KB_TYPE_JONEX_WIKI):
                return [{"kind": f"{_REF_KIND_PREFIX}.wiki", "params": params}]
            return [{"kind": f"{_REF_KIND_PREFIX}.ontology", "params": params}]
        # 其它步骤（认不出的）不给入口 —— 宁可不显示，也不给一个点进去 404 的按钮。
        return []

    async def _report_failure(self, row: Dict[str, Any], reason: str) -> None:
        from jonex_core.task_runtime import get_task_runtime

        await get_task_runtime().report_terminal(
            self._carrier(row), TerminalReport.failed(reason[:2000]),
        )

    # ========================================================
    # 杂项
    # ========================================================

    @staticmethod
    def _elapsed(since: Optional[datetime]) -> float:
        """从 `since` 到现在的秒数。`None` 当 0 秒（= 刚开始），不判超时。

        两侧都是 Python `datetime.now()` 写出来的 naive 本地时间（`TimestampMixin`
        与 `begin_step` 同口径）—— **不能混用 PG 的 `func.now()`**：那是 timestamptz，
        落进 `TIMESTAMP WITHOUT TIME ZONE` 时按会话时区换算，会话时区与进程本地时区
        不一致时整条链的计时会偏几小时，把在途行全判死。
        """
        if since is None:
            return 0.0
        return (datetime.now() - since).total_seconds()


__all__ = ["ONTOLOGY_DEFINITION_ID", "TaskWatchService"]
