"""Rerank 客户端：经 llm-gateway 统一出口，带计量链路头。

失败返回 None，由调用方兜底降级（不阻断主链路）。

⚠️ 配置在模块级读取（仿 ontology_embedding.py 现有模式）：运行时改这些环境变量
   不生效，需重启服务。灰度调参后务必重启相关服务。
"""

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_BASE = os.getenv("RERANK_BINDING_HOST", "http://llm-gateway:8787/v1")
_KEY = os.getenv("RERANK_BINDING_API_KEY", "gw_lightrag")
# 模型名唯一来源是环境变量，不留字面量兜底（上游改名后字面量会被静默捡回来）。
# 未配置时由 rerank() 走「降级返回 None」的老路，并发告警 —— 本模块的契约是
# 「失败返回 None，由调用方兜底降级」，所以这里不抛错，但也绝不拿空名字去请求上游。
_MODEL = os.getenv("RERANK_MODEL", "").strip()
_TIMEOUT = float(os.getenv("RERANK_CLIENT_TIMEOUT", "30"))


async def rerank(
    query: str,
    documents: list[str],
    *,
    top_n: Optional[int] = None,
    tenant_id: Optional[str] = None,
    kb_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    user_id: Optional[str] = None,
    doc_id: Optional[str] = None,
) -> Optional[list[dict]]:
    """对文档按与 query 的相关性打分。

    Returns:
        [{"index": i, "relevance_score": s}, ...]（按上游返回，未必排序）；失败返回 None。
    """
    if not query or not documents:
        return None
    if not _MODEL:
        # 降级而非抛错：本模块契约就是「失败返回 None，不阻断主链路」。
        # 但不能静默 —— 没有告警的话，检索质量下滑会被当成「模型效果不好」。
        logger.warning(
            "RERANK_MODEL 未配置，rerank 降级跳过（调用方按无 rerank 继续）；"
            "请设置环境变量 RERANK_MODEL"
        )
        return None
    headers = {
        "Authorization": f"Bearer {_KEY}",
        "X-Jonex-Tenant-Id": tenant_id or "unknown",
        "X-Jonex-Scene": "ontology_rerank",
    }
    if kb_id:
        headers["X-Jonex-Kb-Id"] = kb_id
    if trace_id:
        headers["X-Jonex-Trace-Id"] = trace_id
    if user_id:
        headers["X-Jonex-User-Id"] = user_id
    if doc_id:
        headers["X-Jonex-Doc-Id"] = doc_id
    payload: dict = {"model": _MODEL, "query": query, "documents": documents}
    if top_n is not None:
        payload["top_n"] = top_n
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as cli:
            resp = await cli.post(f"{_BASE}/rerank", json=payload, headers=headers)
            resp.raise_for_status()
            results = resp.json().get("results") or []
            if not results:
                # 网关对「模型名不认识」这类错误**不一定**报非 2xx：实测
                # `RERANK_MODEL` 与网关实际加载的模型不一致时返回
                # `200 + {"results": []}`（无异常、无日志）。调用方拿到空列表
                # 会静默降级——统一分全池退化成 `order_decay`、`score_kind`
                # 无声失真，排查时链路上一行日志都没有。这里必须留痕。
                logger.warning(
                    "[rerank] 网关返回空 results（model=%s docs=%d base=%s）——"
                    "调用方将兜底降级；请核对 RERANK_MODEL 是否为网关已加载的模型",
                    _MODEL, len(documents), _BASE,
                )
            return results
    except Exception as e:
        logger.warning("[rerank] 调用失败，调用方将兜底降级: %s", e)
        return None


__all__ = ["rerank"]
