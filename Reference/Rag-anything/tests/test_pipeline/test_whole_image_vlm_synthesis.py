"""独立图片 OCR 后整图 VLM 理解（§standalone-image-whole-vlm）单元测试。

覆盖（对应 docs/document-reference/standalone-image-whole-vlm-plan.md）：

- 纯 helper：_extract_ocr_text 合并/过滤/保序；_truncate_ocr 截断；
  _whole_image_vlm_enabled 开关；
- _synthesize_whole_image_item 守卫表（无 override / env 关 / 非图片扩展 /
  文件不存在 / processor 缺失或无 caption func / happy path / OCR 截断）；
- MultimodalStage(STANDALONE) 端到端：纯 OCR content_list + vision_prompt
  override → 合成整图 item → ImageModalProcessor 生成的最终 VLM prompt 同时
  含用户配置的提示词、OCR 参考说明段、OCR 文本；
- 未配置 override 时保持纯 OCR（无 VLM 调用）；
- fallback 路径剥离 _whole_image_ocr，OCR 文本不泄漏进兜底 chunk。
"""

import base64
import json
import types
from typing import Optional

import pytest

from raganything.modalprocessors import (
    ImageModalProcessor,
    _OCR_REFERENCE_HEADER,
    _WHOLE_IMAGE_OCR_FIELD,
    _append_ocr_reference,
)
from raganything.pipeline.base import PipelineContext, PipelineServices
from raganything.pipeline.stages import (
    MultimodalStage,
    _extract_ocr_text,
    _synthesize_whole_image_item,
    _truncate_ocr,
    _whole_image_vlm_enabled,
)
from raganything.pipeline_mode import PipelineMode
from raganything.service.prompt_integration import PromptOverride

# 1×1 透明 PNG，仅用于满足 generate_description_only 的文件存在与 base64 编码。
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

_OCR_LINES = [
    {"type": "text", "page_idx": 0, "text": "第二行 单价 100元"},
    {"type": "text", "page_idx": 0, "text": "第一行 合同编号 A001"},
]
_USER_PROMPT = "你是资深财务，分析这张发票图片中的关键字段"


class _NoCtx:
    """空 ContextExtractor 替身：提取上下文恒为空，避免测试日志噪音。"""

    def extract_context(self, *args, **kwargs):
        return ""


def _make_services(processor=None, config=None) -> PipelineServices:
    processors = {"image": processor} if processor is not None else {}
    cfg = config or types.SimpleNamespace(
        max_parallel_multimodal=2,
        max_parallel_vlm=2,
        enable_image_processing=True,
    )
    return PipelineServices(
        config=cfg,
        lightrag=None,
        doc_parser=None,
        modal_processors=processors,
    )


def _make_ctx(tmp_path, name: str = "invoice.png", prompt: Optional[str] = _USER_PROMPT):
    """构造带 OCR 文本 content_list + vision_prompt override 的最小 ctx。"""
    png = tmp_path / name
    png.write_bytes(_PNG_1PX)
    ctx = PipelineContext(file_path=str(png), file_name=name)
    ctx.content_list = [dict(row) for row in _OCR_LINES]
    ctx.prompt_overrides = (
        PromptOverride(by_code={"vision_prompt": prompt}) if prompt else None
    )
    return ctx


def _new_image_processor(caption_func) -> ImageModalProcessor:
    # 跳过 BaseModalProcessor.__init__（需要真实 LightRAG dataclass），
    # 只装配本特性依赖的属性。同 test_context_injection_f0.py 的做法。
    proc = object.__new__(ImageModalProcessor)
    proc.modal_caption_func = caption_func
    proc.content_source = None
    proc.content_format = "minerU"
    proc.context_extractor = _NoCtx()
    return proc


# ── 纯 helper ────────────────────────────────────────────────────────────


def test_extract_ocr_text_merges_text_only_in_order():
    content = [
        {"type": "image", "img_path": "x.png"},          # 非 text 忽略
        {"type": "text", "page_idx": 0, "text": "第二行 单价 100元"},
        {"type": "text", "page_idx": 0, "text": "  第一行 合同编号 A001  "},  # strip
        {"type": "text", "page_idx": 0, "text": ""},      # 空忽略
        "not-a-dict",                                    # 非 dict 忽略
    ]
    assert _extract_ocr_text(content) == "第二行 单价 100元\n第一行 合同编号 A001"


