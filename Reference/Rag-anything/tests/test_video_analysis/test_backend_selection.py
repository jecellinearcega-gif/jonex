"""_select_backend 路由单测 + _create_video 后端装配单测。# [jonex] 新增"""

import asyncio
import logging

import pytest

from raganything.processor_builder import ModalProcessorBuilder, _create_video
from raganything.video_analysis.backends.mineru import MineruVideoBackend
from raganything.video_analysis.backends.mps import MPSVideoBackend
from raganything.video_processor import VideoModalProcessor


class _StubProcessor:
    """只带上 _select_backend 所需属性的最小对象。"""

    _select_backend = VideoModalProcessor._select_backend


def _make(default_backend, pool_keys):
    proc = _StubProcessor()
    proc._default_backend = default_backend
    proc._video_backends = {k: object() for k in pool_keys}
    return proc


def test_mps_marker_wins_over_default():
    proc = _make("mineru", ["mps", "mineru"])

    assert proc._select_backend({"mps_video_url": "http://cos/x.mp4"}) == "mps"


def test_default_backend_returned_when_no_marker():
    proc = _make("mineru", ["mineru"])

    assert proc._select_backend({}) == "mineru"


def test_local_default_falls_back_to_builtin_pipeline():
    """default=local 时返回 None，走内置 local 管线（不是后端池）。"""
    proc = _make("local", ["local"])

    assert proc._select_backend({}) is None


def test_default_not_in_pool_falls_back_to_builtin_pipeline():
    """配置了未入池的后端时安全回落，不抛错。"""
    proc = _make("mineru", [])

    assert proc._select_backend({}) is None


# ── _create_video 后端装配（修复轮次新增） ─────────────────────────────────
#
# 被测规格（processor_builder._create_video）：
#   1. default_backend 取自 config.video_analysis_binding，空值回落 "local"；
#   2. 非 local 且未入池时，用 get_video_analysis_backend 取类并实例化入池；
#   3. 取类或实例化抛异常时只打 warning 并回落 "local"，不得抛穿；
#   4. 结果经 default_backend= / video_analysis_backends= 下发给 VideoModalProcessor。
#
# 既有 tests/test_processor_builder.py 用 @patch(...VideoModalProcessor) 只断言
# 「构造被调用过」，新装配块的入参不在它的断言范围内，故在此单独覆盖。


_MISSING = object()
# 用例内与「属性存在但值为 None」区分：前者走 getattr 默认值。


class _FakeLightRAG:
    """_create_video 只从 lightrag 取 tokenizer 一个属性。"""

    tokenizer = "fake-tokenizer"


class _FakeConfig:
    """最小 config 替身：只提供 _create_video 读取的 video_analysis_binding。"""

    def __init__(self, binding=_MISSING):
        if binding is not _MISSING:
            self.video_analysis_binding = binding


class _ProcessorSpy:
    """VideoModalProcessor 替身：只记录构造 kwargs。

    真实构造会走 BaseModalProcessor.__init__ 里的 asdict(lightrag)，
    需要完整 LightRAG 实例，代价远超本用例目的。
    """

    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _ExplodingBackend:
    """取类成功、实例化抛异常——覆盖「构造失败」分支。"""

    def __init__(self, config):
        raise RuntimeError("credentials rejected")


def _build_video_processor(monkeypatch, binding=_MISSING, mps_enabled=False):
    """真跑一遍 _create_video，返回替身捕获到的构造 kwargs。"""
    if mps_enabled:
        monkeypatch.setenv("MPS_ENABLED", "true")
    else:
        monkeypatch.delenv("MPS_ENABLED", raising=False)
    # _create_video 在函数体内 `from raganything.video_processor import
    # VideoModalProcessor`，取值点是 raganything.video_processor 的模块属性
    # （processor_builder 命名空间下并没有这个名字，patch 那里会无效）。
    monkeypatch.setattr(
        "raganything.video_processor.VideoModalProcessor", _ProcessorSpy
    )
    builder = ModalProcessorBuilder(
        lightrag=_FakeLightRAG(),
        config=_FakeConfig(binding),
        llm_func=lambda *args, **kwargs: "",
    )
    return _create_video(builder).kwargs


def _builder_warnings(caplog):
    """只取 processor_builder 自己打的 WARNING，避免混入后端构造的日志。"""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "raganything.processor_builder"
        and r.levelno == logging.WARNING
    ]


def test_binding_mineru_enters_pool_and_becomes_default(monkeypatch):
    """binding=mineru：取类实例化入池，并把 default_backend 下发给处理器。"""
    kwargs = _build_video_processor(monkeypatch, "mineru")

    pool = kwargs["video_analysis_backends"]
    assert kwargs["default_backend"] == "mineru"
    assert set(pool) == {"mineru"}
    assert isinstance(pool["mineru"], MineruVideoBackend)

    # 装配结果喂给真实 _select_backend：路由确实落到 mineru 后端。
    proc = _make(kwargs["default_backend"], list(pool))
    assert proc._select_backend({}) == "mineru"


