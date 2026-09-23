"""[jonex] 视频解析器 per-KB 路由单测（设计 2026-09-01-video-parser-per-kb-routing）。

raganything 依赖 pydantic v2，必须用 .venv-atomic-rag 运行：
    .venv-atomic-rag/bin/python -m pytest \
        Reference/Rag-anything/tests/test_video_analysis/test_video_parser_routing.py -v

⚠ 2026-10-08 从 `tests/unit/` 迁来：根 `.gitignore:84` 有一条 `/tests/`，新增
文件默认被忽略 —— `git ls-files` 证明它当年**从未被跟踪**（计划文档里那句
`git add tests/unit/test_video_parser_routing.py` 因此静默没生效）。测试进不了
版本库就不算闸门，故与 `test_backend_selection.py` 同目录：后者测
`_select_backend` 与后端池装配，本文件测**两条链**谁按它行动。
"""
import pytest

from raganything.service.task_manager import build_mps_video_url


class TestBuildMpsVideoUrl:
    def test_video_mps_cos_builds_url(self):
        url = build_mps_video_url(
            preset_name="video_mps",
            storage_backend="cos",
            storage_key="jonex/kb/t/kb1/doc1/doc1.mp4",
            cos_bucket="my-bucket-1250000000",
            cos_region="ap-guangzhou",
        )
        assert url == (
            "https://my-bucket-1250000000.cos.ap-guangzhou.myqcloud.com"
            "/jonex/kb/t/kb1/doc1/doc1.mp4"
        )

    def test_non_video_mps_preset_returns_empty(self):
        assert build_mps_video_url(
            preset_name="video_full_pipeline",
            storage_backend="cos",
            storage_key="k",
            cos_bucket="b",
        ) == ""

    def test_non_cos_backend_returns_empty(self):
        assert build_mps_video_url(
            preset_name="video_mps",
            storage_backend="local",
            storage_key="k",
            cos_bucket="b",
        ) == ""

    def test_missing_storage_key_returns_empty(self):
        assert build_mps_video_url(
            preset_name="video_mps",
            storage_backend="cos",
            storage_key="",
            cos_bucket="b",
        ) == ""

    def test_missing_bucket_returns_empty(self):
        assert build_mps_video_url(
            preset_name="video_mps",
            storage_backend="cos",
            storage_key="k",
            cos_bucket="",
        ) == ""

    def test_blank_preset_returns_empty(self):
        assert build_mps_video_url(
            preset_name="", storage_backend="cos", storage_key="k", cos_bucket="b",
        ) == ""


from dataclasses import dataclass
from unittest.mock import MagicMock

from raganything.video_processor import VideoModalProcessor


@dataclass
class _FakeConfig:
    """最小配置替身：BaseModalProcessor.__init__ 会访问 lightrag 的存储实例
    并对其调 asdict()，全部给 None 占位（构造阶段仅赋值，不调用）。"""

    text_chunks: object = None
    chunks_vdb: object = None
    entities_vdb: object = None
    relationships_vdb: object = None
    chunk_entity_relation_graph: object = None
    embedding_func: object = None
    llm_model_func: object = None
    llm_response_cache: object = None
    tokenizer: object = None


def _make_processor(video_backends=None):
    return VideoModalProcessor(
        lightrag=_FakeConfig(),
        modal_caption_func=None,
        video_analysis_backends=video_backends or {},
        default_backend="local",
    )


