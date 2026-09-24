# -*- coding: utf-8 -*-
"""[jonex] P0-a 零产出兜底规模闸门单测。

线上事故（2026-09-13）实证链条：financial_metric.md 的 1438 张表**全部**
主键识别失败（``应用表头判定`` 成功但 21 列无一被认作主键）→
``_merge_table_objects`` 返回 False → ``table_produced=False``；而该文档的
**全部** 46290 个 chunk 都是表格 chunk，主抽取又一个都不跑（见 P0-b）。

两头一夹，P10 批 2.4 的「零产出兜底」集合就**等于全量集合** —— 兜底退化成
「把整篇文档原样喂回 LLM」：46290 chunk / 5787 批 ≈ 4.6 小时，产出 19647
实体 + 34522 关系，回写 payload 过大超时（``custom-kg 回写失败`` 空错误信息 =
``TimeoutError``），backfill 阶段把宿主内存吃到 23.7GB 被内核 OOM killer 杀掉。

闸门判据（双闸，任一命中即拒绝）：
- 绝对闸 ``ONTOLOGY_TABLE_FALLBACK_MAX_CHUNKS``（默认 2000）——独立生效，
  真正防 OOM 的那道；
- 比例闸 ``ONTOLOGY_TABLE_FALLBACK_MAX_RATIO``（默认 0.5）——需叠加规模下限
  ``ONTOLOGY_TABLE_FALLBACK_RATIO_MIN_CHUNKS``（默认 200）才生效。

**规模下限不是可有可无的**：小文档「整篇都是表」是常态，仓库里就有一个
1 chunk 的表格文档单测（test_task_manager_ontology_p4.py::TestTableSupplement）
—— 1 个 chunk 占 100% 也花不掉几个 LLM。无下限时比例闸会把这类正常小文档
一并误杀，把「防 OOM」变成「小表格文档一律不抽」。

拒绝而非降级：写图是 ``delete_by_document`` 全量替换，放它跑完等于用「表格当
普通文本」的差结果换掉首轮好结果；拒绝则图一行不动。
"""
import pytest

from raganything.service.task_manager import TaskManager

GATE = TaskManager._table_fallback_gate


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """默认值必须与线上未配置 env 时一致，故先清干净。"""
    monkeypatch.delenv("ONTOLOGY_TABLE_FALLBACK_MAX_RATIO", raising=False)
    monkeypatch.delenv("ONTOLOGY_TABLE_FALLBACK_MAX_CHUNKS", raising=False)
    monkeypatch.delenv("ONTOLOGY_TABLE_FALLBACK_RATIO_MIN_CHUNKS", raising=False)


def test_live_incident_shape_is_rejected():
    """线上那次的实际形状：46290/46290 —— 必须拒绝。"""
    ok, detail = GATE(46290, 46290)
    assert ok is False
    assert "46290/46290" in detail
    assert "100%" in detail


def test_small_minority_fallback_is_allowed():
    """兜底的本职：补回少量被误剔除的表格 chunk —— 放行。"""
    ok, detail = GATE(20, 5000)
    assert ok is True
    assert "20/5000" in detail


# ── 规模下限：防误杀小文档（既有 TestTableSupplement 用 1 chunk 表格文档）──

def test_tiny_all_table_doc_is_allowed():
    """1 chunk 且 100% 是表格 —— 兜底必须照跑。"""
    assert GATE(1, 1)[0] is True
    assert GATE(150, 150)[0] is True        # 下限 200 以内，比例闸不生效


def test_tiny_doc_ratio_gate_engages_above_floor():
    """超过下限后比例闸才生效。"""
    assert GATE(200, 300)[0] is True        # 67% > 50%，但 200 未过下限
    assert GATE(201, 300)[0] is False       # 67% > 50%，且 201 > 200 → 拦


def test_ratio_threshold_is_exclusive():
    """恰好等于 50% 不在闸门内（判据是 > 而非 >=）。"""
    assert GATE(250, 500)[0] is True
    assert GATE(260, 500)[0] is False


def test_absolute_gate_is_independent_of_ratio():
    """绝对闸独立生效：哪怕占比很低，规模超上限也拦。"""
    assert GATE(2001, 100000)[0] is False   # ratio 2%，count 超 2000
    assert GATE(2000, 100000)[0] is True    # 边界：不超即放行


def test_absolute_gate_can_be_raised_to_allow_big_fallback(monkeypatch):
    """运维确认可承受时，调高绝对闸+比例闸即可放行（不是硬编码死判）。"""
    monkeypatch.setenv("ONTOLOGY_TABLE_FALLBACK_MAX_CHUNKS", "50000")
    monkeypatch.setenv("ONTOLOGY_TABLE_FALLBACK_MAX_RATIO", "1.0")
    assert GATE(46290, 46290)[0] is True


def test_gates_can_be_tightened(monkeypatch):
    monkeypatch.setenv("ONTOLOGY_TABLE_FALLBACK_MAX_RATIO", "0.05")
    monkeypatch.setenv("ONTOLOGY_TABLE_FALLBACK_MAX_CHUNKS", "100000")
    monkeypatch.setenv("ONTOLOGY_TABLE_FALLBACK_RATIO_MIN_CHUNKS", "10")
    assert GATE(300, 100000)[0] is True     # 0.3% < 5%
    assert GATE(3000, 10000)[0] is False    # 30% > 5%，且 3000 > 10


def test_invalid_env_falls_back_to_defaults(monkeypatch):
    """env 写坏了不能把闸门关掉（旧写法 int() 会抛 ValueError 直接炸穿抽取）。"""
    monkeypatch.setenv("ONTOLOGY_TABLE_FALLBACK_MAX_CHUNKS", "not-a-number")
    monkeypatch.setenv("ONTOLOGY_TABLE_FALLBACK_MAX_RATIO", "abc")
    monkeypatch.setenv("ONTOLOGY_TABLE_FALLBACK_RATIO_MIN_CHUNKS", "")
    assert GATE(2001, 10000)[0] is False   # 绝对闸按默认 2000 生效
    assert GATE(1, 1)[0] is True           # 比例闸按默认 0.5 + 下限 200，不抛异常


def test_zero_total_does_not_divide_by_zero():
    """chunks 为空时上游已短路，但闸门自身不得抛除零。"""
    ok, detail = GATE(0, 0)
    assert ok is True
    assert "0/0" in detail
