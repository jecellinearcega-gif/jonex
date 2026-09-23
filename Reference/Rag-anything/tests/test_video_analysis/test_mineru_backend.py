"""MineruVideoBackend 单测 — 映射与 HTTP 生命周期。# [jonex] 新增"""

import asyncio
import json
import logging
import subprocess
import sys

import pytest

from raganything.video_analysis.backends.mineru import (
    BASE_URL_ENV,
    FALLBACK_BASE_URL_ENV,
    MineruVideoBackend,
    _resolve_base_url,
    map_video_result,
)


def _mineru_result(**overrides):
    """构造一份 MinerU VideoResult（§3.4 契约）。"""
    base = {
        "type": "video",
        "source": {
            "file_type": "video",
            "video_format": "mp4",
            "original_filename": "demo.mp4",
        },
        "duration": 120.5,
        "timeline_mode": "speech",
        "video_mode": "text",
        "summary": {
            "overview": "这是一段产品演示视频。",
            "topics": ["产品", "演示"],
            "lang": "zh",
        },
        "chapters": [
            {
                "id": 0,
                "start": 0.0,
                "end": 30.0,
                "title": "开场",
                "summary": "主持人介绍背景。",
                "key_points": ["背景说明"],
            },
            {
                "id": 1,
                "start": 30.0,
                "end": 120.5,
                "title": "功能演示",
                "summary": "演示核心功能。",
                "key_points": [],
            },
        ],
        "segments": [{"start": 0.0, "end": 5.0, "text": "大家好", "source": "speech"}],
        "slides": [],
        "keyframe_ocr": {"enabled": False, "keyframes": []},
        "errors": [],
    }
    base.update(overrides)
    return base


def test_maps_chapters_to_scenes_with_expected_keys():
    """下游 _analyze_via_mps 只读 description/start_time/end_time。

    注：`_mineru_result` 里另有一条 speech 段，会按 chapter_id 归并出一条
    asr scene —— 故这里按 source_type 过滤出章节来看断言。
    """
    result = map_video_result(_mineru_result())

    assert result.analysis_method == "mineru"
    assert result.summary == "这是一段产品演示视频。"
    assert result.scenes is not None

    chapters = [s for s in result.scenes if s["source_type"] == "chapter"]
    assert len(chapters) == 2

    first = chapters[0]
    assert first["start_time"] == 0.0
    assert first["end_time"] == 30.0
    assert "主持人介绍背景。" in first["description"]
    assert "背景说明" in first["description"]


def test_empty_chapters_yields_none_scenes():
    """三类来源全空时才 scenes=None（只清 chapters 仍会有 asr scene）。"""
    result = map_video_result(
        _mineru_result(chapters=[], slides=[], segments=[])
    )

    assert result.scenes is None
    assert result.summary == "这是一段产品演示视频。"


def test_none_timestamps_pass_through():
    """chapter.start/end 为 None 时透传，下游会按索引均分时长。"""
    result = map_video_result(_mineru_result(chapters=[
        {"id": 0, "start": None, "end": None, "title": "无时间", "summary": "", "key_points": []},
    ]))

    assert result.scenes[0]["start_time"] is None
    assert result.scenes[0]["end_time"] is None


def test_topics_wrapped_into_tags():
    """MinerU topics 是 list[str]，VideoAnalysisResult.tags 是 List[Dict]。"""
    result = map_video_result(_mineru_result())

    assert result.tags == [
        {"name": "产品", "confidence": None, "category": None},
        {"name": "演示", "confidence": None, "category": None},
    ]


def test_empty_topics_yields_none_tags():
    result = map_video_result(_mineru_result(summary={"overview": "x", "topics": [], "lang": "zh"}))

    assert result.tags is None


def test_raw_json_is_lossless():
    """segments/slides/keyframe_ocr 无对应字段，必须完整保留在 raw_json。"""
    payload = _mineru_result()
    result = map_video_result(payload)

    assert json.loads(result.raw_json) == payload


