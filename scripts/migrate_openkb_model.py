#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""存量 openkb KB 的模型名改写 —— 升级前的一次性动作，以及**以后每次换模型的常规动作**。

背景
----
``llm-gateway`` 原先有一张 ``MODEL_ALIAS_MAP``，把入站的 ``gpt-5.4`` 在转发前改写
成部署里的模型名，替存量 KB 兜着。2026-10-08 那张表被**整体删除**（模型名应当在 KB
自己的 ``config.yaml`` 里显式可见、可审计，而不是由网关隐藏改写）。删掉之后，
``config.yaml`` 里仍写着 ``gpt-5.4`` 的 KB 会把这个名字原样发到上游，撞
``No available channel``。本脚本是那个「升级前改写存量数据」动作的入口。

改什么
------
遍历 ``<root>/**/.openkb/config.yaml``，以及 openkb 的全局配置
（``~/.config/openkb/global.yaml``，``model`` 在全局层也是有效键，会作用到每个 KB），
把 ``model`` 值改写成 ``--to``。

两种匹配口径：

- **默认（白名单）**：只改写 ``model`` 值**恰好等于 ``--from``** 的项。保守，不猜。
- **``--all``（全量同步）**：不看当前值，只要 ``model`` 不等于 ``--to`` 就改写。
  这是**换模型后的常规动作** —— 模型名的唯一来源是环境变量，改完 env 就用这条把
  存量 KB 一次性对齐。⚠️ 它会连带改掉**有意指定了别的模型**的 KB，跑之前先看预演。

KBs **没写 ``model`` 键**的一律不动（它们本来就走环境变量回落，已经是对的）；
本脚本也**不会**替它们补上这个键 —— 补上等于把「跟随环境变量」变成「钉死」，
反而挡住下一次换模型。

**只动 ``model:`` 那一行**：注释、键顺序、空行全部逐字节保留。用
``yaml.safe_load`` + ``safe_dump`` 重写整份文件会把注释全吃掉，所以检测用 YAML、
定位用正则、落盘前再用 YAML 复核 —— 三层任意一层对不上都会**报出来**，不静默跳过。

CLI 用法::

    # 预演（默认行为，不落盘）—— 先看清楚会改哪些
    python scripts/migrate_openkb_model.py --root /app/data/kb_storage

    # 真跑（每个被改动的文件先备份成 <file>.bak-<UTC 时间戳>）
    python scripts/migrate_openkb_model.py --root /app/data/kb_storage --apply

换模型后的全量同步（``--to`` 默认取 openkb 那条 env 链，通常不用手写）::

    python scripts/migrate_openkb_model.py --all            # 先预演
    python scripts/migrate_openkb_model.py --all --apply    # 确认后落盘

容器里跑 —— **推荐 `docker exec` 进已经在跑的容器**，不要用
``docker run --rm python:3.12-slim`` 那套（那个模式对**本**脚本不成立，原因见下）::

    docker cp scripts/migrate_openkb_model.py jonex-openkb:/tmp/
    # 预演（默认行为）
    docker exec jonex-openkb python /tmp/migrate_openkb_model.py --all
    # 真跑
    docker exec jonex-openkb python /tmp/migrate_openkb_model.py --all --apply

``--root`` 与 ``--to`` 在容器里都能自动取到：前者默认 ``$OPENKB_KB_ROOT``
（= ``/app/data/kb_storage``），后者默认取 ``OPENKB_LLM_MODEL``（即 §八 之后
openkb init 写进 ``config.yaml`` 的同一个值），这两个变量都在容器的 ``env_file`` 里。

