"""Base parser class for document parsing utilities.

Provides common functionality, format constants, and abstract interface
for all document parsers in the RAG-Anything system.
"""

import hashlib
import logging
import mimetypes
import os
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


def _is_sep_cell(cell: str) -> bool:
    """markdown 表分隔行单元格（``---`` / ``:--`` / ``--:``）。"""
    body = cell.strip()
    if body.startswith(":"):
        body = body[1:]
    if body.endswith(":"):
        body = body[:-1]
    return bool(body) and all(ch == "-" for ch in body)


def _pipe_table_header_prefix(text: str) -> str:
    """[jonex] §table-grid-v2: 取段落开头的 markdown 表头（表头行 + 分隔行）。

    仅用于超长段落硬切后的片段回填；不满足「首行 pipe 行 + 次行分隔行」时返回空串。
    """
    lines = text.split("\n", 2)
    if len(lines) < 2:
        return ""
    head, sep = lines[0].strip(), lines[1].strip()
    if not (head.startswith("|") and head.endswith("|")) or "|" not in sep:
        return ""
    if not all(_is_sep_cell(c) for c in sep.strip("|").split("|")):
        return ""
    return head + "\n" + sep + "\n"


class Parser:
    """Base class for document parsing utilities."""

    # [jonex] Office 全量（含 legacy OLE2）：提交给自建 MinerU（jonex-gpu :20490 fork）时
    # 一律带 multipart 字段 `convert_office_to_pdf=true`（见 `MineruSelfHostParser._submit_task`）。
    # 起因：该服务的 active 档位 `jonex-default` 的 file_types **不含** `legacy_office`，
    # legacy 三件套原样上传必被 400 拒：
    #   {"detail":"Unsupported file type: xls（legacy Office 需要 convert_office_to_pdf=true）"}
    # 该字段的实测语义是**放行（permission）、不是强制转换**：带上之后 `.xls` / `.doc` 的产物
    # `converted_pdf_url` 仍为空 = 服务端**原生解析**、没走 PDF 中转，且内容正确
    # （`.xls` md 281 / cl 415、`.doc` md 266 / cl 560）；给真 PDF 带该字段结果完全不变。
    # 故对整个 Office 家族统一携带 —— 对能原生解析的新格式是无副作用的 no-op。
    # ⚠️ 别改成下发档位 `profile=office-to-pdf`（那条路才是**强制转换**）：实测它 202 受理、
    # 任务 completed、`converted_pdf_url` 有值、转出的 PDF 内容完好（pdftotext 可读），但
    # `md_content` 与 `content_list` **都是空的** —— 静默产出空结果，比 400 危险；
    # 新格式加它同样变空（`.xlsx` md 281 → 0）。
    OFFICE_FORMATS = {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
    IMAGE_FORMATS = {".png", ".jpeg", ".jpg", ".bmp", ".tiff", ".tif", ".gif", ".webp"}
    TEXT_FORMATS = {".txt", ".md", ".markdown", ".json", ".jsonl"}
    # 纯文本切块参数（parse_text 用；对齐 v1 _read_plain_text_blocks）
    TEXT_CHUNK_CHARS_DEFAULT = 12000
    TEXT_FILE_ENCODINGS = ("utf-8", "utf-8-sig", "gb18030", "gbk", "latin-1")
    AUDIO_FORMATS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".wma", ".aac", ".opus", ".amr"}
    VIDEO_FORMATS = {
        ".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv",
        ".webm", ".m4v", ".mpg", ".mpeg", ".3gp",
    }

    logger = logging.getLogger(__name__)

    @staticmethod
    def _is_url(path: str) -> bool:
        try:
            result = urllib.parse.urlparse(str(path))
            return all([result.scheme, result.netloc])
        except ValueError:
            return False

    def _download_file(self, url: str) -> Path:
        tmp_path = None
        response = None
        try:
            self.logger.info(f"Downloading file from URL: {url}")
            parsed_url = urllib.parse.urlparse(url)
            path = Path(parsed_url.path)
            suffix = path.suffix if path.suffix else ""
            req = urllib.request.Request(
                url,
                data=None,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/91.0.4472.114 Safari/537.36"
                },
            )
            response = urllib.request.urlopen(req, timeout=30)
            if not suffix:
                content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
                if content_type:
                    guessed_ext = mimetypes.guess_extension(content_type)
                    if guessed_ext:
                        suffix = guessed_ext
                        self.logger.info(
                            f"Inferred file extension '{suffix}' from Content-Type: {content_type}"
                        )
            fd, tmp_path = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            tmp_path_obj = Path(tmp_path)
            with open(tmp_path_obj, "wb") as out_file:
                shutil.copyfileobj(response, out_file)
            self.logger.info(
                f"Downloaded to temporary file: {tmp_path_obj} ({tmp_path_obj.stat().st_size} bytes)"
            )
            return tmp_path_obj
        except Exception as e:
            error_tmp = tmp_path
            if error_tmp and Path(error_tmp).exists():
                try:
                    Path(error_tmp).unlink()
                except Exception:
                    pass
            self.logger.error(f"Failed to download file from {url}: {e}")
            raise RuntimeError(f"Failed to download file from {url}: {e}") from e
        finally:
            if response:
                response.close()

    def __init__(self) -> None:
        pass

    @staticmethod
    def _unique_output_dir(base_dir: Union[str, Path], file_path: Union[str, Path]) -> Path:
        file_path_resolved = Path(file_path).resolve()
        stem = file_path_resolved.stem
        path_hash = hashlib.md5(str(file_path_resolved).encode()).hexdigest()[:8]
        return Path(base_dir) / f"{stem}_{path_hash}"

    # ── Abstract / placeholder methods ─────────────────────────────────

    def parse_pdf(
        self,
        pdf_path: Union[str, Path],
        output_dir: Optional[str] = None,
        method: str = "auto",
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """Parse PDF document. Must be implemented by subclasses."""
        raise NotImplementedError("parse_pdf must be implemented by subclasses")

    def parse_image(
        self,
        image_path: Union[str, Path],
        output_dir: Optional[str] = None,
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """Parse image document. Must be implemented by subclasses."""
        raise NotImplementedError("parse_image must be implemented by subclasses")

    def parse_document(
        self,
        file_path: Union[str, Path],
        method: str = "auto",
        output_dir: Optional[str] = None,
        lang: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """Parse a document. Must be implemented by subclasses."""
        raise NotImplementedError("parse_document must be implemented by subclasses")

    def parse_audio(
        self,
        audio_path: Union[str, Path],
        output_dir: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """Parse audio file — minimal: just records the path. ASR done by AsrModalProcessor."""
        audio_path_resolved = Path(audio_path)
        if not audio_path_resolved.exists():
            raise FileNotFoundError(f"Audio file does not exist: {audio_path}")
        ext = audio_path_resolved.suffix.lower()
        if ext not in self.AUDIO_FORMATS:
            raise ValueError(
                f"Unsupported audio format: {ext}. "
                f"Supported formats: {', '.join(sorted(self.AUDIO_FORMATS))}"
            )
        return [{
            "type": "audio",
            "audio_path": str(audio_path_resolved.resolve()),
            "file_name": audio_path_resolved.name,
        }]

    def parse_video(
        self,
        video_path: Union[str, Path],
        output_dir: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """Parse video file — minimal: just records the path. Processing by VideoModalProcessor."""
        video_path_resolved = Path(video_path)
        if not video_path_resolved.exists():
            raise FileNotFoundError(f"Video file does not exist: {video_path}")
        ext = video_path_resolved.suffix.lower()
        if ext not in self.VIDEO_FORMATS:
            raise ValueError(
                f"Unsupported video format: {ext}. "
                f"Supported formats: {', '.join(sorted(self.VIDEO_FORMATS))}"
            )
        return [{
            "type": "video",
            "video_path": str(video_path_resolved.resolve()),
            "file_name": video_path_resolved.name,
        }]

    def parse_text(
        self,
        file_path: Union[str, Path],
        output_dir: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """Parse plain text (.txt/.md) into content_list, bypassing MinerU/OCR.

        纯文本按空行分段切块，单段超长按 RAG_TEXT_CHUNK_CHARS 硬切，并回填字符位置锚点。
        """
        text_path = Path(file_path)
        if not text_path.exists():
            raise FileNotFoundError(f"Text file does not exist: {file_path}")
        ext = text_path.suffix.lower()
        if ext not in self.TEXT_FORMATS:
            raise ValueError(
                f"Unsupported text format: {ext}. "
                f"Supported formats: {', '.join(sorted(self.TEXT_FORMATS))}"
            )
        content = text_path.read_text(encoding="utf-8", errors="ignore")
        try:
            max_chars = int(os.getenv("RAG_TEXT_CHUNK_CHARS", "12000"))
        except ValueError:
            max_chars = 12000
        blocks: List[Dict[str, Any]] = []
        pos = 0
        for para in content.split("\n\n"):
            seg = para.strip()
            if seg:
                for i in range(0, len(seg), max_chars):
                    chunk = seg[i:i + max_chars]
                    blocks.append({
                        "type": "text",
                        "text": chunk,
                        "char_start": pos + i,
                        "char_end": pos + i + len(chunk),
                    })
            pos += len(para) + 2  # 补回 split 掉的 "\n\n"
        if not blocks and content.strip():
            blocks = [{"type": "text", "text": content.strip()}]
        return blocks

    def parse_text(
        self,
        file_path: Union[str, Path],
        output_dir: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """Parse plain-text files (.txt/.md/.markdown) directly — bypass MinerU/VLM.

        Splits on blank lines into <= RAG_TEXT_CHUNK_CHARS chunks and backfills
        char_start/char_end offsets. Mirrors v1 ``_read_plain_text_blocks`` so
        text files are not sent to MinerU (which rejects them / turns them to PDF+VLM).
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Text file does not exist: {file_path}")

        try:
            max_chars = int(os.getenv("RAG_TEXT_CHUNK_CHARS", str(self.TEXT_CHUNK_CHARS_DEFAULT)))
        except ValueError:
            max_chars = self.TEXT_CHUNK_CHARS_DEFAULT
        if max_chars <= 0:
            max_chars = self.TEXT_CHUNK_CHARS_DEFAULT

        text = ""
        last_err: Optional[UnicodeDecodeError] = None
        for enc in self.TEXT_FILE_ENCODINGS:
            try:
                with open(path, "r", encoding=enc) as fh:
                    text = fh.read()
                break
            except UnicodeDecodeError as exc:
                last_err = exc
            except OSError as exc:
                raise ValueError(f"读取文本文件失败: {file_path}: {exc}") from exc
        else:
            raise ValueError(f"文本文件编码无法识别: {file_path}: {last_err}") from last_err

        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            return []

        # 段落列表（含在归一化文本中的字符位置）
        paragraphs: List[Dict[str, Any]] = []
        pos = 0
        for para in text.split("\n\n"):
            stripped = para.strip()
            para_len = len(para)
            if stripped:
                leading_ws = para_len - len(para.lstrip())
                paragraphs.append({
                    "text": stripped,
                    "char_start": pos + leading_ws,
                    "char_end": pos + leading_ws + len(stripped),
                })
            pos += para_len + 2  # +2 for the "\n\n" separator

        chunks: List[Dict[str, Any]] = []
        current_parts: List[str] = []
        current_len = 0
        current_start: Optional[int] = None

        for p in paragraphs:
            if len(p["text"]) > max_chars:
                if current_parts:
                    chunks.append({
                        "type": "text",
                        "text": "\n\n".join(current_parts),
                        "char_start": current_start,
                        "char_end": current_start + current_len if current_start is not None else None,
                    })
                    current_parts, current_len, current_start = [], 0, None
                # [jonex] §table-grid-v2: 超长 markdown 表段落被按字符硬切后，
                # 第 2 片起只剩数据行 → 归一化只能回退 col_N 占位列名（sdn.md
                # 实测 502/503 片如此）。此处两件事：①按行边界切，避免把表格行
                # 拦腰截断（截断后的残行不以 `|` 开头，会打断 pipe 行连续段，
                # 让表头行与数据行分属两个 segment）；②把「表头行 + 分隔行」
                # 前缀回填到后续片段，使每片都能还原真实列名。
                table_prefix = _pipe_table_header_prefix(p["text"])
                start = 0
                while start < len(p["text"]):
                    end = min(start + max_chars, len(p["text"]))
                    if table_prefix and end < len(p["text"]):
                        nl = p["text"].rfind("\n", start, end)
                        if nl > start:
                            end = nl + 1
                    text_piece = p["text"][start:end]
                    if table_prefix and start > 0:
                        text_piece = table_prefix + text_piece
                    chunks.append({
                        "type": "text",
                        "text": text_piece,
                        "char_start": p["char_start"] + start,
                        "char_end": p["char_start"] + end,
                    })
                    start = end
                continue

            next_len = current_len + len(p["text"]) + (2 if current_parts else 0)
            if current_parts and next_len > max_chars:
                chunks.append({
                    "type": "text",
                    "text": "\n\n".join(current_parts),
                    "char_start": current_start,
                    "char_end": (current_start + current_len) if current_start is not None else None,
                })
                current_parts = [p["text"]]
                current_len = len(p["text"])
                current_start = p["char_start"]
            else:
                if not current_parts:
                    current_start = p["char_start"]
                current_parts.append(p["text"])
                current_len = next_len

        if current_parts:
            chunks.append({
                "type": "text",
                "text": "\n\n".join(current_parts),
                "char_start": current_start,
                "char_end": (current_start + current_len) if current_start is not None else None,
            })

        return chunks

    def check_installation(self) -> bool:
        """Check if the parser is properly installed. Must be implemented by subclasses."""
        raise NotImplementedError("check_installation must be implemented by subclasses")

    @staticmethod
    def _is_name_clash() -> None:
        """Sentinel to detect when local variable 'Parser' shadows the class."""
        pass
