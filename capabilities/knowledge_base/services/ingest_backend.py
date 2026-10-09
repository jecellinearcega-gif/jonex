# [jonex] 新增文件 — 入库后端选择（spec §5.8 / D9）
"""入库后端选择（spec §5.8 / D9）。

**提交时决定、写进文档行、查询时读回来** —— 三处调用点共用本模块，
且**只有提交那一处读 env**。

为什么查询侧不读 env：一个按某个后端提交、还没跑完的文档，在开关翻转后必须继续
从**当初那个**后端取状态。查询侧各读一次环境变量的写法，会在翻转那一刻把在途文档
永远卡在 PARSING —— 一个不报错、只是永远不完成的失败形态。

当前注册的后端只有一条：`rag`（任务中心链：atomic-rag 解析 + 推切片）。

判据从高到低两层（都在当场判，不做启动期校验 —— env 与库值都是运行时可变的）：

    库级派单值（`knowledge_info.ingest_backend`，非 NULL）→ 服务端开关 `KB_INGEST_BACKEND`（缺省 `rag`）

库值同样是**提交时读、写进文档行**（粘性），查询侧只读粘性；改库设置只影响下一次提交。
"""

from __future__ import annotations

import os
from typing import Any

from jonex_core.common.exceptions import InternalError

VALID_BACKENDS = ("rag",)

# 两个消费点各自的「任务还活着」白名单：
#   · `patrol_parsing_timeout`：探活查到的状态不在 `RAG_ALIVE_STATUSES` ⇒ `alive=False`
#     ⇒ 到 HARD 上限把**正在跑**的任务判死成 FAILED；
#   · `_reconcile_one`：不在 `RAG_RECONCILE_ALIVE_STATUSES` ⇒ 不清零判死计数（连续
#     三拍后同样落 FAILED）。
# 两份不是子集关系（`completed` 只在后者、`running`/`extracting` 只在前者），
# 所以这里不合并 —— 合并会在其中一处悄悄放宽 / 收紧判死口径。
RAG_ALIVE_STATUSES = frozenset({"pending", "queued", "processing", "running", "extracting"})
RAG_RECONCILE_ALIVE_STATUSES = frozenset({"created", "queued", "processing", "completed"})


class IngestBackendConfigError(InternalError, ValueError):
    """`KB_INGEST_BACKEND` 取值非法 —— **服务端配置**错误，不是调用方参数错误。

    同时继承 `ValueError`：配置类错误用 `except ValueError` 抓是运维/脚本的自然
    写法（也是 brief 用例钉住的形状）；而本项目**不许裸 `ValueError`** 逃出异常
    体系（`InternalError` 侧负责这条）。两个契约都满足，不是二选一。
    """


def current_backend() -> str:
    """读服务端开关并校验。**只允许在提交时调用**（见模块 docstring：查询侧读 env
    会把在途文档卡死）。缺省 `rag`；非法值响亮抛错，不静默回落。"""
    value = os.environ.get("KB_INGEST_BACKEND", "").strip() or "rag"
    if value not in VALID_BACKENDS:
        raise IngestBackendConfigError(
            f"KB_INGEST_BACKEND 取值非法：{value!r}（可选 {VALID_BACKENDS}）"
        )
    return value


def resolve_backend(kb_backend: str | None = None) -> str:
    """**提交时**定后端：库级派单值（非 NULL）优先，否则读服务端开关。

    非 NULL 的库值**双向覆盖**服务端开关（`NULL` = 跟随开关 —— 存量库全是
    NULL，行为与从前逐字一致）；域外值在这条路上照抛 `IngestBackendConfigError`
    （响亮失败，不静默回落）。
    """
    if kb_backend is not None:
        if kb_backend not in VALID_BACKENDS:
            raise IngestBackendConfigError(
                f"知识库行的 ingest_backend 取值非法：{kb_backend!r}（可选 {VALID_BACKENDS}）"
            )
        return kb_backend
    return current_backend()


def backend_for_document(doc: Any) -> str:
    """**查询时**定后端：读文档行上的**粘性**值；缺失 / 域外值一律回落 `rag`。

    行上的值只可能由 `resolve_backend` 写入，出现别的值是数据脏，不是「切到别的
    后端」的信号。当前只有 `rag` 一条链，所以结果恒为 `rag` —— 但读粘性值是既定
    语义：文档按哪个后端提交，就按哪个后端收尾（见模块 docstring）。
    """
    metadata = getattr(doc, "extra_metadata", None) or {}
    value = metadata.get("ingest_backend")
    return value if value in VALID_BACKENDS else "rag"
