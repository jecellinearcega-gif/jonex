"""[jonex] 转写失败原因分类：HTTP 状态码 / 网络错误 / 任务体失败 必须可区分。

bd ``jonex-platform-977``：``_classify_transcribe_error`` 原先只按异常**类型名**判定，
而 ``parsers/mineru.py`` 把 ``urllib.error.HTTPError`` / ``URLError`` **一律重抛成
``RuntimeError``**，``_poll_task`` 的「任务失败」也是 ``RuntimeError`` ——
于是 400/401/402/404/429/5xx/连不上/对端掐断**全部塌缩成同一句「转写服务不可用」**。
2026-10-09 线上那次上游欠费（402）之所以排查绕远路，就是因为用户与运维看到的是同一句话。

本文件只走**行为面**：驱动真实传输层与真实轮询，拿**真实抛出的异常**去喂分类器，
不引用任何新内部类型名 —— 这样实现怎么改都不影响本测试的判别力。

安全约束（沿用既有规矩）：分类结果会写进 ``content.md`` → 编译 → 索引 → 可能出现在
搜索结果里，所以**只准是粗粒度原因串**；异常原文（可能含 COS 签名 URL / 内网域名）只进日志。
``test_reason_is_safe_for_content_md`` 就是钉这条的。
"""
import http.client
import io
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from raganything.parsers.mineru import MineruSelfHostParser
from raganything.service.task_manager import TaskManager

BASE = "http://mineru.test:8000"

#: 2026-10-09 线上真实失败文本（上游 tokenhub 欠费，由 MinerU 写在**任务体**里，
#: HTTP 请求本身是 200 —— 所以只按 HTTP 状态码分档抓不到这一例）。
_UPSTREAM_QUOTA_ERROR = (
    "视频解析 step1(VLM 粗读)失败: 402 Client Error: Payment Required for url: "
    "http://127.0.0.1:21491/v1/chat/completions"
)


def _reason(exc: BaseException) -> str:
    return TaskManager._classify_transcribe_error(exc)


def _parser() -> MineruSelfHostParser:
    parser = MineruSelfHostParser()
    parser.base_url = BASE
    return parser


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        f"{BASE}/tasks/x", code, "err", {}, io.BytesIO(b'{"detail":"boom"}')
    )


def _raise_through_transport(exc: BaseException) -> str:
    """让真实传输层抛错，再把真实异常喂给分类器（不走人造异常）。"""
    with patch.object(urllib.request, "urlopen", side_effect=exc):
        try:
            _parser()._get_json("/tasks/x")
        except BaseException as raised:  # noqa: BLE001 — 诊断路径，原样取用
            return _reason(raised)
    raise AssertionError("传输层没有抛错，测试装置失效")


def _raise_through_task_failure(error_text: str) -> str:
    """让真实 ``_poll_task`` 看到 failed 任务，再分类它抛出的异常。"""
    with patch.object(
        MineruSelfHostParser, "_get_json",
        return_value={"status": "failed", "error": error_text},
    ):
        try:
            _parser()._poll_task("task-x", timeout=1)
        except BaseException as raised:  # noqa: BLE001
            return _reason(raised)
    raise AssertionError("_poll_task 没有抛错，测试装置失效")


# ── ① HTTP 状态码通道 ────────────────────────────────────────────────

@pytest.mark.parametrize("code,expected", [
    (400, "转写服务拒绝请求"),
    (401, "转写服务鉴权失败"),
    (402, "转写服务额度不足"),
    (403, "转写服务鉴权失败"),
    (404, "转写服务端点不对"),
    (429, "转写服务限流"),
    (500, "转写服务内部错误"),
    (502, "转写服务内部错误"),
    (503, "转写服务内部错误"),
])
def test_http_status_filtered_through_transport_is_distinguished(code, expected):
    assert _raise_through_transport(_http_error(code)) == expected


# ── ② 网络通道 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("exc", [
    urllib.error.URLError("Connection refused"),
    urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
    ConnectionRefusedError(111, "Connection refused"),
    http.client.RemoteDisconnected("Remote end closed connection without response"),
])
def test_network_failure_is_distinguished(exc):
    assert _raise_through_transport(exc) == "转写服务连不上"


# ── ③ 任务体失败通道（线上那次的形态）──────────────────────────────

def test_task_body_402_is_reported_as_quota():
    """上游欠费写在任务体里 —— 这正是 2026-10-09 线上的形态。"""
    assert _raise_through_task_failure(_UPSTREAM_QUOTA_ERROR) == "转写服务额度不足"


@pytest.mark.parametrize("error_text,expected", [
    ("... 401 Unauthorized ...", "转写服务鉴权失败"),
    ("... 403 Forbidden ...", "转写服务鉴权失败"),
    ("... 429 Too Many Requests ...", "转写服务限流"),
    ("... 404 Not Found ...", "转写服务端点不对"),
    ("... 500 Internal Server Error ...", "转写服务内部错误"),
    ("... ConnectTimeout: timed out ...", "转写超时"),
    ("... 磁盘写满了 ...", "转写服务不可用"),          # 认不出 → 兜底，不许瞎猜
    ("", "转写服务不可用"),
])
def test_task_body_failure_is_classified_or_falls_back(error_text, expected):
    assert _raise_through_task_failure(error_text) == expected


# ── ④ 既有分档不许回归 ──────────────────────────────────────────────

def test_existing_buckets_unchanged():
    assert _reason(TimeoutError("timed out")) == "转写超时"
    assert _reason(FileNotFoundError("/x/a.mp4")) == "转写服务不可用"
    assert _reason(ValueError("whatever")) == "转写服务不可用"

    class ConfigurationError(Exception):
        pass

    assert _reason(ConfigurationError("missing")) == "配置缺失"


def test_name_based_http_client_exceptions_still_classified():
    """httpx 那条链（非 stdlib）靠类型名判定的既有行为不能丢。"""

    class ConnectError(Exception):
        pass

    assert _reason(ConnectError("boom")) == "转写超时"


# ── ⑤ 安全约束闸门：进 content.md 的只能是粗粒度原因串 ────────────────

def test_reason_is_safe_for_content_md():
    """原因串会进 content.md → 编译 → 索引 → 搜索结果，绝不许夹带原文。"""
    produced = [
        _raise_through_transport(_http_error(code))
        for code in (400, 401, 402, 403, 404, 429, 500, 503)
    ] + [
        _raise_through_transport(urllib.error.URLError("Connection refused")),
        _raise_through_task_failure(_UPSTREAM_QUOTA_ERROR),
        _raise_through_task_failure("... 401 Unauthorized ..."),
        _reason(TimeoutError("timed out")),
        _reason(ValueError("whatever")),
    ]

    forbidden = ("http", "://", "/v1/", ":2049", "127.0.0.1", "sk-", "Bearer", ".com", ".cn")
    for reason in produced:
        assert reason, "原因串不能为空"
        assert len(reason) <= 20, f"原因串过长，可能夹带原文：{reason!r}"
        for token in forbidden:
            assert token not in reason, f"原因串夹带了 {token!r}：{reason!r}"
