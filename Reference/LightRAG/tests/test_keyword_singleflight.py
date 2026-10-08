#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""[jonex] 关键词抽取 single-flight —— 并发合并语义回归。

对应改动：`lightrag/operate.py` 的 `_keyword_singleflight` 与
`extract_keywords_only` 第 3~5 步。

背景：`extract_keywords_only` 的缓存 key（`args_hash`）只含
(mode, text, language)，**不含 workspace**，而 `hashing_kv` 是 per-workspace 的
PGKVStorage 实例（`RAG_WORKSPACE_ISOLATION=true`）→ 同一 query 在 N 个 KB 上
并发时，N 个 workspace 同时 miss、各抽一次 LLM（实测 5 KB = 5 次、单次 3~9s，
是单查询耗时的大头）。这层按 `args_hash` 在进程内合并并发。

本文件锁四条契约：

1. **同 key 并发只跑一次 factory**，且所有等待者拿到同一结果 —— 核心收益；
2. **不同 key 互不干扰** —— 不同 query 不得被错误合并；
3. **失败一致放散** —— factory 异常对所有等待者可见，不得部分静默；
4. **取消不扩散** —— 单个等待者被取消时不牵连共享任务与其他等待者
   （`asyncio.shield` 语义），否则一个 KB 超时会把其余 KB 一起带走。

为什么是独立脚本而非 pytest：与 `test_priority_limiter_context.py` 同因 ——
vendored LightRAG 模块加载期依赖重，本仓用 `asyncio.run` + 断言。

容器内运行：
    docker exec jonex-lightrag python /app/tests/test_keyword_singleflight.py
或本机（需 import lightrag 可用）：
    python Reference/LightRAG/tests/test_keyword_singleflight.py
全部通过打印 "ALL PASSED"，否则抛 AssertionError。
"""

import asyncio

from lightrag.operate import _KEYWORD_INFLIGHT, _keyword_singleflight


async def test_same_key_runs_factory_once():
    """契约 1：同 key 并发 5 次 → factory 只跑 1 次，且 5 个等待者结果一致。"""
    calls = []

    async def factory():
        calls.append(1)
        await asyncio.sleep(0.05)  # 让并发窗口真实存在
        return (["hl"], ["ll"])

    results = await asyncio.gather(
        *[_keyword_singleflight("k_same", factory) for _ in range(5)]
    )
    assert len(calls) == 1, f"factory 应只跑 1 次，实跑 {len(calls)} 次"
    assert results == [(["hl"], ["ll"])] * 5, f"等待者结果不一致: {results}"
    assert "k_same" not in _KEYWORD_INFLIGHT, "完成后应清理 inflight 记录"


async def test_different_keys_do_not_merge():
    """契约 2：不同 key 各自跑一次 —— 不同 query 不得被错误合并。"""
    calls = []

    def make(tag):
        async def _f():
            calls.append(tag)
            await asyncio.sleep(0.02)
            return ([tag], [])
        return _f

    a, b = await asyncio.gather(
        _keyword_singleflight("k_a", make("A")),
        _keyword_singleflight("k_b", make("B")),
    )
    assert sorted(calls) == ["A", "B"], f"两个 key 应各跑一次，实为 {calls}"
    assert a == (["A"], []) and b == (["B"], []), (a, b)


async def test_exception_propagates_to_all_waiters():
    """契约 3：factory 抛异常 → 所有等待者都收到（一致降级、不部分静默）。"""

    async def factory():
        await asyncio.sleep(0.02)
        raise RuntimeError("LLM down")

    results = await asyncio.gather(
        *[_keyword_singleflight("k_err", factory) for _ in range(3)],
        return_exceptions=True,
    )
    assert all(isinstance(r, RuntimeError) for r in results), results
    assert all(str(r) == "LLM down" for r in results), results
    assert "k_err" not in _KEYWORD_INFLIGHT, "异常后也应清理 inflight 记录"


async def test_waiter_cancel_does_not_kill_shared_task():
    """契约 4：取消一个等待者 → 共享任务与其他等待者不受影响（shield 语义）。"""
    done = []

    async def factory():
        await asyncio.sleep(0.1)
        done.append(1)
        return (["ok"], [])

    t1 = asyncio.ensure_future(_keyword_singleflight("k_cancel", factory))
    t2 = asyncio.ensure_future(_keyword_singleflight("k_cancel", factory))
    await asyncio.sleep(0.02)  # 确保两者都已进入 inflight
    t1.cancel()

    r1 = await asyncio.gather(t1, return_exceptions=True)
    assert isinstance(r1[0], asyncio.CancelledError), (
        f"被取消的等待者应收到 CancelledError，实为 {r1[0]!r}"
    )

    r2 = await t2
    assert r2 == (["ok"], []), f"另一个等待者仍应拿到结果，实为 {r2!r}"
    assert done == [1], "共享任务不应被等待者的取消牵连"


async def test_inflight_table_drained():
    """收尾：全部用例跑完后 inflight 表不得残留（防跨请求泄漏）。"""
    assert _KEYWORD_INFLIGHT == {}, f"inflight 表残留: {_KEYWORD_INFLIGHT}"


async def main():
    await test_same_key_runs_factory_once()
    await test_different_keys_do_not_merge()
    await test_exception_propagates_to_all_waiters()
    await test_waiter_cancel_does_not_kill_shared_task()
    await test_inflight_table_drained()
    print("ALL PASSED")


if __name__ == "__main__":
    asyncio.run(main())
