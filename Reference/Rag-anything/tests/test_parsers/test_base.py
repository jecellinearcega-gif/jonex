"""Tests for the Parser base class."""

from pathlib import Path

import pytest

from raganything.parsers.base import Parser, _pipe_table_header_prefix


class _ConcreteParser(Parser):
    """Minimal concrete subclass for testing base class behavior."""

    def check_installation(self) -> bool:
        return True

    def parse_document(self, file_path, **kwargs):
        return [{"type": "text", "text": "test"}]


class TestParserFormatConstants:
    def test_office_formats(self):
        assert ".docx" in Parser.OFFICE_FORMATS
        assert ".pptx" in Parser.OFFICE_FORMATS
        assert ".xlsx" in Parser.OFFICE_FORMATS
        assert ".pdf" not in Parser.OFFICE_FORMATS

    def test_image_formats(self):
        assert ".png" in Parser.IMAGE_FORMATS
        assert ".jpg" in Parser.IMAGE_FORMATS
        assert ".jpeg" in Parser.IMAGE_FORMATS

    def test_audio_formats(self):
        assert ".mp3" in Parser.AUDIO_FORMATS
        assert ".wav" in Parser.AUDIO_FORMATS

    def test_video_formats(self):
        assert ".mp4" in Parser.VIDEO_FORMATS
        assert ".avi" in Parser.VIDEO_FORMATS
        assert ".mov" in Parser.VIDEO_FORMATS


class TestParserIsUrl:
    def test_valid_http_url(self):
        assert Parser._is_url("http://example.com/file.pdf")

    def test_valid_https_url(self):
        assert Parser._is_url("https://example.com/file.pdf")

    def test_invalid_local_path(self):
        assert not Parser._is_url("/local/path/file.pdf")

    def test_invalid_relative_path(self):
        assert not Parser._is_url("relative/path/file.pdf")

    def test_empty_string(self):
        assert not Parser._is_url("")


class TestParserUniqueOutputDir:
    def test_returns_path_with_hash(self, tmp_path):
        result = Parser._unique_output_dir(str(tmp_path), "/path/to/document.pdf")
        assert isinstance(result, Path)
        assert str(tmp_path) in str(result)
        assert "document" in str(result)

    def test_different_files_different_dirs(self, tmp_path):
        r1 = Parser._unique_output_dir(str(tmp_path), "/path/to/a.pdf")
        r2 = Parser._unique_output_dir(str(tmp_path), "/path/to/b.pdf")
        assert r1 != r2


class TestParserParseAudio:
    def test_existing_audio_file(self, tmp_path):
        parser = _ConcreteParser()
        audio_file = tmp_path / "test.mp3"
        audio_file.write_bytes(b"fake audio content")

        result = parser.parse_audio(str(audio_file))
        assert len(result) == 1
        assert result[0]["type"] == "audio"
        assert "test.mp3" in result[0]["audio_path"]

    def test_nonexistent_audio_raises(self):
        parser = _ConcreteParser()
        with pytest.raises(FileNotFoundError):
            parser.parse_audio("/nonexistent/audio.mp3")

    def test_unsupported_format_raises(self, tmp_path):
        parser = _ConcreteParser()
        f = tmp_path / "test.xyz"
        f.write_bytes(b"fake")
        with pytest.raises(ValueError, match="Unsupported audio format"):
            parser.parse_audio(str(f))


class TestParserParseVideo:
    def test_existing_video_file(self, tmp_path):
        parser = _ConcreteParser()
        video_file = tmp_path / "test.mp4"
        video_file.write_bytes(b"fake video content")

        result = parser.parse_video(str(video_file))
        assert len(result) == 1
        assert result[0]["type"] == "video"
        assert "test.mp4" in result[0]["video_path"]

    def test_nonexistent_video_raises(self):
        parser = _ConcreteParser()
        with pytest.raises(FileNotFoundError):
            parser.parse_video("/nonexistent/video.mp4")

    def test_unsupported_format_raises(self, tmp_path):
        parser = _ConcreteParser()
        f = tmp_path / "test.xyz"
        f.write_bytes(b"fake")
        with pytest.raises(ValueError, match="Unsupported video format"):
            parser.parse_video(str(f))


class TestParserCheckInstallation:
    def test_base_raises_not_implemented(self):
        # Parser base class raises NotImplementedError
        with pytest.raises(NotImplementedError):
            Parser().check_installation()

    def test_concrete_subclass_works(self):
        parser = _ConcreteParser()
        assert parser.check_installation() is True


class TestPipeTableHeaderPrefix:
    """[jonex] §table-grid-v2: 超长 markdown 表段的表头前缀回填。"""

    def test_header_and_separator_detected(self):
        prefix = _pipe_table_header_prefix(
            "| 编号 | 名称 |\n| --- | --- |\n| 1 | A |"
        )
        assert prefix == "| 编号 | 名称 |\n| --- | --- |\n"

    def test_alignment_separator_detected(self):
        prefix = _pipe_table_header_prefix(
            "| a | b |\n| :--- | ---: |\n| 1 | 2 |"
        )
        assert prefix.startswith("| a | b |")

    def test_plain_paragraph_has_no_prefix(self):
        assert _pipe_table_header_prefix("普通段落\n第二行") == ""
        assert _pipe_table_header_prefix("| a | b |\n| 1 | 2 |") == ""


class TestParseTextLongTableSplitting:
    """[jonex] §table-grid-v2: 超长表段落按行切 + 每片回填表头。

    sdn.md 实测：12000 字符盲切把表切成 503 片，第 2 片起表头丢失、且首行常
    是被拦腰截断的残行（不以 `|` 开头），使 pipe 行连续段断裂、表头行与数据行
    分属两个 segment → 归一化只能给出 col_N 占位列名（真列名 19/6108）。
    """

    @staticmethod
    def _md(tmp_path, body: str):
        f = tmp_path / "t.md"
        f.write_text(body, encoding="utf-8")
        return str(f)

    def test_long_table_paragraph_backfills_header_per_piece(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("RAG_TEXT_CHUNK_CHARS", "200")
        rows = "\n".join(
            f"| {i:04d} | 产品{i} | 备注内容{i} |" for i in range(1, 60)
        )
        path = self._md(
            tmp_path,
            "| 编号 | 名称 | 备注 |\n| --- | --- | --- |\n" + rows + "\n",
        )
        chunks = _ConcreteParser().parse_text(path)
        assert len(chunks) > 1
        for c in chunks:
            assert c["text"].startswith("| 编号 | 名称 | 备注 |\n| --- | --- | --- |\n")
            # 行边界切分：表段落每行都以 `|` 开头，不存在残行
            assert all(
                line.startswith("|")
                for line in c["text"].split("\n") if line
            )

    def test_long_plain_paragraph_keeps_hard_cut(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAG_TEXT_CHUNK_CHARS", "200")
        path = self._md(tmp_path, "x" * 500)
        chunks = _ConcreteParser().parse_text(path)
        assert [len(c["text"]) for c in chunks] == [200, 200, 100]

    def test_pipe_paragraph_without_separator_keeps_hard_cut(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("RAG_TEXT_CHUNK_CHARS", "200")
        path = self._md(tmp_path, "| a | b |\n")  # 无分隔行 → 不识别为表头
        chunks = _ConcreteParser().parse_text(path)
        assert len(chunks) == 1
        assert chunks[0]["text"] == "| a | b |"