class TestSelectBackendRouting:
    def test_mps_url_with_registered_backend_returns_mps(self):
        proc = _make_processor({"mps": MagicMock()})
        assert proc._select_backend({"mps_video_url": "https://b.cos.r.myqcloud.com/k"}) == "mps"

    def test_mps_url_without_registered_backend_returns_none(self):
        proc = _make_processor({})
        assert proc._select_backend({"mps_video_url": "https://b.cos.r.myqcloud.com/k"}) is None

    def test_no_mps_url_returns_none(self):
        proc = _make_processor({"mps": MagicMock()})
        assert proc._select_backend({}) is None
        assert proc._select_backend({"mps_video_url": ""}) is None

    def test_non_mps_binding_returns_that_backend(self):
        """[jonex] 池里装的是 mineru 时，默认后端就是 mineru（不是 mps、也不是 None）。"""
        proc = VideoModalProcessor(
            lightrag=_FakeConfig(),
            modal_caption_func=None,
            video_analysis_backends={"mineru": MagicMock()},
            default_backend="mineru",
        )
        assert proc._select_backend({}) == "mineru"

    def test_mps_binding_without_url_still_selects_mps(self):
        """[jonex] KB 绑定 mps 时，即使没有 COS URL，_select_backend 也会选 mps
        （走 default 分支，不是 mps_video_url 分支）—— 这正是 task_manager 那条
        「MPS 需要 COS URL」短路口必须保留的原因。"""
        proc = VideoModalProcessor(
            lightrag=_FakeConfig(),
            modal_caption_func=None,
            video_analysis_backends={"mps": MagicMock()},
            default_backend="mps",
        )
        assert proc._select_backend({}) == "mps"


# ════════════════════════════════════════════════════════════════════════════
# 2026-10-08 现场：VIDEO_ANALYSIS_BINDING=mineru 却被当成 MPS 短路
#
# 现象：上传视频后 chunk 正文是「该视频的转写已跳过：MPS 视频分析需要 COS 存储
# 的 URL…」，而该 KB 根本没配 MPS（MPS_ENABLED=false，绑定的是 mineru）。
#
# 成因：task_manager._textualize_multimodal 的判据是「_video_backends 非空」，
# 注释也写成「非空 = MPS 路径（MPS_ENABLED=true）」。这在 video_analysis_binding
# 还是死配置（恒为 local）时成立；绑定接活后 mineru 也会入池，池子非空就再
# 不等于走 MPS。正确判据是「这次到底选中了哪个后端」。
# ════════════════════════════════════════════════════════════════════════════

import asyncio
from pathlib import Path
from types import SimpleNamespace

from raganything.service.models import TaskInfo
from raganything.service.task_manager import TaskHandle, TaskManager

MPS_SKIP_MARK = "MPS 视频分析需要 COS 存储的 URL"
TRANSCRIPT = "这是 MinerU 转写出来的正文"


def _task(**kw):
    return TaskInfo(
        tenant_id="tenant_jonex_demo", name="v.mp4", file_path="/tmp/v.mp4",
        document_id="doc-1", **kw,
    )


class _VideoProcStub:
    """只换掉「真的去调后端」这一步；路由逻辑用真实的 _select_backend。"""
    _select_backend = VideoModalProcessor._select_backend

    def __init__(self, backends, default_backend="local"):
        self._video_backends = backends
        self._default_backend = default_backend
        self.calls = []
        self.item_info_seen = []

    async def generate_description_only(self, *, modal_content, content_type,
                                        prompt_overrides=None, item_info=None):
        # ⚠ 两条链的调用签名本身有一处已知差异（不是本次要修的"路由判据"）：
        #   · full 链 stages.py:619      传 item_info=item（供 context 提取）
        #   · parse_only 链 task_manager  不传
        # 所以替身必须容忍 item_info，否则 full 链会 TypeError 被 gather 静默吞掉。
        # 真实后端会**原地**写 _audio_segments（转写正文），task_manager 从它取数。
        # 先写再记录：这样 calls[0] 同时带上「注入的 COS URL」与「产出的正文」。
        self.item_info_seen.append(item_info)
        modal_content["_audio_segments"] = [
            {"text": TRANSCRIPT, "start_time": 0.0, "end_time": 1.0},
        ]
        self.calls.append(dict(modal_content))
        return "全局摘要", {}


def _manager(proc, warnings):
    tm = TaskManager.__new__(TaskManager)  # 绕开 __init__，只铺这条路径用到的属性
    tm._pipeline_executor = SimpleNamespace(
        modal_processors={"video": proc, "generic": object()},
        config=SimpleNamespace(
            enable_video_processing=True,
            enable_audio_processing=True,
            enable_image_processing=True,
        ),
    )
    tm._resolve_task_prompt_overrides = lambda task: None
    tm._record_multimodal_warning = lambda task, wtype, msg: warnings.append((wtype, msg))
    tm._repo = SimpleNamespace(save=lambda task: None)
    return tm