def test_metadata_carries_diagnostics():
    result = map_video_result(_mineru_result(errors=["step asr skipped: no audio"]))

    assert result.metadata["duration"] == 120.5
    assert result.metadata["timeline_mode"] == "speech"
    assert result.metadata["video_mode"] == "text"
    assert result.metadata["errors"] == ["step asr skipped: no audio"]
    assert result.metadata["source"]["video_format"] == "mp4"


def test_missing_summary_block_does_not_crash():
    result = map_video_result({"chapters": [], "segments": []})

    assert result.summary == ""
    assert result.scenes is None
    assert result.tags is None


# ── MineruVideoBackend ────────────────────────────────────────────


class _FakeTransport:
    """替身：记录调用并按脚本返回。"""

    def __init__(self, content_list, *, poll_error=None, submit_error=None):
        self.content_list = content_list
        self.poll_error = poll_error
        self.submit_error = submit_error
        self.submitted_fields = None
        self.polled = []
        self.fetched_stem = None

    def _ascii_safe_filename(self, name):
        return name

    def _post_multipart(self, path, file_path, fields, timeout=300):
        assert path == "/tasks"
        self.submitted_fields = fields
        if self.submit_error:
            raise self.submit_error
        return {"task_id": "t-1"}

    def _poll_task(self, task_id, timeout=None):
        self.polled.append(task_id)
        if self.poll_error:
            raise self.poll_error

    def _fetch_result_item(self, task_id, file_stem):
        self.fetched_stem = file_stem
        return {"content_list": self.content_list}


def _write_video(tmp_path):
    path = tmp_path / "demo.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    return path


def test_backend_submits_polls_and_maps(tmp_path, monkeypatch):
    payload = _mineru_result()
    transport = _FakeTransport(json.dumps(payload))
    monkeypatch.setattr(
        MineruVideoBackend, "_build_transport", lambda self: transport
    )

    backend = MineruVideoBackend(config=None)
    path = _write_video(tmp_path)
    result = asyncio.run(backend.analyze_video(str(path)))

    assert result.analysis_method == "mineru"
    assert transport.polled == ["t-1"]
    assert transport.fetched_stem == "demo"
    assert transport.submitted_fields["return_content_list"] is True


def test_backend_sends_video_mode_when_not_text(tmp_path, monkeypatch):
    transport = _FakeTransport(json.dumps(_mineru_result()))
    monkeypatch.setattr(
        MineruVideoBackend, "_build_transport", lambda self: transport
    )
    monkeypatch.setenv("MINERU_VIDEO_MODE", "visual")

    backend = MineruVideoBackend(config=None)
    asyncio.run(backend.analyze_video(str(_write_video(tmp_path))))

    assert transport.submitted_fields["video_mode"] == "visual"


def test_backend_omits_video_mode_for_text(tmp_path, monkeypatch):
    transport = _FakeTransport(json.dumps(_mineru_result()))
    monkeypatch.setattr(
        MineruVideoBackend, "_build_transport", lambda self: transport
    )
    monkeypatch.delenv("MINERU_VIDEO_MODE", raising=False)

    backend = MineruVideoBackend(config=None)
    asyncio.run(backend.analyze_video(str(_write_video(tmp_path))))

    assert "video_mode" not in transport.submitted_fields


def test_backend_raises_on_poll_failure(tmp_path, monkeypatch):
    transport = _FakeTransport(None, poll_error=RuntimeError("MinerU failed"))
    monkeypatch.setattr(
        MineruVideoBackend, "_build_transport", lambda self: transport
    )

    backend = MineruVideoBackend(config=None)
    with pytest.raises(RuntimeError, match="MinerU failed"):
        asyncio.run(backend.analyze_video(str(_write_video(tmp_path))))


def test_backend_raises_when_content_list_missing(tmp_path, monkeypatch):
    transport = _FakeTransport(None)
    monkeypatch.setattr(
        MineruVideoBackend, "_build_transport", lambda self: transport
    )

    backend = MineruVideoBackend(config=None)
    with pytest.raises(RuntimeError, match="content_list"):
        asyncio.run(backend.analyze_video(str(_write_video(tmp_path))))