⚠ ``--to`` **刻意不回落 ``DEFAULT_LLM_MODEL``**：本脚本写进 ``config.yaml`` 的值
最终被 openkb 原样交给 LiteLLM（LiteLLM 把 ``X/Y`` 当 ``provider/model``、剥掉第一段
再发上游），所以必须是 ``provider/上游服务ID`` 形态；而 ``DEFAULT_LLM_MODEL`` 是给
「原样发名」的消费者（平台 / JonexWiki / RAG，走 openai SDK）用的裸名形态。跨形态
回落会写出一个**必然报错**的模型名（400004 / ``LLM Provider NOT provided``），
故宁可不给默认值、让 operator 显式 ``--to``。

**为什么不能套用 ``docker run --rm -v <卷>:/data ... python:3.12-slim``**：

1. ``python:3.12-slim`` **没有 pyyaml** —— 本脚本 import yaml，会直接
   ``ModuleNotFoundError``。（本脚本需要 YAML 解析才能安全地"只改 model 那一行"。）
2. **全局配置不在那个卷里**。``~/.config/openkb/global.yaml`` 住在 ``jonex-openkb``
   容器自己的可写层，**不在** ``jonex-openkb-storage`` 卷内。新起的
   ``docker run --rm`` 容器 ``$HOME=/root``，该路径不存在 → 脚本会打印
   "全局配置不存在，跳过" 然后继续 —— **一个全局 ``model: gpt-5.4`` 就被静默漏掉了**。
3. ``docker run`` 不继承 ``env_file``，``OPENKB_LLM_MODEL`` / ``OPENKB_KB_ROOT``
   都取不到，``--to`` 与 ``--root`` 必须手写死。

确实想用 ``docker run --rm``（比如服务已停）就用 **openkb 自己的镜像**，
并把全局配置所在的目录也挂进去::

    docker run --rm \
      -v deploy_jonex-openkb-storage:/app/data/kb_storage \
      -v jonex-openkb-globalcfg:/root/.config/openkb \
      -v "$(pwd)/scripts:/scripts:ro" \
      --env-file deploy/.env.openkb \
      jonex-openkb-source \
      python /scripts/migrate_openkb_model.py --all --apply

⚠️ 与 ``scripts/`` 下其它脚本的习惯**相反**：本脚本默认预演、必须显式
``--apply`` 才落盘（``--dry-run`` 也接受，但只是个显式写法的别名）。因为它改的是
磁盘上的文件，误跑的代价不是"多读几次"。

**要不要停服**：不必。落盘走同目录临时文件 + ``os.replace``，是**原子替换** ——
运行中的 openkb 每请求都 ``load_config()`` 读这个文件，但也只会读到"旧的"或"新的"，
不会读到半截 YAML。想更稳妥（避免改写瞬间正在处理的那个请求用了旧值）可以先
``docker stop jonex-openkb``，改完 ``docker start``。

退出码：0 = 正常；1 = 参数非法 / 致命错误；2 = 跑完但有待人工处理的文件。
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Iterable, Iterator, Sequence

import yaml

#: 默认只认这一个旧值 —— 不做"见到什么改什么"的猜测。历史上还有过
#: ``deepseek-v4-flash-202605``，需要时用 ``--from`` 显式加上。
DEFAULT_FROM_VALUES = ("gpt-5.4",)

#: 与 openkb 适配器 `jonex_core/capability/atomic/openkb/openkb_adapter.py` 的
#: `OPENKB_KB_ROOT` 默认值一致。
DEFAULT_KB_ROOT = "/app/data/kb_storage"

_EXIT_OK = 0
_EXIT_ERROR = 1
_EXIT_NEEDS_ATTENTION = 2


class RewriteNotSupported(RuntimeError):
    """YAML 里读到了旧值，但无法安全定位/替换那一行（需人工处理）。"""


@dataclasses.dataclass(frozen=True)
class Rewrite:
    """一次改写的计划（纯数据，不碰磁盘）。"""

    old_value: str
    new_value: str
    new_text: str