async def _transcribe(tmp_path, monkeypatch, proc, item, *, task_mps_url=""):
    """task_mps_url 模拟 task.mps_video_url —— 仅 COS 存储时平台侧才会设置它。"""
    monkeypatch.setenv("RAG_INPUTS_DIR", str(tmp_path))
    warnings = []
    task = _task(mps_video_url=task_mps_url)
    await _manager(proc, warnings)._textualize_multimodal(
        task, TaskHandle(lambda: None), [item],
    )
    return task, warnings


class TestMpsShortCircuitGate:
    """后端池非空 ≠ 走 MPS。"""

    @pytest.mark.asyncio
    async def test_mineru_binding_transcribes_instead_of_skipping(self, tmp_path, monkeypatch):
        """线上的实际形态：绑定 mineru、池子非空、无 COS URL → 必须真转写。"""
        proc = _VideoProcStub({"mineru": MagicMock()}, default_backend="mineru")
        item = {"type": "video", "video_path": "/tmp/v.mp4"}

        _task_obj, warnings = await _transcribe(tmp_path, monkeypatch, proc, item)

        assert len(proc.calls) == 1, "mineru 绑定必须真的调后端，不能被 MPS 短路口拦掉"
        assert MPS_SKIP_MARK not in item["description"]
        assert TRANSCRIPT in item["description"]
        assert [w[0] for w in warnings] == []

    @pytest.mark.asyncio
    async def test_mineru_binding_is_not_given_a_cos_url(self, tmp_path, monkeypatch):
        """mineru 后端拿 video_path 上传本地文件；塞 COS URL 会被当本地路径打开。"""
        proc = _VideoProcStub({"mineru": MagicMock()}, default_backend="mineru")
        item = {"type": "video", "video_path": "/tmp/v.mp4"}

        await _transcribe(tmp_path, monkeypatch, proc, item)

        assert proc.calls[0].get("mps_video_url", "") == ""
        assert proc.calls[0]["video_path"] == "/tmp/v.mp4"

    @pytest.mark.asyncio
    async def test_mps_binding_without_cos_url_still_skips(self, tmp_path, monkeypatch):
        """绑定 mps 但视频在本地存储 → 保留原来的「提前短路 + 明确占位」。"""
        proc = _VideoProcStub({"mps": MagicMock()}, default_backend="mps")
        item = {"type": "video", "video_path": "/tmp/v.mp4"}

        _t, warnings = await _transcribe(tmp_path, monkeypatch, proc, item)

        assert proc.calls == [], "MPS 路径拿不到 COS URL 时不该去调后端（会白等长超时）"
        assert MPS_SKIP_MARK in item["description"]
        assert [w[0] for w in warnings] == ["video_mps_no_cos_url"]

    @pytest.mark.asyncio
    async def test_mps_binding_with_cos_url_uses_mps(self, tmp_path, monkeypatch):
        """COS 存储 + 绑定 mps → 走 MPS，且 COS URL 必须传进去。"""
        proc = _VideoProcStub({"mps": MagicMock()}, default_backend="mps")
        item = {"type": "video", "video_path": "/tmp/v.mp4"}
        url = "https://b.cos.r.myqcloud.com/k.mp4"

        _t, warnings = await _transcribe(tmp_path, monkeypatch, proc, item,
                                         task_mps_url=url)

        assert len(proc.calls) == 1
        assert proc.calls[0]["mps_video_url"] == url
        assert MPS_SKIP_MARK not in item["description"]
        assert [w[0] for w in warnings] == []

    @pytest.mark.asyncio
    async def test_mps_in_pool_but_binding_local_and_cos_url_uses_mps(
        self, tmp_path, monkeypatch,
    ):
        """video_mps 预设的形态：MPS_ENABLED=true 但 VIDEO_ANALYSIS_BINDING=local，
        task 带 COS URL（COS 存储）→ 仍须走 MPS（这条老行为不能被改掉）。"""
        proc = _VideoProcStub({"mps": MagicMock()}, default_backend="local")
        item = {"type": "video", "video_path": "/tmp/v.mp4"}
        url = "https://b.cos.r.myqcloud.com/k.mp4"

        _t, _w = await _transcribe(tmp_path, monkeypatch, proc, item, task_mps_url=url)

        assert len(proc.calls) == 1
        assert proc.calls[0]["mps_video_url"] == url

    @pytest.mark.asyncio
    async def test_mineru_binding_with_cos_storage_is_not_given_the_url(
        self, tmp_path, monkeypatch,
    ):
        """mineru 绑定 + COS 存储（task 带 COS URL）→ 仍然**不能**注入：
        mineru 拿 video_path 上传本地文件，塞 URL 会被当本地路径打开。"""
        proc = _VideoProcStub({"mineru": MagicMock()}, default_backend="mineru")
        item = {"type": "video", "video_path": "/tmp/v.mp4"}

        _t, _w = await _transcribe(
            tmp_path, monkeypatch, proc, item,
            task_mps_url="https://b.cos.r.myqcloud.com/k.mp4",
        )

        assert len(proc.calls) == 1
        assert proc.calls[0].get("mps_video_url", "") == ""
        assert proc.calls[0]["video_path"] == "/tmp/v.mp4"

    @pytest.mark.asyncio
    async def test_local_binding_with_no_backend_pool_uses_local_pipeline(
        self, tmp_path, monkeypatch,
    ):
        """池子空 + 绑定 local（默认画像）→ 内置本地管线，行为不变。"""
        proc = _VideoProcStub({}, default_backend="local")
        item = {"type": "video", "video_path": "/tmp/v.mp4"}

        _t, warnings = await _transcribe(tmp_path, monkeypatch, proc, item)

        assert len(proc.calls) == 1
        assert MPS_SKIP_MARK not in item["description"]
        assert [w[0] for w in warnings] == []


