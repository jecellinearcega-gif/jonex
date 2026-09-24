"""[jonex] office 查看原文定位：preview.pdf 上传（stages.upload_office_preview）。

覆盖本次修复的关键不变量（背景：preview.pdf 原先只在 full/HTTP pipeline 的
OfficePreviewUploadStage 里产出，走 parse_only（OpenKB 管线）的 KB 永远没有
preview.pdf，查看原文回退下载原始 office 文件、弹窗里没有预览）：

- full 与 parse_only 两条链路共用同一实现
  （OfficePreviewUploadStage.execute 与 TaskManager._upload_office_preview
   都调 upload_office_preview），gate 与「复用已转换 PDF」语义只有一份；
- 解析侧已落 {output_dir}/{stem}.pdf 时直接复用，不再调 LibreOffice；
- 非 office 扩展名 / gate 关闭 / jonex_core 缺失 / 无 doc 锚点 → 返回 False；
- 转换或上传失败只影响返回值，绝不上抛（best-effort，不阻塞主链路）。

用例同步调用 asyncio.run，不依赖 pytest-asyncio。
"""

import asyncio
import sys
from unittest import mock

import pytest

from raganything.pipeline.stages import (
    OFFICE_PREVIEW_EXTS,
    ensure_office_preview_pdf,
    upload_office_preview,
)


@pytest.fixture(autouse=True)
def _gate_default_on(monkeypatch):
    """默认开启（生产口径），用例内再显式关闭做对照。"""
    monkeypatch.setenv("OFFICE_PREVIEW_UPLOAD_ENABLED", "true")


@pytest.fixture
def fake_obj_storage():
    """伪造 jonex_core.common.object_storage，隔离平台侧真实依赖。

    注入 sys.modules 使函数内的延迟 import 命中假模块（同
    test_asset_upload_stage.py 的做法）；键的真实拼装口径由
    build_preview_key 单测覆盖，此处只关心调用参数与返回结果。
    """
    fake = mock.MagicMock(name="jonex_core.common.object_storage")
    storage = mock.AsyncMock(name="storage")
    fake.get_object_storage.return_value = storage
    fake.build_preview_key.side_effect = (
        lambda tenant, kb, doc: f"jonex/kb/{tenant}/{kb}/{doc}/preview.pdf"
    )
    with mock.patch.dict(sys.modules, {"jonex_core.common.object_storage": fake}):
        yield fake, storage


def _call(tmp_path, **overrides):
    """按 upload_office_preview 的默认实参发起调用，用例只覆盖关心的字段。"""
    kwargs = {
        "file_path": str(tmp_path / "src.xlsx"),
        "file_name": "2025年5月菜单.xlsx",
        "tenant_id": "t1",
        "kb_id": "kb1",
        "doc_anchor": "doc-1",
        "output_dir": str(tmp_path),
        "logger": mock.MagicMock(),
    }
    kwargs.update(overrides)
    return asyncio.run(upload_office_preview(**kwargs))


def test_office_preview_exts_matches_parser_whitelist():
    assert OFFICE_PREVIEW_EXTS == {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}


def test_non_office_extension_skipped(tmp_path, fake_obj_storage):
    """非 office 文档整体跳过：不转换、不上传。"""
    fake, storage = fake_obj_storage
    converter = mock.MagicMock()
    with mock.patch(
        "raganything.parsers.conversion.convert_office_to_pdf", converter
    ):
        assert _call(tmp_path, file_name="说明.txt") is False
    converter.assert_not_called()
    storage.put_bytes.assert_not_called()


def test_gate_disabled_skips(tmp_path, fake_obj_storage, monkeypatch):
    monkeypatch.setenv("OFFICE_PREVIEW_UPLOAD_ENABLED", "false")
    _, storage = fake_obj_storage
    assert _call(tmp_path) is False
    storage.put_bytes.assert_not_called()


def test_reuses_existing_converted_pdf(tmp_path, fake_obj_storage):
    """解析侧已落 {output_dir}/{stem}.pdf → 直接复用，不再调 LibreOffice。

    这是 parse_only 之外的常见路径（本地 mineru 解析会把同一 PDF 落在产物目录，
    page_idx 与该 PDF 天然对齐）。
    """
    _, storage = fake_obj_storage
    existing = tmp_path / "src.pdf"
    existing.write_bytes(b"%PDF-1.7 fake")
    converter = mock.MagicMock()
    with mock.patch(
        "raganything.parsers.conversion.convert_office_to_pdf", converter
    ):
        assert _call(tmp_path) is True
    converter.assert_not_called()
    assert asyncio.run(ensure_office_preview_pdf(str(tmp_path / "src.xlsx"), str(tmp_path))) == str(existing)


def test_converts_and_uploads_with_preview_key(tmp_path, fake_obj_storage):
    """无现成 PDF 时现转，并上传到 build_preview_key 的键上、content_type 为 pdf。"""
    fake, storage = fake_obj_storage
    generated = tmp_path / "generated.pdf"
    generated.write_bytes(b"%PDF-1.7 generated")
    converter = mock.MagicMock(return_value=generated)
    with mock.patch(
        "raganything.parsers.conversion.convert_office_to_pdf", converter
    ):
        assert _call(tmp_path) is True
    converter.assert_called_once()
    key, data = storage.put_bytes.call_args.args
    assert key == "jonex/kb/t1/kb1/doc-1/preview.pdf"
    assert data == b"%PDF-1.7 generated"
    assert storage.put_bytes.call_args.kwargs["content_type"] == "application/pdf"


def test_missing_doc_anchor_skips_upload(tmp_path, fake_obj_storage):
    """无 document_id/doc_id 锚点 → 不上传（上传键的 doc 段会与检索侧推导分叉）。"""
    _, storage = fake_obj_storage
    (tmp_path / "src.pdf").write_bytes(b"%PDF-1.7 fake")
    assert _call(tmp_path, doc_anchor="") is False
    storage.put_bytes.assert_not_called()


def test_conversion_failure_is_best_effort(tmp_path, fake_obj_storage):
    """LibreOffice 缺失/转换失败 → 返回 False，不抛异常。"""
    _, storage = fake_obj_storage
    converter = mock.MagicMock(side_effect=RuntimeError("LibreOffice not installed"))
    with mock.patch(
        "raganything.parsers.conversion.convert_office_to_pdf", converter
    ):
        assert _call(tmp_path) is False
    storage.put_bytes.assert_not_called()


def test_missing_jonex_core_is_best_effort(tmp_path):
    """非平台环境（jonex_core 未打包）→ 静默跳过，不抛异常。"""
    (tmp_path / "src.pdf").write_bytes(b"%PDF-1.7 fake")
    with mock.patch.dict(sys.modules, {"jonex_core.common.object_storage": None}):
        assert _call(tmp_path) is False


def test_upload_failure_is_best_effort(tmp_path, fake_obj_storage):
    """上传抛错（如对象存储不可用）→ 返回 False，不冒泡到调用方。"""
    _, storage = fake_obj_storage
    storage.put_bytes.side_effect = OSError("storage down")
    (tmp_path / "src.pdf").write_bytes(b"%PDF-1.7 fake")
    assert _call(tmp_path) is False
