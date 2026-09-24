# -*- coding: utf-8 -*-
"""[jonex] P0-2 崩溃恢复护栏单测。

线上事故（2026-09-13）：financial_metric.md（17MB / 46290 chunk）本体抽取把
宿主内存吃到 23.7GB，被内核 oom-killer 杀在**落盘前**——进程死了，磁盘上
``ontology_status`` 恒为 ``extracting``，``timeline.ontology_extract.ended_at``
实测为 None。任务管理器启动时的恢复扫描把「extracting」一律判为「卡住」并
重置重跑，于是「跑 4.6 小时 → 被杀 → 从头重跑」可以无限循环。

``recovery_attempts`` 是持久化在 TaskInfo 上的计数护栏：超限即落终态，把
无限循环收敛成明确失败。本测试锁住三件事：

1. 计数自增后**必须落盘**（只在内存计数的话每次重启归零，护栏等于没有）
2. 超限分支要**同时**处理两个条件——``ontology_status`` 落 ``failed`` 且
   ``status`` 落终态、不再入队（只改前者的话，恢复逻辑第二个
   ``if task.status not in TERMINAL_STATES`` 仍会把任务重新塞回队列）
3. 未超限时行为与旧版一致（重置 pending + 重新入队）
"""
import asyncio
import contextlib

from unittest import mock

from raganything.service.models import TaskInfo, TaskStatus
from raganything.service.task_manager import TaskManager


def _make_task(**overrides) -> TaskInfo:
    base = dict(
        task_id="task-1",
        tenant_id="tenant_jonex_demo",
        name="financial_metric.md",
        file_path="/tmp/financial_metric.md",
        kb_id="kb1",
        document_id="doc-1",
        ontology_status="extracting",
        status=TaskStatus.PROCESSING,
    )
    base.update(overrides)
    return TaskInfo(**base)


async def _boot(tm: TaskManager, tasks: dict) -> mock.MagicMock:
    """跑一次 start() 的恢复扫描，返回记录调用次数的 repo mock。

    worker_count=0 避免真的起 worker；cleanup_loop 起来后立刻取消。
    """
    repo = mock.MagicMock()
    repo.load_all.return_value = tasks
    tm._repo = repo
    await tm.start()
    if tm._cleanup_task is not None:
        tm._cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await tm._cleanup_task
    return repo


def _saved_attempts(repo: mock.MagicMock) -> list:
    """从 save 调用里取出各次落盘的 recovery_attempts 序列。"""
    return [c.args[0].recovery_attempts for c in repo.save.call_args_list]


def _drain(tm: TaskManager) -> list:
    out = []
    while not tm._queue.empty():
        out.append(tm._queue.get_nowait())
    return out


# ── 护栏计数 ───────────────────────────────────────────────────────


async def test_attempts_incremented_and_persisted(monkeypatch):
    """未超限：计数 0→1，且**无条件先落盘**（不等分支）。"""
    monkeypatch.setenv("ONTOLOGY_RECOVERY_MAX_ATTEMPTS", "2")
    task = _make_task(recovery_attempts=0)
    tm = TaskManager(worker_count=0)
    repo = await _boot(tm, {task.task_id: task})

    assert task.recovery_attempts == 1
    assert 1 in _saved_attempts(repo), "自增后必须落盘"


async def test_attempts_persisted_even_when_not_requeued(monkeypatch):
    """status 已是终态、不会被重新入队时，计数仍必须落盘。

    这是原先的漏洞：save 只在「重新入队」分支里调用，磁盘上 status 为终态
    而 ontology_status 仍为 extracting 的任务，每次重启都从同一个计数开始，
    护栏永远不前进。
    """
    monkeypatch.setenv("ONTOLOGY_RECOVERY_MAX_ATTEMPTS", "2")
    task = _make_task(recovery_attempts=0, status=TaskStatus.FAILED)
    tm = TaskManager(worker_count=0)
    repo = await _boot(tm, {task.task_id: task})

    assert task.recovery_attempts == 1
    assert 1 in _saved_attempts(repo), "不入队的分支也必须落盘计数"
    assert _drain(tm) == [], "终态任务不得重新入队"


async def test_default_max_attempts_is_2(monkeypatch):
    """未配置时缺省 2：容忍偶发崩溃，但两次之后停止自动重跑。"""
    monkeypatch.delenv("ONTOLOGY_RECOVERY_MAX_ATTEMPTS", raising=False)
    task = _make_task(recovery_attempts=1)
    tm = TaskManager(worker_count=0)
    await _boot(tm, {task.task_id: task})

    assert task.ontology_status == "pending", "1+1=2 未超上限 2，应重置重跑"