# ════════════════════════════════════════════════════════════════════════════
# 方案 A：两条链共用同一个决策函数，并用「两链一致」用例盯住它们不分叉
#
# 背景：同一个 preset、同一个视频，本体库（full 链）能解析、jonex_wiki
# （parse_only 链）不行 —— 因为「要不要走 MPS / 要不要注入 COS URL」这段判断
# 原本在两处各写了一份，只有 parse_only 那份用了过期的「后端池非空 = MPS」。
# 现在两处都调 video_processor.route_video，本文件既测它本身，也把两条**真实**
# 链各跑一遍比对结论。
# ════════════════════════════════════════════════════════════════════════════

from raganything.video_processor import route_video

COS_URL = "https://b.cos.r.myqcloud.com/k.mp4"


class TestRouteVideoDecisionMatrix:
    """共享决策函数的判定表（参数化的场景矩阵）。"""

    @pytest.mark.parametrize(
        "pool,binding,mps_url,exp_backend,exp_use_mps,exp_needs_url",
        [
            # pool,              binding,  mps_url, backend,  use_mps, needs_cos_url
            ({"mineru"}, "mineru", "", "mineru", False, False),   # ← 线上形态
            ({"mineru"}, "mineru", COS_URL, "mineru", False, False),  # 不许塞 URL 给 mineru
            ({"mps"}, "mps", "", "mps", True, True),              # 绑定 mps 但本地存储 → 短路
            ({"mps"}, "mps", COS_URL, "mps", True, False),        # 绑定 mps + COS → 走 MPS
            ({"mps"}, "local", "", None, False, False),           # MPS 入池但用不上 → 本地管线
            ({"mps"}, "local", COS_URL, None, True, False),       # video_mps 预设 + COS → 走 MPS
            ({}, "local", "", None, False, False),                # 默认画像 → 本地管线
            ({}, "local", COS_URL, None, False, False),           # mps 未入池 → 注入是空动作
        ],
    )
    def test_matrix(self, pool, binding, mps_url, exp_backend, exp_use_mps, exp_needs_url):
        proc = _VideoProcStub(pool, default_backend=binding)
        r = route_video(proc, {}, mps_url)
        assert r.backend == exp_backend
        assert r.use_mps is exp_use_mps
        assert r.needs_cos_url is exp_needs_url

    def test_needs_cos_url_implies_use_mps(self):
        """needs_cos_url 是 use_mps 的子集（不能出现"要短路却没打算走 MPS"）。"""
        for pool in ({}, {"mps"}, {"mineru"}, {"mps", "mineru"}):
            for binding in ("local", "mps", "mineru"):
                for url in ("", COS_URL):
                    r = route_video(_VideoProcStub(pool, default_backend=binding), {}, url)
                    assert not r.needs_cos_url or r.use_mps, (pool, binding, url)
                    assert not (r.use_mps and not url) or r.needs_cos_url