def test_backend_missing_file_raises():
    backend = MineruVideoBackend(config=None)
    with pytest.raises(FileNotFoundError):
        asyncio.run(backend.analyze_video("/nonexistent/nope.mp4"))


@pytest.mark.parametrize(
    "video_env,selfhost_env,expected",
    [
        ("http://video:9000", "http://selfhost:8000", "http://video:9000"),
        (None, "http://selfhost:8000", "http://selfhost:8000"),
        ("http://video:9000/", None, "http://video:9000"),
        (None, None, "http://127.0.0.1:8000"),
    ],
)
def test_resolve_base_url_priority(monkeypatch, video_env, selfhost_env, expected):
    """MINERU_VIDEO_BASE_URL → MINERU_SELFHOST_BASE_URL → 默认，且去尾斜杠。"""
    for name, value in (
        (BASE_URL_ENV, video_env),
        (FALLBACK_BASE_URL_ENV, selfhost_env),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    assert _resolve_base_url() == expected


def test_title_fallback_when_chapter_summary_empty():
    """description 回落链：chapter.summary 为空时用 chapter.title。"""
    result = map_video_result(_mineru_result(chapters=[
        {"id": 0, "start": 0.0, "end": 1.0, "title": "仅标题",
         "summary": "", "key_points": []},
    ]))

    assert result.scenes[0]["description"] == "仅标题"


def test_key_points_only_without_body():
    """无 summary 也无 title 时，description 为要点列表。"""
    result = map_video_result(_mineru_result(chapters=[
        {"id": 0, "start": 0.0, "end": 1.0, "title": "", "summary": "",
         "key_points": ["要点一", "要点二"]},
    ]))

    assert result.scenes[0]["description"] == "- 要点一\n- 要点二"


# ── 修复轮次新增：诊断上下文与回落留痕 ──────────────────────────────


def _install_transport(monkeypatch, transport):
    monkeypatch.setattr(
        MineruVideoBackend, "_build_transport", lambda self: transport
    )


def test_submit_http_400_points_at_video_enabled(tmp_path, monkeypatch):
    """400（多为 VIDEO_ENABLED=false）必须在信息里点明该 env。"""
    transport = _FakeTransport(
        None,
        submit_error=RuntimeError(
            "MinerU self-host request failed: POST /tasks "
            "HTTP 400: Unsupported file type"
        ),
    )
    _install_transport(monkeypatch, transport)

    backend = MineruVideoBackend(config=None)
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(backend.analyze_video(str(_write_video(tmp_path))))

    message = str(excinfo.value)
    assert "VIDEO_ENABLED" in message
    # 原始信息保留，便于排障
    assert "Unsupported file type" in message
    assert "HTTP 400" in message
    # 指引里要提到改 env 后需重启容器（上传白名单 import 时定形）
    assert "重启" in message


def test_submit_non_400_error_passes_through_unwrapped(tmp_path, monkeypatch):
    """非 400 的 RuntimeError 原样透传，不被包装。"""
    original = RuntimeError(
        "MinerU self-host request failed: POST /tasks HTTP 503: boom"
    )
    transport = _FakeTransport(None, submit_error=original)
    _install_transport(monkeypatch, transport)

    backend = MineruVideoBackend(config=None)
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(backend.analyze_video(str(_write_video(tmp_path))))

    assert excinfo.value is original
    assert "VIDEO_ENABLED" not in str(excinfo.value)


def test_content_list_invalid_json_carries_raw_snippet(tmp_path, monkeypatch):
    """非法 JSON 要带 task_id 与原始响应片段（设计 §8）。"""
    transport = _FakeTransport("{not json at all")
    _install_transport(monkeypatch, transport)

    backend = MineruVideoBackend(config=None)
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(backend.analyze_video(str(_write_video(tmp_path))))

    message = str(excinfo.value)
    assert "t-1" in message
    assert "{not json at all" in message


def test_content_list_non_dict_reports_actual_type(tmp_path, monkeypatch):
    """解析结果是 list 时提前报错，而不是在 map_video_result 里 AttributeError。"""
    transport = _FakeTransport(json.dumps([{"type": "video"}]))
    _install_transport(monkeypatch, transport)

    backend = MineruVideoBackend(config=None)
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(backend.analyze_video(str(_write_video(tmp_path))))

    message = str(excinfo.value)
    assert "VideoResult" in message
    assert "list" in message


def test_backend_warns_when_video_base_url_falls_back(monkeypatch, caplog):
    """未设 MINERU_VIDEO_BASE_URL 时回落必须留痕（可能落到文档端点）。"""
    monkeypatch.delenv(BASE_URL_ENV, raising=False)
    monkeypatch.setenv(FALLBACK_BASE_URL_ENV, "http://127.0.0.1:8000")

    with caplog.at_level(logging.WARNING):
        backend = MineruVideoBackend(config=None)

    assert backend.base_url == "http://127.0.0.1:8000"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert BASE_URL_ENV in message
    assert "http://127.0.0.1:8000" in message


def test_backend_does_not_warn_when_video_base_url_explicit(monkeypatch, caplog):
    """显式设置时不打回落告警。"""
    monkeypatch.setenv(BASE_URL_ENV, "http://video:9000")

    with caplog.at_level(logging.WARNING):
        MineruVideoBackend(config=None)

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_prompt_is_not_forwarded_but_logged(tmp_path, monkeypatch, caplog):
    """per-KB prompt 不转发（不等于 coarse_prompt），但要留痕。"""
    monkeypatch.setenv(BASE_URL_ENV, "http://video:9000")
    monkeypatch.delenv("MINERU_VIDEO_COARSE_PROMPT", raising=False)
    transport = _FakeTransport(json.dumps(_mineru_result()))
    _install_transport(monkeypatch, transport)

    backend = MineruVideoBackend(config=None)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            backend.analyze_video(str(_write_video(tmp_path)), prompt="按知识库口径汇总")
        )

    assert "coarse_prompt" not in transport.submitted_fields
    backend_logs = [
        r
        for r in caplog.records
        if r.name == "raganything.video_analysis.backends.mineru"
        and r.levelno == logging.DEBUG
    ]
    assert len(backend_logs) == 1
    message = backend_logs[0].getMessage()
    assert "MINERU_VIDEO_COARSE_PROMPT" in message
    assert "未" in message and "转发" in message


