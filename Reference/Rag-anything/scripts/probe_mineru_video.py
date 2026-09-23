#!/usr/bin/env python3
"""探活：MinerU 视频转写链路（含「MinerU 机器内部」那一跳）。  # [jonex]

为什么需要它
------------
平台解析视频是**两跳**，两跳的地址与凭证是两个来源：

  ① 平台 --HTTP--> ``MINERU_VIDEO_BASE_URL``（如 http://127.0.0.1:20490）
     平台只发 ``/tasks`` 三个接口：POST /tasks、GET /tasks/{id}、GET /tasks/{id}/result。

  ② MinerU 容器 --> ``VIDEO_VLM_BASE_URL``（jonex-gpu 是 http://127.0.0.1:20491/v1，
     客户那台是 http://127.0.0.1:21491/v1）--> tokenhub.tencentmaas.com

第 ② 跳的地址和 key **都住在 MinerU 那台机器内部**（key 在它的 ``~/mineru-video/.env``，
由那台机器上的 nginx auth-proxy 在回环地址上注入，不暴露局域网），
**平台侧既配不到、也探不到**。

所以只能走完整任务，用任务的**终态**与 **error 字段**间接判定第 ② 跳：

  * ``completed`` 且结果里有转写正文  → ①② 全通
  * ``failed`` 且 error 含 402 / Payment Required → 第 ② 跳的上游账号欠费
  * ``failed`` 其他文本 → **原样打印**（别再靠猜；这正是 bd 977 修的那件事）

只依赖标准库。用法::

    python3 probe_mineru_video.py /path/to/small.mp4
    python3 probe_mineru_video.py /path/to/small.mp4 --base-url http://127.0.0.1:20490
    python3 probe_mineru_video.py /path/to/small.mp4 --json          # 附原始响应

退出码：0 = 两跳全通；1 = 任务失败（含欠费）；2 = 用法/传送层错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

#: 与 ``video_analysis/backends/mineru.py::_submit`` 保持一致的业务字段。
SUBMIT_FIELDS = {"return_content_list": True, "return_md": False}


# ── HTTP（只 stdlib，与 parsers/mineru.py 同款契约）─────────────────────

def _multipart(fields: dict, file_path: Path) -> tuple[bytes, str]:
    """构造 multipart/form-data。文件字段名固定为 ``files``（同 MineruSelfHostParser）。"""
    boundary = "----jonexprobe" + os.urandom(16).hex()
    crlf = b"\r\n"
    body = bytearray()
    for key, value in fields.items():
        body.extend(b"--" + boundary.encode() + crlf)
        body.extend(f'Content-Disposition: form-data; name="{key}"'.encode() + crlf)
        body.extend(crlf)
        body.extend(
            ("true" if value is True else "false" if value is False else str(value)).encode()
            + crlf
        )
    body.extend(b"--" + boundary.encode() + crlf)
    body.extend(
        f'Content-Disposition: form-data; name="files"; filename="{file_path.name}"'.encode()
        + crlf
    )
    body.extend(b"Content-Type: application/octet-stream" + crlf)
    body.extend(crlf)
    body.extend(file_path.read_bytes() + crlf)
    body.extend(b"--" + boundary.encode() + b"--" + crlf)
    return bytes(body), boundary


def _request(url: str, *, data: bytes | None = None, ctype: str | None = None,
             timeout: int = 120) -> dict:
    headers = {"Accept": "application/json"}
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {exc.code} {url}: {detail[:400]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连不上 {url}: {exc}") from exc


# ── 判定 ────────────────────────────────────────────────────────────────

#: 与 ``TaskManager._classify_transcribe_error``（bd 977）同口径的粗粒度归类。
_PHRASES = (
    ("payment required", "第②跳上游账号**欠费**（402 Payment Required）"),
    ("insufficient", "第②跳上游账号**余额不足**"),
    ("unauthorized", "第②跳上游**鉴权失败**（401）"),
    ("forbidden", "第②跳上游**鉴权失败**（403）"),
    ("too many requests", "第②跳上游**限流**（429）"),
    ("rate limit", "第②跳上游**限流**（429）"),
    ("not found", "第②跳**端点不对**（404）"),
    ("internal server error", "第②跳上游**内部错误**（500）"),
    ("timeout", "第②跳**超时**"),
    ("timed out", "第②跳**超时**"),
)


def _verdict(error_text: str) -> str:
    lowered = (error_text or "").lower()
    for needle, label in _PHRASES:
        if needle in lowered:
            return label
    if not error_text:
        return "任务失败但 MinerU **没给 error 文本**（要去看那台的容器日志）"
    return "任务失败，原因**认不出** —— 原始文本见上（别靠猜，去那台的日志查）"


def _report_entry(item: dict) -> tuple[int, list]:
    """打印一条结果的概况；返回 ``(有正文的段数, errors 列表)``。

    两种形状都要认（**这里踩过坑，别再改回去**）：

    * **视频结果** —— ``content_list`` 是 **JSON 字符串**，解析后是 VideoResult::

          {type: "video", source, duration, timeline_mode, video_mode,
           summary: {overview, topics, lang},      ← step1 VLM 粗读的产物
           chapters: [...], segments: [{start,end,text,source,...}],  ← step2 ASR/转写
           slides: [...], keyframe_ocr: {enabled, keyframes},         ← step3 OCR
           errors: [...]}                          ← 管线**自报**的错误，必须打出来

    * **文档结果** —— ``content_list`` 是 ``list[dict]``（``type``/``text``/``description``）。
    """
    raw = item.get("content_list")
    if raw is None:
        print(f"      ⚠ 没有 content_list，条目键={list(item.keys())}")
        return 0, []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            print(f"      ⚠ content_list 不是合法 JSON（前 200 字）：{raw[:200]}")
            return 0, []
    if item.get("converted_pdf_url"):
        print(f"      converted_pdf_url: {str(item['converted_pdf_url'])[:90]}")

    # ── 视频结果 ──────────────────────────────────────────────────
    if isinstance(raw, dict) and raw.get("type") == "video":
        src = raw.get("source") or {}
        print(f"      源文件   : {src.get('original_filename')}  "
              f"{raw.get('duration')}s  {raw.get('timeline_mode')}/{raw.get('video_mode')}")
        overview = ((raw.get("summary") or {}).get("overview") or "").strip()
        head = f"  {overview[:80]!r}" if overview else ""
        print(f"      step1 VLM: {'✓ 有全局摘要' if overview else '✗ 无全局摘要'}{head}")
        segments = [s for s in (raw.get("segments") or []) if isinstance(s, dict)]
        with_text = [s for s in segments if (s.get("text") or "").strip()]
        print(f"      章节/分段: {len(raw.get('chapters') or [])} 章 / "
              f"{len(segments)} 段（**{len(with_text)} 段有正文**）")
        srcs = Counter(s.get("source") for s in with_text)
        if srcs:
            print(f"      正文来源 : {dict(srcs)}")
        for seg in with_text[:3]:
            print(f"        · [{seg.get('start')}-{seg.get('end')}] {seg['text'].strip()[:80]!r}")
        print(f"      step3 OCR: {len((raw.get('keyframe_ocr') or {}).get('keyframes') or [])} 关键帧")
        return len(with_text), list(raw.get("errors") or [])

    # ── 文档结果 ──────────────────────────────────────────────────
    if isinstance(raw, list):
        print(f"      content_list: {len(raw)} 项")
        texts = 0
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            body = entry.get("description") or entry.get("text") or entry.get("content") or ""
            if isinstance(body, str) and body.strip():
                texts += 1
                if texts <= 3:
                    print(f"        · [{entry.get('type', '?')}] {body.strip()[:80]!r}")
        return texts, []

    print(f"      ⚠ content_list 解析后是意外的 {type(raw).__name__}：{str(raw)[:120]}")
    return 0, []


# ── 主流程 ──────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="探活 MinerU 视频链路（两跳都要通）")
    ap.add_argument("video", type=Path, help="小视频文件（几秒即可，别用长视频烧钱）")
    ap.add_argument(
        "--base-url",
        default=(os.environ.get("MINERU_VIDEO_BASE_URL")
                 or os.environ.get("MINERU_SELFHOST_BASE_URL")
                 or "http://127.0.0.1:20490"),
        help="MinerU 端点（默认取 MINERU_VIDEO_BASE_URL → MINERU_SELFHOST_BASE_URL）",
    )
    ap.add_argument("--poll-timeout", type=int, default=1800, help="轮询总超时秒（默认 1800）")
    ap.add_argument("--interval", type=int, default=5, help="轮询间隔秒（默认 5）")
    ap.add_argument("--json", action="store_true", help="附打原始响应")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    if not args.video.is_file():
        print(f"✗ 找不到视频文件：{args.video}", file=sys.stderr)
        return 2
    size_mb = args.video.stat().st_size / 1048576
    print(f"  探测目标 : {base}")
    print(f"  视频     : {args.video.name}  ({size_mb:.2f} MB)")
    print()

    # ① 提交
    print("① 提交任务  POST /tasks")
    body, boundary = _multipart(SUBMIT_FIELDS, args.video)
    try:
        submitted = _request(
            f"{base}/tasks", data=body,
            ctype=f"multipart/form-data; boundary={boundary}", timeout=600,
        )
    except RuntimeError as exc:
        print(f"   ✗ {exc}")
        print("\n❌ 判定：**第①跳就断了** —— 平台连不上这台 MinerU（与 402 无关）。")
        print("   检查：网络可达 / 端口 / MINERU_VIDEO_BASE_URL 是否写错。")
        return 2
    task_id = submitted.get("task_id")
    if not task_id:
        print(f"   ✗ 未返回 task_id：{submitted}")
        return 2
    print(f"   ✓ task_id = {task_id}")

    # ② 轮询
    print(f"\n② 轮询  GET /tasks/{task_id}（最多 {args.poll_timeout}s）")
    deadline = time.monotonic() + args.poll_timeout
    last, status = "", {}
    while time.monotonic() < deadline:
        try:
            status = _request(f"{base}/tasks/{task_id}", timeout=60)
        except RuntimeError as exc:
            print(f"   ✗ 轮询失败：{exc}")
            return 2
        state = status.get("status", "")
        if state != last:
            print(f"   [{time.strftime('%H:%M:%S')}] {state}")
            last = state
        if state in ("completed", "failed"):
            break
        time.sleep(args.interval)
    else:
        print(f"   ✗ {args.poll_timeout}s 内没到终态（最后 {last!r}）")
        return 2

    # ③ 终态
    if status.get("status") == "failed":
        error_text = status.get("error") or ""
        print(f"\n③ 任务失败\n   error: {error_text or '(空)'}")
        if args.json:
            print(f"\n   原始响应: {json.dumps(status, ensure_ascii=False)[:1200]}")
        print(f"\n❌ 判定：{_verdict(error_text)}")
        if "欠费" in _verdict(error_text) or "余额" in _verdict(error_text):
            print("   → 第②跳的 key 在 **MinerU 那台机器** 的 ~/mineru-video/.env，")
            print("     由那台的 nginx auth-proxy 在回环地址上注入；**平台侧改不到它**。")
            print("     要换 key/充值，得登到 127.0.0.1 那台去改。")
        return 1

    print("\n③ 任务完成  GET /tasks/{id}/result")
    try:
        payload = _request(f"{base}/tasks/{task_id}/result", timeout=300)
    except RuntimeError as exc:
        print(f"   ✗ 取结果失败：{exc}")
        return 2
    results = payload.get("results") or {}
    if not results:
        print(f"   ✗ 结果为空：{json.dumps(payload, ensure_ascii=False)[:400]}")
        return 1
    texts, errors = 0, []
    for stem, item in results.items():
        print(f"   ── {stem}")
        if isinstance(item, dict):
            found, errs = _report_entry(item)
            texts += found
            errors += errs
    if args.json:
        print(f"\n   原始响应: {json.dumps(payload, ensure_ascii=False)[:1200]}")

    print()
    if errors:
        print(f"⚠ 管线自报了 {len(errors)} 条错误（任务终态仍是 completed —— 说明有步骤降级）：")
        for err in errors[:5]:
            print(f"     · {str(err)[:200]}")
    if texts:
        print("✅ 判定：**两跳全通** —— 第②跳（MinerU 机器内部 VLM/ASR → tokenhub）正常，")
        print(f"   结果里有 {texts} 段正文" + ("（但带上面那些降级错误，需留意）。" if errors else "。"))
        return 0
    if errors:
        print("⚠ 判定：没有正文，**但有降级错误** —— 原因见上，别靠猜。")
        return 1
    print("⚠ 判定：任务 completed，但**结果里既没有正文、也没有自报错误**。")
    print("   两跳都通（没报错），是转写本身没出文本（无语音 / 全静音）。")
    print("   换一段**有人说话**的短视频再试，或去看那台 MinerU 的容器日志。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