# ── 两条真实链的驱动 ──────────────────────────────────────────────────────

def _effective_backend(proc, calls):
    """这次调用实际会分派到哪个后端 = processor 内部那次 _select_backend。"""
    return proc._select_backend(calls[0]) if calls else None


def _injected_url(calls):
    return (calls[0].get("mps_video_url") or "") if calls else None


async def _run_full_chain(proc, item, *, task_mps_url=""):
    """full 链（本体库走的那条）：MultimodalStage.execute → _process_one。"""
    from raganything.pipeline.stages import MultimodalStage

    ctx = SimpleNamespace(
        multimodal_items=[dict(item)], mps_video_url=task_mps_url,
        content_list=None, file_name="v.mp4", doc_id="doc-1",
    )
    services = SimpleNamespace(
        modal_processors={"video": proc, "generic": object()},
        config=SimpleNamespace(max_parallel_multimodal=1, max_parallel_vlm=1),
        lightrag=None, logger=None, callback_manager=None, event_bus=None,
    )
    return await MultimodalStage().execute(ctx, services)


class TestTwoChainsAgree:
    """同一个场景下，full 链与 parse_only 链必须做出**完全相同**的视频后端决策。

    这是方案 A 的核心闸门：两链各写一份判据是 2026-10-08 那次
    「本体库正常、jonex_wiki 假 MPS 占位」的根因。
    """

    @pytest.mark.parametrize(
        "pool,binding,task_mps_url,parse_short_circuits",
        [
            ({"mineru"}, "mineru", "", False),        # 线上：必须都真调后端
            ({"mineru"}, "mineru", COS_URL, False),   # 都不许注入 URL
            ({"mps"}, "mps", "", True),               # parse_only 短路；full 链**没有**这道闸
            ({"mps"}, "mps", COS_URL, False),         # 都必须走 MPS
            ({"mps"}, "local", COS_URL, False),       # 都必须走 MPS
            ({}, "local", "", False),                 # 都必须走本地管线
        ],
    )
    @pytest.mark.asyncio
    async def test_same_decision_in_both_chains(
        self, tmp_path, monkeypatch, pool, binding, task_mps_url,
        parse_short_circuits,
    ):
        item = {"type": "video", "video_path": "/tmp/v.mp4"}
        ctx = f"pool={pool} binding={binding} url={bool(task_mps_url)}"

        proc_parse = _VideoProcStub(pool, default_backend=binding)
        await _transcribe(tmp_path, monkeypatch, proc_parse, dict(item),
                          task_mps_url=task_mps_url)

        proc_full = _VideoProcStub(pool, default_backend=binding)
        await _run_full_chain(proc_full, item, task_mps_url=task_mps_url)

        # ① 参数表与共享决策函数同步（防止测试自己写错期望）
        shared = route_video(_VideoProcStub(pool, default_backend=binding), {}, task_mps_url)
        assert shared.needs_cos_url is parse_short_circuits, f"参数表过期：{ctx}"

        # ② 分享同一个决策函数之后，**真正必须一致**的是这两样：
        #    注入给后端的 COS URL、以及实际分派的后端。
        if proc_parse.calls and proc_full.calls:
            assert _injected_url(proc_parse.calls) == _injected_url(proc_full.calls), \
                f"注入的 COS URL 不一致：{ctx}"
            assert _effective_backend(proc_parse, proc_parse.calls) == \
                _effective_backend(proc_full, proc_full.calls), f"选中后端不一致：{ctx}"

        # ③ 唯一允许保留的不对称：needs_cos_url 时 parse_only 链提前短路（不白等
        #    MPS 长超时、写占位），而 full 链**没有**这道闸、照调不误。
        #    这是有意的差异（§56），不是判据漂移 —— 所以两条链的**决策**仍一致。
        if parse_short_circuits:
            assert proc_parse.calls == [], f"parse_only 链应短路：{ctx}"
            assert proc_full.calls, f"full 链没有短路闸、应当照调：{ctx}"
            assert _effective_backend(proc_full, proc_full.calls) == "mps", ctx
        else:
            assert proc_parse.calls and proc_full.calls, f"两条链都该真调后端：{ctx}"

    @pytest.mark.asyncio
    async def test_item_info_divergence_is_known_and_pinned(self, tmp_path, monkeypatch):
        """⚠ 已知分歧（不属"路由判据"，本次未改，故意钉住现状）。

        full 链 stages.py:619 传 ``item_info=item``（供 ContextExtractor 按 page
        取周边文本），parse_only 链 task_manager 不传 → 后者拿不到页面上下文。
        这是**另一处**两链漂移，与本次修的 MPS 判据无关；把它钉在这里是为了让
        「两条链应当一致」这件事不至于再次悄悄滑过去 —— 若将来对齐了，请连同
        本测试一起改。
        """
        proc_parse = _VideoProcStub({"mineru": MagicMock()}, default_backend="mineru")
        item = {"type": "video", "video_path": "/tmp/v.mp4"}
        await _transcribe(tmp_path, monkeypatch, proc_parse, dict(item))
        proc_full = _VideoProcStub({"mineru": MagicMock()}, default_backend="mineru")
        await _run_full_chain(proc_full, item)

        # 比关键字段即可：full 链传的是**同一个 dict 对象**，替身会往它上面写
        # _audio_segments，整体相等比的是被就地改过的内容。
        assert proc_parse.item_info_seen == [None], \
            "parse_only 链开始传 item_info 了 → 请更新本测试"
        assert len(proc_full.item_info_seen) == 1
        assert proc_full.item_info_seen[0]["video_path"] == "/tmp/v.mp4", \
            "full 链不再传 item_info 了 → 请更新本测试"

    @pytest.mark.asyncio
    async def test_bound_mineru_shape_really_transcribes_in_both_chains(self, tmp_path, monkeypatch):
        """线上形态（绑定 mineru、本地存储）两条链都要真的拿到转写正文。"""
        pool = {"mineru": MagicMock()}

        # ① parse_only 链（jonex_wiki / openkb）：正文落进 item["description"]
        parse_item = {"type": "video", "video_path": "/tmp/v.mp4"}
        proc_parse = _VideoProcStub(pool, default_backend="mineru")
        _t, warnings = await _transcribe(
            tmp_path, monkeypatch, proc_parse, parse_item)

        # ② full 链（本体库）：正文进 multimodal_results 的 description
        full_item = {"type": "video", "video_path": "/tmp/v.mp4"}
        proc_full = _VideoProcStub(pool, default_backend="mineru")
        result = await _run_full_chain(proc_full, full_item)

        # 两条链都真的分派到 mineru，且都拿到转写正文、都没写 MPS 占位
        assert _effective_backend(proc_parse, proc_parse.calls) == "mineru"
        assert _effective_backend(proc_full, proc_full.calls) == "mineru"
        assert TRANSCRIPT in parse_item["description"]
        assert MPS_SKIP_MARK not in parse_item["description"]
        assert warnings == []
        assert TRANSCRIPT in proc_full.calls[0]["_audio_segments"][0]["text"]
        assert "description" in result.multimodal_results[0]

    def test_neither_chain_reimplements_the_decision(self):
        """漂移守卫：两处调用点都必须走 route_video，不许再内联自己的判据。"""
        import raganything.pipeline.stages as stages_mod
        import raganything.service.task_manager as tm_mod

        full_src = Path(stages_mod.__file__).read_text(encoding="utf-8")
        parse_src = Path(tm_mod.__file__).read_text(encoding="utf-8")

        assert "route_video(" in full_src, "full 链没有走共享决策函数"
        assert "route_video(" in parse_src, "parse_only 链没有走共享决策函数"
        # 旧的两处各自为政的写法都不该复活
        assert "_video_backends" not in full_src, \
            "full 链又去直接读后端池了 —— 判据必须留在 route_video"
        assert "and not mps_url" not in parse_src, \
            "parse_only 链又出现了「池非空就短路」的粗判据"