def test_backend_registered_in_fresh_interpreter():
    """全新解释器只 import backends 包，验证 `__init__` 触发了注册。

    同进程内无法验证：本文件顶层已 import 过 mineru 子模块，装饰器早已执行，
    任何进程内断言都恒真（删掉 __init__.py 那行 import 也照样通过）。
    """  # [jonex]
    code = (
        "import raganything.video_analysis.backends\n"
        "from raganything.video_analysis import get_video_analysis_backend\n"
        "print(get_video_analysis_backend('mineru').__name__)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "MineruVideoBackend"


# ── 三类来源聚合进 scenes（chapter / slide / asr）──  # [jonex] 新增


def _multi_source_result():
    """含 chapters + slides + segments 三类来源的 VideoResult。"""
    return {
        "type": "video",
        "source": {"file_type": "video", "video_format": "mp4",
                   "original_filename": "demo.mp4"},
        "duration": 100.0,
        "timeline_mode": "speech",
        "video_mode": "text",
        "summary": {"overview": "总览", "topics": [], "lang": "zh"},
        "chapters": [
            {"id": 0, "start": 0.0, "end": 40.0, "title": "开场",
             "summary": "介绍背景", "key_points": []},
            {"id": 1, "start": 40.0, "end": 100.0, "title": "演示",
             "summary": "功能演示", "key_points": []},
        ],
        "slides": [
            {"page": 1, "start": 0.0, "end": 20.0, "text": "SLIDE ONE",
             "image": "images/slide-001.jpg"},
        ],
        "segments": [
            {"start": 0.0, "end": 10.0, "text": "大家好", "source": "speech",
             "chapter_id": 0},
            {"start": 10.0, "end": 20.0, "text": "欢迎收看", "source": "speech",
             "chapter_id": 0},
            {"start": 40.0, "end": 55.0, "text": "看这个功能", "source": "speech",
             "chapter_id": 1},
            {"start": 0.0, "end": 20.0, "text": "SLIDE ONE", "source": "slide",
             "slide_page": 1},
        ],
        "keyframe_ocr": {"enabled": False, "keyframes": []},
        "errors": [],
    }


def _by_source(scenes, source_type):
    return [s for s in scenes if s.get("source_type") == source_type]


def test_all_three_sources_land_in_scenes():
    """chapter / slide / asr 三类都进 scenes，且各自带 source_type。"""
    scenes = map_video_result(_multi_source_result()).scenes

    assert scenes is not None
    assert len(scenes) == 5
    assert len(_by_source(scenes, "chapter")) == 2
    assert len(_by_source(scenes, "slide")) == 1
    assert len(_by_source(scenes, "asr")) == 2


def test_chapters_carry_source_type():
    scenes = map_video_result(_multi_source_result()).scenes

    chapter = _by_source(scenes, "chapter")[0]
    assert "介绍背景" in chapter["description"]
    assert chapter["start_time"] == 0.0
    assert chapter["end_time"] == 40.0


def test_slides_carry_text_page_and_source_type():
    scenes = map_video_result(_multi_source_result()).scenes

    slide = _by_source(scenes, "slide")[0]
    assert slide["description"] == "SLIDE ONE"
    assert slide["start_time"] == 0.0
    assert slide["end_time"] == 20.0
    assert slide["slide_page"] == 1


def test_speech_segments_aggregated_by_chapter_id():
    """ASR 段按 chapter_id 归并：一章一条，描述拼接、时间取 min/max。"""
    scenes = map_video_result(_multi_source_result()).scenes

    asr = sorted(_by_source(scenes, "asr"), key=lambda s: s["start_time"])
    assert len(asr) == 2

    first = asr[0]
    assert first["description"] == "大家好 欢迎收看"
    assert first["start_time"] == 0.0
    assert first["end_time"] == 20.0
    assert first["chapter_id"] == 0

    second = asr[1]
    assert second["description"] == "看这个功能"
    assert second["start_time"] == 40.0
    assert second["end_time"] == 55.0


def test_slide_sourced_segments_are_skipped():
    """segments[source=slide] 与 slides[] 重复，不得二次入 scenes。"""
    scenes = map_video_result(_multi_source_result()).scenes

    assert len(_by_source(scenes, "slide")) == 1, "slide 只能来自 slides[]"
    assert all(
        s["description"] != "SLIDE ONE" for s in _by_source(scenes, "asr")
    )


def test_scenes_sorted_by_start_time():
    scenes = map_video_result(_multi_source_result()).scenes

    starts = [s["start_time"] for s in scenes]
    assert starts == sorted(starts)


def test_speech_without_chapter_id_grouped_together():
    """无 chapter_id 的语音段归为一组（兜底），不逐条散落。"""
    payload = _multi_source_result()
    payload["chapters"] = []
    payload["slides"] = []
    payload["segments"] = [
        {"start": 0.0, "end": 5.0, "text": "甲", "source": "speech"},
        {"start": 5.0, "end": 9.0, "text": "乙", "source": "speech"},
    ]

    scenes = map_video_result(payload).scenes

    assert scenes is not None
    assert len(scenes) == 1
    assert scenes[0]["source_type"] == "asr"
    assert scenes[0]["description"] == "甲 乙"


def test_blank_segment_text_does_not_create_empty_scene():
    """全空文本的语音段不产出空 scene。"""
    payload = _multi_source_result()
    payload["chapters"] = []
    payload["slides"] = []
    payload["segments"] = [
        {"start": 0.0, "end": 5.0, "text": "   ", "source": "speech",
         "chapter_id": 0},
    ]

    assert map_video_result(payload).scenes is None
