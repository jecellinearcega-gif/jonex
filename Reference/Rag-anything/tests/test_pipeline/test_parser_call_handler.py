"""回归测试：`_call_handler` 不得向不接受 `method` 形参的 bound fallback handler 注入 method。

线上故障：上传独立图片（.jpg）走本地 `MineruParser` 时解析失败
  TypeError: MineruParser._run_mineru_command() got multiple values for
  keyword argument 'method'
根因：`_default_parser_registry` 给图片注册了 handler `Parser.parse_image`
（基类会抛 NotImplementedError）+ fallback `MineruParser().parse_image`
（bound）。当 primary 失败落到 fallback 时，`_call_handler` 的 bound 分支
无条件调用 `handler(str(path), output_dir=..., method=parse_method)`，把
`method` 硬塞给签名只有 (self, image_path, output_dir, lang, **kwargs) 的
`MineruParser.parse_image`。`method` 落进 **kwargs 后与 parse_image 内部显式
的 `_run_mineru_command(method="ocr", **kwargs)` 双值冲突。

修复：bound / unbound 分支统一走签名检查，仅当 handler 声明 `method` 形参
时才透传；另给本地 `MineruParser.parse_image` 补 `kwargs.pop("method", None)`
与 MineruOnlineParser / MineruSelfHostParser 对齐。
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from raganything.pipeline.stages import _call_handler  # noqa: E402
from raganything.parsers.base import Parser  # noqa: E402
from raganything.parsers.mineru import MineruParser  # noqa: E402


def _make_fixture(tmp_path, ext=".jpg"):
    """返回真实存在的空文件 + 本地 MinerU parser 实例。"""
    f = tmp_path / ("image" + ext)
    f.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)
    return f, MineruParser()


class TestCallHandlerMethodInjection:
    def test_bound_parse_image_no_method_collision(self, tmp_path):
        """bound fallback（MineruParser().parse_image）签名无 method 形参，
        不得被注入 method；其内部 _run_mineru_command(method='ocr', **kwargs)
        不再双值冲突。"""
        f, parser = _make_fixture(tmp_path)
        with patch.object(MineruParser, "_run_mineru_command", return_value=[]) as m:
            result = _call_handler(
                parser.parse_image,  # bound
                parser,
                f,
                str(tmp_path),
                "ocr",
            )
        assert m.call_count == 1
        # _run_mineru_command 只收到一份 method='ocr'（由 parse_image 内部显式传入）
        assert m.call_args.kwargs["method"] == "ocr"
        assert isinstance(result, list)

    def test_unbound_parse_image_resolves_to_subclass(self, tmp_path):
        """unbound Parser.parse_image 解析到实例方法（签名无 method）也不注入。"""
        f, parser = _make_fixture(tmp_path)
        with patch.object(MineruParser, "_run_mineru_command", return_value=[]) as m:
            _call_handler(Parser.parse_image, parser, f, str(tmp_path), "ocr")
        assert m.call_count == 1
        assert m.call_args.kwargs["method"] == "ocr"

    def test_method_declared_handler_still_gets_method(self, tmp_path):
        """签名带 method 形参的 handler（parse_pdf）仍应透传，行为不回归。"""
        f, parser = _make_fixture(tmp_path, ext=".pdf")
        f.write_bytes(b"%PDF-1.4\n")
        with patch.object(MineruParser, "_run_mineru_command", return_value=[]) as m:
            _call_handler(Parser.parse_pdf, parser, f, str(tmp_path), "ocr")
        assert m.call_count == 1
        assert m.call_args.kwargs["method"] == "ocr"