def test_extract_ocr_text_empty_inputs():
    assert _extract_ocr_text(None) == ""
    assert _extract_ocr_text([]) == ""
    assert _extract_ocr_text([{"type": "table", "text": "x"}]) == ""


def test_truncate_ocr_short_passthrough():
    assert _truncate_ocr("abc", 10) == "abc"
    assert _truncate_ocr("", 5) == ""


def test_truncate_ocr_over_max_marks_cut():
    text = "x" * 100
    out = _truncate_ocr(text, 10)
    assert out.startswith("x" * 10)
    assert out.endswith("…(OCR 内容过长，已截断)")
    assert len(out) < len(text)


def test_whole_image_vlm_enabled_env_and_config(monkeypatch):
    assert _whole_image_vlm_enabled(None) is True
    monkeypatch.setenv("RAG_IMAGE_FULL_VLM_ENABLED", "false")
    assert _whole_image_vlm_enabled(None) is False
    monkeypatch.setenv("RAG_IMAGE_FULL_VLM_ENABLED", "true")
    assert (
        _whole_image_vlm_enabled(
            types.SimpleNamespace(enable_image_processing=False)
        )
        is False
    )
    assert _whole_image_vlm_enabled(
        types.SimpleNamespace(enable_image_processing=True)
    ) is True


def test_append_ocr_reference_appends_once(tmp_path):
    prompt = "分析图片"
    out = _append_ocr_reference(prompt, "合同编号 A001")
    assert _OCR_REFERENCE_HEADER in out
    assert "合同编号 A001" in out
    assert out.startswith(prompt)
    # 防重：已含 header 时不再追加
    assert _append_ocr_reference(out, "更多 OCR") == out


def test_append_ocr_reference_empty_noop():
    assert _append_ocr_reference("提示词", "") == "提示词"
    assert _append_ocr_reference("提示词", "   ") == "提示词"


# ── _synthesize_whole_image_item 守卫 ───────────────────────────────────


def _synth(ctx, services):
    return _synthesize_whole_image_item(ctx, services)


def test_synthesize_requires_vision_prompt_override(tmp_path):
    ctx = _make_ctx(tmp_path)
    ctx.prompt_overrides = None
    services = _make_services(
        processor=types.SimpleNamespace(modal_caption_func=object())
    )
    assert _synth(ctx, services) is None

    ctx.prompt_overrides = PromptOverride(by_code={"image_prompt": "x"})
    assert _synth(ctx, services) is None


def test_synthesize_env_disabled_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_FULL_VLM_ENABLED", "false")
    ctx = _make_ctx(tmp_path)
    services = _make_services(
        processor=types.SimpleNamespace(modal_caption_func=object())
    )
    assert _synth(ctx, services) is None


def test_synthesize_cfg_image_processing_disabled_returns_none(tmp_path):
    ctx = _make_ctx(tmp_path)
    services = _make_services(
        processor=types.SimpleNamespace(modal_caption_func=object()),
        config=types.SimpleNamespace(
            max_parallel_multimodal=2,
            max_parallel_vlm=2,
            enable_image_processing=False,
        ),
    )
    assert _synth(ctx, services) is None


def test_synthesize_rejects_non_supported_extension(tmp_path):
    ctx = _make_ctx(tmp_path, name="invoice.pdf")
    services = _make_services(
        processor=types.SimpleNamespace(modal_caption_func=object())
    )
    assert _synth(ctx, services) is None


def test_synthesize_rejects_missing_file(tmp_path):
    ctx = _make_ctx(tmp_path, name="ghost.png")
    (tmp_path / "ghost.png").unlink()  # 写后删除 → 存在性校验失败
    services = _make_services(
        processor=types.SimpleNamespace(modal_caption_func=object())
    )
    assert _synth(ctx, services) is None


def test_synthesize_requires_bound_vlm(tmp_path):
    ctx = _make_ctx(tmp_path)
    # 无 processor
    assert _synth(ctx, _make_services(processor=None)) is None
    # processor 存在但 modal_caption_func 未绑定
    assert (
        _synth(
            ctx,
            _make_services(
                processor=types.SimpleNamespace(modal_caption_func=None)
            ),
        )
        is None
    )


