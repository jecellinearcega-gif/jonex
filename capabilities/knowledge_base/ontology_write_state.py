# -*- coding: utf-8 -*-
"""[jonex] 本体写图状态键的单一事实来源。

事故（2026-09-16）：ontology_write_retries 只在「写图成功」一处 pop，
落 FAILED 的路径漏清，导致计数跨轮累积——ONTOLOGY_WRITE_RETRIES=5 从
第二轮起重抽起每轮只剩 1 次机会（现场：ontology_error「重写 6 次达上限」
→「重写 8 次达上限」）。

根因是「每加一个写图状态键，就要在每个清理点各补一次 pop」，人一定会漏。
故把所有写图状态键集中登记在 ONTOLOGY_WRITE_STATE_KEYS，清理一律走
clear_ontology_write_state()，新增状态只需登记一次。

放在包根而非 services/：repository 层（reset_ontology_for_kb）也要用这份
清单，而它 import services.reconciliation_service 会构成循环。
"""

ONTOLOGY_WRITE_STATE_KEYS: tuple[str, ...] = (
    "ontology_write_retries",
    "ontology_write_transient_n",
    "ontology_write_backoff_until",
    "ontology_auto_retry_disabled",
)


def clear_ontology_write_state(fm: dict | None) -> dict:
    """清空全部写图状态键，返回**新 dict**（不改入参）。

    调用方用它整体赋给 ``fresh.extra_metadata``：in-place 改 dict 不会被
    SQLAlchemy 侦测为 JSONB 变更，必须整体替换。
    """
    out = dict(fm or {})
    for k in ONTOLOGY_WRITE_STATE_KEYS:
        out.pop(k, None)
    return out