async def test_invalid_env_falls_back_to_2(monkeypatch):
    """非法配置值回落缺省 2，而不是抛异常把整个恢复扫描打断。"""
    monkeypatch.setenv("ONTOLOGY_RECOVERY_MAX_ATTEMPTS", "not-a-number")
    task = _make_task(recovery_attempts=1)
    tm = TaskManager(worker_count=0)
    await _boot(tm, {task.task_id: task})

    assert task.ontology_status == "pending"


# ── 未超限：与旧行为一致 ───────────────────────────────────────────


async def test_under_limit_resets_and_requeues(monkeypatch):
    """未超限：ontology_status 回 pending，任务重新入队。"""
    monkeypatch.setenv("ONTOLOGY_RECOVERY_MAX_ATTEMPTS", "3")
    task = _make_task(recovery_attempts=0)
    tm = TaskManager(worker_count=0)
    await _boot(tm, {task.task_id: task})

    assert task.ontology_status == "pending"
    assert task.status == TaskStatus.QUEUED
    assert _drain(tm) == [task.task_id]


# ── 超限：两个条件都要处理 ─────────────────────────────────────────


async def test_over_limit_marks_failed_and_does_not_requeue(monkeypatch):
    """超过上限：落终态、写明确错因、不再入队。

    用户强调的正是这条：光改 ``ontology_status`` 不够——恢复逻辑后半段是
    ``if task.status not in TERMINAL_STATES`` 才决定入队，两个条件都要
    处理，否则任务照样被重跑。
    """
    monkeypatch.setenv("ONTOLOGY_RECOVERY_MAX_ATTEMPTS", "2")
    task = _make_task(recovery_attempts=2)
    tm = TaskManager(worker_count=0)
    await _boot(tm, {task.task_id: task})

    assert task.recovery_attempts == 3
    # 条件一：本体侧落 failed
    assert task.ontology_status == "failed"
    assert "OOM" in task.ontology_error or "内存" in task.ontology_error
    # 条件二：任务侧落终态 + 不入队
    assert task.status == TaskStatus.FAILED
    assert task.error_message
    assert _drain(tm) == [], "超限任务不得重新入队（否则死循环依旧）"


async def test_over_limit_boundary_is_exact(monkeypatch):
    """边界：计数等于上限时仍重跑，恰超过 1 次才落终态。"""
    monkeypatch.setenv("ONTOLOGY_RECOVERY_MAX_ATTEMPTS", "2")

    t_ok = _make_task(task_id="t-ok", recovery_attempts=1)
    tm_ok = TaskManager(worker_count=0)
    await _boot(tm_ok, {t_ok.task_id: t_ok})
    assert t_ok.ontology_status == "pending", "1+1=2 == 上限，仍允许重跑"

    t_bad = _make_task(task_id="t-bad", recovery_attempts=2)
    tm_bad = TaskManager(worker_count=0)
    await _boot(tm_bad, {t_bad.task_id: t_bad})
    assert t_bad.ontology_status == "failed", "2+1=3 > 上限，落终态"


# ── 不误伤 ─────────────────────────────────────────────────────────


async def test_non_extracting_task_untouched(monkeypatch):
    """非 extracting 的任务不受护栏影响，计数不变。"""
    monkeypatch.setenv("ONTOLOGY_RECOVERY_MAX_ATTEMPTS", "2")
    done = _make_task(
        task_id="t-done", ontology_status="completed", status=TaskStatus.COMPLETED
    )
    pending = _make_task(
        task_id="t-pending", ontology_status="pending", status=TaskStatus.QUEUED
    )
    tm = TaskManager(worker_count=0)
    await _boot(tm, {done.task_id: done, pending.task_id: pending})

    assert done.recovery_attempts == 0
    assert done.ontology_status == "completed"
    assert pending.recovery_attempts == 0
    assert pending.ontology_status == "pending"


async def test_legacy_task_without_field_is_tolerated(monkeypatch):
    """老任务 JSON 无 recovery_attempts 字段 → 缺省 0，不炸。"""
    monkeypatch.setenv("ONTOLOGY_RECOVERY_MAX_ATTEMPTS", "2")
    task = _make_task()
    assert task.recovery_attempts == 0  # 字段缺省即 0，等价于历史 JSON
    tm = TaskManager(worker_count=0)
    await _boot(tm, {task.task_id: task})

    assert task.recovery_attempts == 1
    assert task.ontology_status == "pending"