@dataclasses.dataclass
class Report:
    """一次迁移的结果汇总。"""

    scanned: int = 0
    matched: int = 0
    rewritten: int = 0
    #: [(path, 原因)] —— YAML 读到旧值但改不了，需人工处理
    unhandled: list[tuple[Path, str]] = dataclasses.field(default_factory=list)
    #: 实际写下的备份文件
    backups: list[Path] = dataclasses.field(default_factory=list)


def iter_kb_configs(root: Path | str) -> Iterator[Path]:
    """产出 ``<root>/**/.openkb/config.yaml``（按路径排序，结果稳定）。

    只认 ``.openkb`` 目录下的 ``config.yaml``；root 不存在时产出空序列
    （升级脚本不该因为"还没建过任何 KB"而报错）。
    """
    root = Path(root)
    if not root.is_dir():
        return
    for path in sorted(root.rglob("config.yaml")):
        if path.parent.name == ".openkb":
            yield path


def _reads_back_as(text: str, expected: str) -> bool:
    """落盘前自查：文本仍是合法 YAML，且 ``model`` 等于期望值。"""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    return isinstance(data, dict) and data.get("model") == expected


def plan_rewrite(
    text: str,
    *,
    from_values: Iterable[str],
    to_value: str,
    match_any: bool = False,
) -> Rewrite | None:
    """算出改写方案；不需要改写返回 ``None``，改不了抛 :class:`RewriteNotSupported`。

    返回 ``None`` 的情形（都属正常，不算失败）：

    - 没写 ``model`` 键 / 值为 null —— 解析链会自动回落到环境变量，无需迁移；
    - ``model`` 值 **已经等于** ``to_value`` —— 幂等：重复跑是空操作；
    - 值不是字符串；
    - 默认口径下，``model`` 值不在 ``from_values`` 白名单里。

    ``match_any=True``（CLI 的 ``--all``）时**不看当前值**：只要它不等于
    ``to_value`` 就改写。这时 ``from_values`` 被忽略，允许为空。

    检测（YAML）与定位（正则）是两条独立路径：YAML 说"这是旧值"而正则找不到那一行
    （例如 flow mapping ``{model: gpt-5.4}``）时**必须抛错**，不能返回 ``None``
    假装无事发生 —— 那会让运维以为迁移干净了。
    """
    from_set = {v.strip() for v in from_values}
    if not match_any and not from_set:
        return None

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RewriteNotSupported(f"YAML 解析失败，无法判断 model：{exc}") from exc
    if not isinstance(data, dict):
        return None

    current = data.get("model")
    if not isinstance(current, str):
        return None
    current = current.strip()
    if not current or current == to_value:
        return None
    if not match_any and current not in from_set:
        return None

    pattern = re.compile(
        r'^(?P<prefix>[ \t]*model[ \t]*:[ \t]*)(?P<quote>["\']?)'
        + re.escape(current)
        + r'(?P=quote)(?P<tail>[ \t]*(?:#.*)?)$',
        re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise RewriteNotSupported(
            f"model 值为旧值 {current!r}，但匹配到 {len(matches)} 处 `model:` 行"
            "（应为恰好 1 处，重复键或写法特殊），需人工处理"
        )

    match = matches[0]
    # 优先沿用原有引号风格；原样读不回时再试无引号、双引号（目标值可能含特殊字符）。
    for quote in dict.fromkeys((match.group("quote"), "", '"')):
        candidate = (
            text[: match.start()]
            + match.group("prefix")
            + quote
            + to_value
            + quote
            + match.group("tail")
            + text[match.end() :]
        )
        if _reads_back_as(candidate, to_value):
            return Rewrite(old_value=current, new_value=to_value, new_text=candidate)

    raise RewriteNotSupported(
        f"model 值 {current!r} 改写为 {to_value!r} 后无法被 YAML 正确读回，需人工处理"
    )


def _atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 + ``os.replace`` —— 原子替换。

    不能直接 ``path.write_text()``：那是"先截断再写"，**运行中的 openkb 容器**
    每处理一个请求都会 ``load_config()`` 读这个文件，可能读到空文件或半截 YAML
    （表现为随机的 `ModelNotConfiguredError` / 解析失败）。``os.replace`` 在同一
    文件系统内是原子的，读者要么看到旧的、要么看到新的。

    （临时文件放在同目录而非 ``/tmp``：``os.replace`` 跨文件系统会退化成复制。）
    """
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        # 中途失败时别把临时文件留在 KB 目录里（`os.replace` 成功后 tmp 已不存在）。
        if tmp.exists():
            tmp.unlink()


def migrate(
    root: Path | str,
    *,
    from_values: Iterable[str],
    to_value: str,
    apply: bool = False,
    extra_configs: Sequence[Path] = (),
    match_any: bool = False,
) -> Report:
    """扫描并（可选）改写。``apply=False`` 时只统计，不落盘、不备份。

    ``match_any=True``（CLI 的 ``--all``）时不看当前值，凡不等于 ``to_value`` 的
    ``model`` 都改写；``from_values`` 随之被忽略。
    """
    from_values = tuple(from_values)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    targets = list(iter_kb_configs(root))
    for extra in extra_configs:
        if extra.is_file() and extra not in targets:
            targets.append(extra)

    report = Report()
    for path in targets:
        report.scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            report.unhandled.append((path, f"读取失败：{exc}"))
            continue

        try:
            plan = plan_rewrite(
                text, from_values=from_values, to_value=to_value, match_any=match_any
            )
        except RewriteNotSupported as exc:
            report.unhandled.append((path, str(exc)))
            continue
        if plan is None:
            continue

        report.matched += 1
        if not apply:
            continue

        backup = path.with_name(f"{path.name}.bak-{stamp}")
        try:
            shutil.copy2(path, backup)
            _atomic_write_text(path, plan.new_text)
        except OSError as exc:
            report.unhandled.append((path, f"写入失败（备份在 {backup}）：{exc}"))
            continue
        report.rewritten += 1
        report.backups.append(backup)

    return report


def _print_report(report: Report, *, apply: bool, to_value: str) -> None:
    mode = "真跑" if apply else "预演（未落盘）"
    print(
        f"[migrate_openkb_model] {mode} | 扫描 {report.scanned} 个 config.yaml，"
        f"命中 {report.matched} 个，改写 {report.rewritten} 个 → {to_value}"
    )
    if not apply and report.matched:
        print(f"[提示] 预演模式：{report.matched} 个文件**未**改动。确认无误后加 --apply。")
    for backup in report.backups:
        print(f"  备份: {backup}")
    for path, reason in report.unhandled:
        print(f"  ⚠ 需人工处理: {path}\n      {reason}", file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "把 openkb KB config.yaml 里的模型名改写成当前部署的模型名"
            "（换模型后的全量对齐用 --all）"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "容器内推荐用法（--to / --root 都从容器 env 自动取）:\n"
            "  # 换模型后全量对齐：先预演\n"
            "  docker exec jonex-openkb python /tmp/migrate_openkb_model.py --all\n"
            "  # 确认无误后落盘\n"
            "  docker exec jonex-openkb python /tmp/migrate_openkb_model.py --all --apply\n"
        ),
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("OPENKB_KB_ROOT") or DEFAULT_KB_ROOT,
        help=f"KB 存储根目录（默认：$OPENKB_KB_ROOT 或 {DEFAULT_KB_ROOT}）",
    )
    parser.add_argument(
        "--from",
        dest="from_values",
        default=",".join(DEFAULT_FROM_VALUES),
        help=(
            "要改写的旧模型名，逗号分隔（默认：%s）。--all 时此项被忽略"
            % ",".join(DEFAULT_FROM_VALUES)
        ),
    )
    parser.add_argument(
        "--all",
        dest="match_any",
        action="store_true",
        help=(
            "全量同步：不看当前值，凡 model 不等于 --to 的 KB 一律改写"
            "（换模型后的常规动作）。⚠️ 会连带改掉有意指定了别的模型的 KB"
        ),
    )
    parser.add_argument(
        "--to",
        default=os.environ.get("OPENKB_LLM_MODEL", "").strip(),
        help=(
            "改写成的模型名。默认取环境变量 OPENKB_LLM_MODEL"
            "（与 openkb init 写进 config.yaml 的口径一致）。"
            "⚠ 必须是 provider/上游服务ID 形态：该值原样交给 LiteLLM，"
            "LiteLLM 会剥掉第一段 provider/ 再发上游。"
            "刻意不回落 DEFAULT_LLM_MODEL（那是 openai SDK 的裸名口径，形态不兼容）"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="真正落盘。每个被改动的文件会先备份成 <file>.bak-<UTC 时间戳>",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="只预演不落盘（**这本来就是默认行为**，此参数仅为与其它迁移脚本写法一致而提供）",
    )
    parser.add_argument(
        "--global-config",
        default=str(Path.home() / ".config" / "openkb" / "global.yaml"),
        help="openkb 全局配置路径（model 在全局层也生效；不存在则跳过）",
    )
    parser.add_argument(
        "--no-global",
        action="store_true",
        help="不处理全局配置",
    )
    args = parser.parse_args(argv)

    from_values = tuple(v.strip() for v in args.from_values.split(",") if v.strip())
    to_value = (args.to or "").strip()
    if not args.match_any and not from_values:
        print("[错误] --from 不能为空（或改用 --all 全量同步）", file=sys.stderr)
        return _EXIT_ERROR
    if not to_value:
        print(
            "[错误] 未指定目标模型名：请用 --to 显式指定，或先设置环境变量 "
            "OPENKB_LLM_MODEL。\n"
            "       ⚠ 值必须是 provider/上游服务ID 形态（该值原样交给 LiteLLM，"
            "LiteLLM 剥掉第一段 provider/ 再发上游）。\n"
            "       ⚠ 刻意不回落 DEFAULT_LLM_MODEL：那是 openai SDK 消费者的裸名"
            "口径，回落过去会被剥掉前缀、写出一个必然报错的模型名。",
            file=sys.stderr,
        )
        return _EXIT_ERROR
    if not args.match_any and to_value in from_values:
        print(f"[错误] --to 与 --from 相同（{to_value}），无需迁移", file=sys.stderr)
        return _EXIT_ERROR
    if args.match_any:
        # 明确报出来：否则用户会以为 --from 的白名单还在生效
        print("[信息] --all：不看当前值，凡 model 不等于目标值的 KB 一律改写")

    root = Path(args.root)
    if not root.is_dir():
        print(f"[错误] KB 根目录不存在或不是目录：{root}", file=sys.stderr)
        return _EXIT_ERROR

    extra: list[Path] = []
    if not args.no_global:
        global_config = Path(args.global_config)
        if global_config.is_file():
            # 明确报出来：它在 --root 之外，否则"扫描数比 KB 数多 1"会让人以为统计错了。
            print(f"[信息] 一并处理 openkb 全局配置（model 在全局层对每个 KB 都生效）：{global_config}")
            extra.append(global_config)
        else:
            print(f"[信息] 全局配置不存在，跳过：{global_config}")

    report = migrate(
        root,
        from_values=from_values,
        to_value=to_value,
        apply=args.apply,
        extra_configs=extra,
        match_any=args.match_any,
    )
    _print_report(report, apply=args.apply, to_value=to_value)

    if report.unhandled:
        print(
            f"[警告] {len(report.unhandled)} 个文件需人工处理（见上）。"
            "这些 KB 仍会在调用时撞上游 No available channel。",
            file=sys.stderr,
        )
        return _EXIT_NEEDS_ATTENTION
    return _EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