def test_binding_mps_enters_pool_even_without_mps_enabled(monkeypatch):
    """新装配块走通用注册表：MPS_ENABLED 未开，binding=mps 也照样入池。"""
    kwargs = _build_video_processor(monkeypatch, "mps")

    pool = kwargs["video_analysis_backends"]
    assert kwargs["default_backend"] == "mps"
    assert set(pool) == {"mps"}
    assert isinstance(pool["mps"], MPSVideoBackend)


def test_unregistered_binding_warns_and_falls_back_to_local(monkeypatch, caplog):
    """未注册的 binding：不抛穿、不入池、回落 local，并留 warning。"""
    with caplog.at_level(logging.WARNING):
        kwargs = _build_video_processor(monkeypatch, "nonexistent")

    assert kwargs["default_backend"] == "local"
    assert kwargs["video_analysis_backends"] is None

    messages = _builder_warnings(caplog)
    assert len(messages) == 1
    assert "nonexistent" in messages[0]
    assert "local" in messages[0]


def test_backend_init_failure_warns_and_falls_back_to_local(monkeypatch, caplog):
    """取类成功但实例化抛异常：同样只 warn 并回落 local。"""
    monkeypatch.setattr(
        "raganything.video_analysis.get_video_analysis_backend",
        lambda binding: _ExplodingBackend,
    )

    with caplog.at_level(logging.WARNING):
        kwargs = _build_video_processor(monkeypatch, "exploding")

    assert kwargs["default_backend"] == "local"
    assert kwargs["video_analysis_backends"] is None

    messages = _builder_warnings(caplog)
    assert len(messages) == 1
    assert "exploding" in messages[0]
    assert "credentials rejected" in messages[0]


@pytest.mark.parametrize(
    "binding", [_MISSING, None, ""], ids=["missing-attr", "none", "empty-string"]
)
def test_blank_binding_keeps_local_and_instantiates_nothing(monkeypatch, binding):
    """binding 缺失 / None / 空串：default 恒为 local，且不碰任何后端。"""
    lookups = []
    monkeypatch.setattr(
        "raganything.video_analysis.get_video_analysis_backend",
        lambda name: lookups.append(name),
    )
    mps_inits = []
    monkeypatch.setattr(
        "raganything.video_analysis.backends.mps.MPSVideoBackend",
        lambda config: mps_inits.append(config),
    )

    kwargs = _build_video_processor(monkeypatch, binding)

    assert kwargs["default_backend"] == "local"
    assert kwargs["video_analysis_backends"] is None
    assert lookups == []
    assert mps_inits == []


def test_mps_enabled_still_enters_pool(monkeypatch):
    """MPS_ENABLED=true 的既有分支未被新装配块破坏。"""
    kwargs = _build_video_processor(monkeypatch, "local", mps_enabled=True)

    pool = kwargs["video_analysis_backends"]
    assert kwargs["default_backend"] == "local"
    assert set(pool) == {"mps"}
    assert isinstance(pool["mps"], MPSVideoBackend)


# ── _analyze_via_mps 把 source_type 抄进 synthetic segment ──  # [jonex]


class _MpsStub:
    """只带 _analyze_via_mps 所需依赖的替身。"""

    _analyze_via_mps = VideoModalProcessor._analyze_via_mps

    def _quick_video_hash(self, video_path):
        return "quick-hash"

    def _get_duration(self, video_path):
        return 100.0


class _SceneBackend:
    """替身 backend：固定返回给定 scenes。"""

    def __init__(self, scenes):
        self._scenes = scenes

    async def analyze_video(self, video_path, prompt=None):
        from raganything.video_analysis import VideoAnalysisResult

        return VideoAnalysisResult(
            summary="总览", scenes=self._scenes, analysis_method="mineru",
        )


def _run_mps_analyze(scenes):
    stub = _MpsStub()
    modal_content = {}
    asyncio.run(
        stub._analyze_via_mps(modal_content, "demo.mp4", _SceneBackend(scenes))
    )
    return modal_content["_audio_segments"]


def test_source_type_carried_into_synthetic_segments():
    """三类来源的 source_type 必须原样传下去，否则 chunk 分不出类型。"""
    segments = _run_mps_analyze([
        {"description": "章节", "start_time": 0.0, "end_time": 10.0,
         "source_type": "chapter"},
        {"description": "幻灯片", "start_time": 10.0, "end_time": 20.0,
         "source_type": "slide"},
        {"description": "语音", "start_time": 20.0, "end_time": 30.0,
         "source_type": "asr"},
    ])

    assert [s["source_type"] for s in segments] == ["chapter", "slide", "asr"]


def test_missing_source_type_defaults_to_chapter():
    """MPS 后端与历史数据没有 source_type，缺省 chapter（不得为 None）。"""
    segments = _run_mps_analyze([
        {"description": "只有描述", "start_time": 0.0, "end_time": 5.0},
    ])

    assert segments[0]["source_type"] == "chapter"


def test_empty_scenes_branch_still_has_source_type():
    """无 scenes 的兜底单段也要带 source_type，供下游查 ctype。"""
    segments = _run_mps_analyze([])

    assert len(segments) == 1
    assert segments[0]["source_type"] == "chapter"
