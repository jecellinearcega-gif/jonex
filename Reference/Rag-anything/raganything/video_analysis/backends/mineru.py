"""MinerU video analysis backend.  # [jonex] 新增

把视频解析整条委托给 MinerU ``mineru-api`` 服务：其内部自带三步
（VLM 粗读 / ASR / OCR 兜底），产出 §3.4 媒体契约。

HTTP 传输复用 :class:`~raganything.parsers.mineru.MineruSelfHostParser`
的标准库实现（multipart 构造 / JSON 请求 / 轮询），不引入第三方依赖，
也不改动该 parser 本身。

端点由 ``MINERU_VIDEO_BASE_URL`` 指定，未设置时回退
``MINERU_SELFHOST_BASE_URL``。独立变量的原因：文档解析与视频解析可能
指向不同实例（如文档走 mineru.net 云 API、视频走自建实例）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from raganything.parsers.mineru import (
    MineruSelfHostParser,
    MineruTransportError,
    _REASON_REJECTED,
)
from raganything.video_analysis import (
    ValidationIssue,
    VideoAnalysisBackend,
    VideoAnalysisResult,
    register_video_analysis_backend,
)

if TYPE_CHECKING:
    from raganything.config import RAGAnythingConfig

logger = logging.getLogger(__name__)

BASE_URL_ENV = "MINERU_VIDEO_BASE_URL"
FALLBACK_BASE_URL_ENV = "MINERU_SELFHOST_BASE_URL"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def _resolve_base_url() -> str:
    """视频端点解析：MINERU_VIDEO_BASE_URL → MINERU_SELFHOST_BASE_URL → 默认。"""
    for env_name in (BASE_URL_ENV, FALLBACK_BASE_URL_ENV):
        value = (os.environ.get(env_name) or "").strip()
        if value:
            return value.rstrip("/")
    return DEFAULT_BASE_URL


# 三类场景来源（写进 scene 的 source_type，下游映射成 chunk 的 ctype）
SOURCE_TYPE_CHAPTER = "chapter"
SOURCE_TYPE_SLIDE = "slide"
SOURCE_TYPE_ASR = "asr"


def _chapters_to_scenes(chapters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """语义章节 → scene（LLM 归纳的摘要，信息密度高但无精确数字）。"""
    scenes: List[Dict[str, Any]] = []
    for chapter in chapters:
        description = str(chapter.get("summary") or chapter.get("title") or "")
        key_points = chapter.get("key_points") or []
        if key_points:
            bullets = "\n".join(f"- {point}" for point in key_points)
            description = f"{description}\n{bullets}" if description else bullets
        scenes.append(
            {
                "description": description,
                "start_time": chapter.get("start"),
                "end_time": chapter.get("end"),
                "chapter_id": chapter.get("id"),
                "source_type": SOURCE_TYPE_CHAPTER,
            }
        )
    return scenes


def _slides_to_scenes(slides: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """幻灯片页 → scene（OCR 原文，含精确数字；空文本页丢弃）。"""
    scenes: List[Dict[str, Any]] = []
    for slide in slides:
        text = str(slide.get("text") or "").strip()
        if not text:
            continue
        scenes.append(
            {
                "description": text,
                "start_time": slide.get("start"),
                "end_time": slide.get("end"),
                "slide_page": slide.get("page"),
                "source_type": SOURCE_TYPE_SLIDE,
            }
        )
    return scenes


def _speech_to_scenes(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """语音段按 ``chapter_id`` 归并成一章一条。

    为什么必须归并：实测 23 分钟视频有 **646 条** speech 段，逐条入 scenes
    会变成 646 个 chunk（归并后约 10 条，与 chapters 数量级一致）。
    MinerU 的 fuse 已把 speech 段挂到章节（``chapter_id``），按它分组最自然、
    且与 chapters 边界天然对齐；无 ``chapter_id`` 的散段归为**一组**兜底，
    不逐条散落。

    只取 ``source == "speech"``：``source == "slide"`` 的段与 ``slides[]``
    重复，``source == "visual"`` 属第三类（本期不处理，只留 raw_json）。
    """
    grouped: Dict[Any, List[Dict[str, Any]]] = {}
    for segment in segments:
        if str(segment.get("source") or "") != "speech":
            continue
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        grouped.setdefault(segment.get("chapter_id"), []).append(segment)

    scenes: List[Dict[str, Any]] = []
    for chapter_id, items in grouped.items():
        starts = [i.get("start") for i in items if i.get("start") is not None]
        ends = [i.get("end") for i in items if i.get("end") is not None]
        scenes.append(
            {
                "description": " ".join(
                    str(i.get("text") or "").strip() for i in items
                ),
                "start_time": min(starts) if starts else None,
                "end_time": max(ends) if ends else None,
                "chapter_id": chapter_id,
                "source_type": SOURCE_TYPE_ASR,
            }
        )
    return scenes


def map_video_result(video_result: Dict[str, Any]) -> VideoAnalysisResult:
    """MinerU ``VideoResult`` → :class:`VideoAnalysisResult`。

    映射规则（设计文档 §6.2 / §6.6）：

    - ``summary`` ← ``summary.overview``
    - ``scenes``  ← **三类来源聚合**，每条带 ``source_type`` 区分：
      ``chapters[]`` → ``chapter``（语义摘要）；``slides[]`` → ``slide``
      （OCR 原文，带 ``slide_page``）；``segments[source=speech]`` 按
      ``chapter_id`` 归并 → ``asr``。结果按 ``start_time`` 排序。
      下游只读 ``description`` / ``start_time`` / ``end_time``，其余键
      （``source_type`` / ``chapter_id`` / ``slide_page``）供映射成 chunk
      的 ``ctype`` 用。
    - ``tags``    ← ``summary.topics``（``list[str]`` 包装成 ``List[Dict]``）
    - ``raw_json``← 整个 VideoResult，无损兜底
    - ``segments`` / ``slides`` / ``keyframe_ocr`` 的原始形态仅存于 ``raw_json``
    """
    summary_info = video_result.get("summary") or {}
    overview = str(summary_info.get("overview") or "")

    scenes = (
        _chapters_to_scenes(video_result.get("chapters") or [])
        + _slides_to_scenes(video_result.get("slides") or [])
        + _speech_to_scenes(video_result.get("segments") or [])
    )
    # 三类混排后按时间排序；start_time 缺失的排最前（与下游 :212-215 的
    # 兜底口径一致——那里对 None 也按 0.0 处理）
    scenes.sort(key=lambda scene: scene.get("start_time") or 0.0)

    topics = summary_info.get("topics") or []
    tags = [
        {"name": str(topic), "confidence": None, "category": None}
        for topic in topics
    ]

    return VideoAnalysisResult(
        summary=overview,
        scenes=scenes or None,
        tags=tags or None,
        raw_json=json.dumps(video_result, ensure_ascii=False),
        analysis_method="mineru",
        metadata={
            "duration": video_result.get("duration"),
            "timeline_mode": video_result.get("timeline_mode"),
            "video_mode": video_result.get("video_mode"),
            "errors": list(video_result.get("errors") or []),
            "source": dict(video_result.get("source") or {}),
        },
    )


POLL_INTERVAL_ENV = "MINERU_VIDEO_POLL_INTERVAL"
POLL_TIMEOUT_ENV = "MINERU_VIDEO_POLL_TIMEOUT"
MODE_ENV = "MINERU_VIDEO_MODE"
COARSE_PROMPT_ENV = "MINERU_VIDEO_COARSE_PROMPT"


@register_video_analysis_backend()
class MineruVideoBackend(VideoAnalysisBackend):
    """视频解析委托给 MinerU ``mineru-api``。  # [jonex] 新增

    走 ``POST /tasks``（multipart）→ 轮询 ``GET /tasks/{id}`` →
    ``GET /tasks/{id}/result``。失败一律抛错，不降级到本地管线
    （设计决策 D4：保证产出粒度可预测）。
    """

    binding = "mineru"

    def __init__(self, config: "RAGAnythingConfig"):
        super().__init__(config)
        explicit_base_url = (os.environ.get(BASE_URL_ENV) or "").strip()
        self.base_url = _resolve_base_url()
        if not explicit_base_url:
            # [jonex] 回落到自建解析端点是设计 §6.3 的既定行为（单实例部署免配置），
            # 但两者可能指向不同实例（本仓库 .env 的 MINERU_SELFHOST_BASE_URL 就是
            # 文档解析端点），串台会完全静默——此处留痕。
            logger.warning(
                "%s 未显式设置，视频端点回落到自建解析端点 %s；"
                "若该地址实为文档解析端点，视频解析会失败，请显式设置 %s。",
                BASE_URL_ENV,
                self.base_url,
                BASE_URL_ENV,
            )
        self.video_mode = (os.environ.get(MODE_ENV) or "text").strip() or "text"
        self.coarse_prompt = (os.environ.get(COARSE_PROMPT_ENV) or "").strip()
        self.poll_interval = float(os.environ.get(POLL_INTERVAL_ENV, "5"))
        self.poll_timeout = int(os.environ.get(POLL_TIMEOUT_ENV, "1800"))
        self._transport = self._build_transport()

    def _build_transport(self) -> MineruSelfHostParser:
        """构造 HTTP 传输。

        复用 ``MineruSelfHostParser`` 的标准库实现——它本身就是纯 HTTP
        客户端（不依赖本地 mineru 包），此处只覆盖端点与轮询参数。
        """
        transport = MineruSelfHostParser()
        transport.base_url = self.base_url
        transport.poll_interval = self.poll_interval
        transport.poll_timeout = self.poll_timeout
        return transport

    # ── VideoAnalysisBackend ──────────────────────────────────────

    async def analyze_video(
        self, video_path: str, prompt: Optional[str] = None
    ) -> VideoAnalysisResult:
        """阻塞式 HTTP 放入线程池，避免拖慢事件循环。"""
        if prompt and prompt.strip():
            # [jonex] video_processor 恒传 per-KB 覆盖提示词，但本 backend 不消费：
            # MinerU 的 coarse_prompt 是 VLM 粗读偏向，与本地管线汇总用的 per-KB
            # 提示词不是同一个旋钮，盲目转发是行为错误，故只留痕不转发。
            logger.debug(
                "MineruVideoBackend 未把 prompt（%d 字符）转发给 MinerU：该 prompt 是"
                "本地管线汇总用的 per-KB 覆盖提示词；MinerU 的粗读偏向请配置 %s。",
                len(prompt),
                COARSE_PROMPT_ENV,
            )
        return await asyncio.to_thread(self._analyze_blocking, video_path)

    def _analyze_blocking(self, video_path: str) -> VideoAnalysisResult:
        path = Path(video_path)
        if not path.exists():
            raise FileNotFoundError(f"Video file does not exist: {video_path}")

        safe_name = self._transport._ascii_safe_filename(path.name)
        stem = Path(safe_name).stem

        task_id = self._submit(path)
        self._transport._poll_task(task_id)
        item = self._transport._fetch_result_item(task_id, stem)

        raw = item.get("content_list")
        if raw is None:
            raise RuntimeError(
                f"MinerU 视频结果缺少 content_list（task_id={task_id}）："
                f"{list(item.keys())}"
            )
        # [jonex] 设计 §8：解析失败要附原始响应片段（同 parsers/mineru.py 的做法），
        # 且非 dict 时提前报错——否则会在 map_video_result 里变成
        # AttributeError: 'list' object has no attribute 'get'。
        if isinstance(raw, str):
            try:
                video_result = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"MinerU 视频 content_list 不是合法 JSON（task_id={task_id}）："
                    f"{raw[:500]}"
                ) from exc
        else:
            video_result = raw
        if not isinstance(video_result, dict):
            raise RuntimeError(
                f"MinerU 视频结果类型异常（task_id={task_id}）："
                f"期望 VideoResult 对象（dict），实际拿到 "
                f"{type(video_result).__name__}"
            )
        return map_video_result(video_result)

    def _submit(self, path: Path) -> str:
        fields: Dict[str, Any] = {
            "return_content_list": True,
            "return_md": False,
        }
        if self.coarse_prompt:
            fields["coarse_prompt"] = self.coarse_prompt
        # video_mode 为非 text 才发（与上游客户端一致，text 是服务端缺省）
        if self.video_mode and self.video_mode != "text":
            fields["video_mode"] = self.video_mode

        try:
            payload = self._transport._post_multipart(
                "/tasks", path, fields, timeout=300
            )
        except RuntimeError as exc:
            # [jonex] 上传被 gate 拒是 400 的典型场景（设计 §8），而它发生在
            # 「未返回 task_id」分支之前，那段提示原本永远走不到。此处保留原始
            # 信息并追加指引；其余错误码原样抛出，不包装。
            if "HTTP 400" not in str(exc):
                raise
            # [jonex] 重包时必须**带上原来的粗粒度 reason**（bd 977）：分类器靠
            # `reason` 归类，换成裸 RuntimeError 会把「转写服务拒绝请求」丢回
            # 兜底的「转写服务不可用」—— 那正是本单要消灭的塌缩。
            raise MineruTransportError(
                f"{exc}。目标实例 {self.base_url} 未开启 VIDEO_ENABLED"
                f"（VIDEO_ENABLED=true）是 400/Unsupported file type 的最常见主因；"
                f"该 env 在上传白名单 import 时定形，改后必须重启或重建容器才生效。",
                reason=getattr(exc, "reason", None) or _REASON_REJECTED,
            ) from exc
        task_id = payload.get("task_id")
        if not task_id:
            raise RuntimeError(
                f"MinerU 未返回 task_id：{payload}。"
                f"若为 400/Unsupported file type，请确认目标实例 "
                f"VIDEO_ENABLED=true（改该 env 后必须重启容器）。"
            )
        logger.info(
            "MinerU 视频任务已提交：%s (endpoint=%s, file=%s)",
            task_id,
            self.base_url,
            path.name,
        )
        return task_id

    # ── config validation / lifecycle ─────────────────────────────

    @classmethod
    def validate_config(cls, config) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        if not _resolve_base_url():
            issues.append(
                ValidationIssue(
                    "error",
                    BASE_URL_ENV,
                    "MinerU 视频端点未配置（MINERU_VIDEO_BASE_URL）",
                )
            )
        return issues

    def close(self) -> None:
        self._transport = None