def test_synthesize_happy_path(tmp_path):
    ctx = _make_ctx(tmp_path)
    services = _make_services(
        processor=types.SimpleNamespace(modal_caption_func=object())
    )
    item = _synth(ctx, services)
    assert item is not None
    assert item["type"] == "image"
    assert item["img_path"] == str(tmp_path / "invoice.png")
    assert item["file_name"] == "invoice.png"
    assert item["page_idx"] == 0
    assert item[_WHOLE_IMAGE_OCR_FIELD] == "第二行 单价 100元\n第一行 合同编号 A001"
    # 有意不携带 bbox/caption：anchor 提取短路为空
    assert "bbox" not in item
    assert "img_caption" not in item


def test_synthesize_truncates_ocr(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_FULL_VLM_OCR_MAX_CHARS", "10")
    ctx = _make_ctx(tmp_path)
    ctx.content_list = [
        {"type": "text", "page_idx": 0, "text": "哈" * 40},
    ]
    services = _make_services(
        processor=types.SimpleNamespace(modal_caption_func=object())
    )
    item = _synth(ctx, services)
    assert item is not None
    ocr = item[_WHOLE_IMAGE_OCR_FIELD]
    assert "…(OCR 内容过长，已截断)" in ocr
    assert ocr.startswith("哈" * 10)


# ── MultimodalStage(STANDALONE) 端到端 ─────────────────────────────────


@pytest.mark.asyncio
async def test_stage_synthesizes_and_injects_ocr_into_vlm_prompt(tmp_path):
    captured = []

    async def fake_caption(prompt, image_data=None, system_prompt=None):
        captured.append(prompt)
        return json.dumps(
            {
                "detailed_description": "合同关键信息图片描述",
                "entity_info": {
                    "entity_name": "合同页",
                    "entity_type": "image",
                    "summary": "合同内容",
                },
            }
        )

    ctx = _make_ctx(tmp_path)
    proc = _new_image_processor(fake_caption)
    services = _make_services(processor=proc)
    stage = MultimodalStage(PipelineMode.STANDALONE)
    result = await stage.execute(ctx, services)

    assert len(result.multimodal_results) == 1
    assert result.multimodal_results[0]["description"] == "合同关键信息图片描述"
    assert len(captured) == 1
    prompt = captured[0]
    # 用户配置的提示词 + OCR 说明段 + OCR 内容 三者齐备
    assert _USER_PROMPT in prompt
    assert _OCR_REFERENCE_HEADER in prompt
    assert "合同编号 A001" in prompt
    assert "单价 100元" in prompt


@pytest.mark.asyncio
async def test_stage_without_override_stays_pure_ocr(tmp_path):
    called = []

    async def fake_caption(prompt, image_data=None, system_prompt=None):
        called.append(prompt)
        return json.dumps(
            {
                "detailed_description": "不应调用",
                "entity_info": {
                    "entity_name": "x",
                    "entity_type": "image",
                    "summary": "x",
                },
            }
        )

    ctx = _make_ctx(tmp_path, prompt=None)  # 未配置 vision_prompt override
    proc = _new_image_processor(fake_caption)
    services = _make_services(processor=proc)
    stage = MultimodalStage(PipelineMode.STANDALONE)
    result = await stage.execute(ctx, services)

    assert not (result.multimodal_results or [])
    assert called == []  # 纯 OCR：VLM 未被调用


@pytest.mark.asyncio
async def test_fallback_strips_whole_image_ocr(tmp_path):
    async def boom(prompt, image_data=None, system_prompt=None):
        raise RuntimeError("vlm down")

    proc = _new_image_processor(boom)
    png = tmp_path / "invoice.png"
    png.write_bytes(_PNG_1PX)
    content = {
        "type": "image",
        "img_path": str(png),
        "file_name": "invoice.png",
        "page_idx": 0,
        _WHOLE_IMAGE_OCR_FIELD: "合同编号 A001 金额 100 元 这行不应出现在兜底结果里",
    }
    desc, entity = await proc.generate_description_only(
        modal_content=content,
        content_type="image",
        item_info=content,
        prompt_overrides=PromptOverride(by_code={"vision_prompt": _USER_PROMPT}),
    )
    # 兜底 desc = 剥离私有字段的副本 → OCR 文本与说明 header 均不泄漏
    assert _OCR_REFERENCE_HEADER not in desc
    assert "合同编号 A001" not in desc
    assert "不应出现在兜底结果里" not in desc
