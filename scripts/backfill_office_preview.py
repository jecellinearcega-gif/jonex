#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[jonex] 存量 office 文档 preview.pdf 回填（office「查看原文」预览）。

背景
----
preview.pdf 只在**解析时**产出（full 链路 = OfficePreviewUploadStage，
parse_only 链路 = task_manager._upload_office_preview）。在接入 parse_only 之前
入库的 office 文档没有 preview.pdf，此时 GET /documents/{id}/raw 会走
document_service.get_raw_location 的「preview 缺失 → 回退原始文件」分支：
返回的是 xlsx/docx 本体且 Content-Disposition: inline —— 浏览器无法内联渲染
office 二进制，于是点「查看原文」直接变成下载，弹窗里没有任何预览。

本脚本对存量文档补转 PDF 并上传到 preview 键，幂等（已存在则跳过，--force 覆盖）。

用法
----
    # 试跑：只列出会处理的文档，不做转换
    python scripts/backfill_office_preview.py --tenant tenant_jonex_demo --dry-run
    # 回填一个租户
    python scripts/backfill_office_preview.py --tenant tenant_jonex_demo
    # 只回填指定文档并强制覆盖
    python scripts/backfill_office_preview.py --doc-id <id> --force

运行环境
--------
- 用装了 vendored raganything 的解释器跑（本地为 .venv-rag/bin/python）；
- 环境变量与平台进程一致：DB_HOST/DB_PORT/DB_USERNAME/DB_PASSWORD/DB_NAME、
  KB_INPUT_DIR、OBJECT_STORAGE_BACKEND、OUTPUT_DIR；默认自动加载仓库根的
  .env.local（--env-file 可换，--no-env-file 关闭）；
- office → PDF 需要 LibreOffice：镜像/服务器上在 PATH 即可；本地装在 PATH 之外时
  用 LIBREOFFICE_BIN 指向 soffice 绝对路径。

注意
----
preview 的上传后端取 OBJECT_STORAGE_BACKEND（get_object_storage），与检索侧读取
口径同源：object_storage/__init__.py 的注释明确要求 preview 上传/读取同源，否则
「后端从 local 切 cos 后 reparse」会出现 preview 实际在全局后端、查看却按文档后端
找的错配。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("backfill_office_preview")

LOADED_ENV_FILE = ""


def load_env_file(path: str) -> None:
    """加载 shell 风格 env 文件（已存在的环境变量不覆盖）。

    只认 KEY=VALUE；值两端的引号会被剥掉（.env.local 里有单引号包裹的 JSON）。
    """
    global LOADED_ENV_FILE
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
    LOADED_ENV_FILE = str(p)


async def _preview_exists(storage, key: str) -> bool:
    """preview 对象是否已存在。

    口径与 DocumentService._office_preview_available 一致（COS/S3 的 presigned_url
    是纯本地签名、不检查对象存在性，故必须主动探测）。
    """
    try:
        fs_path = storage.fs_path(key) if hasattr(storage, "fs_path") else None
        if fs_path:
            return os.path.exists(fs_path)
        if hasattr(storage, "head_object"):
            return bool(await storage.head_object(key))
        await storage.get_bytes(key)
        return True
    except Exception:  # noqa: BLE001 —— 探测异常一律按「不存在」处理
        return False


async def _resolve_local_path(storage, doc, tmp_dir: str) -> tuple[str, bool]:
    """取文档原件的本地可读路径，返回 (路径, 是否临时文件)。

    local 后端 fs_path 直接给绝对路径（不复制）；cos/s3 走 get_to_path 落地到临时目录。
    """
    fs_path = None
    try:
        fs_path = storage.fs_path(doc.storage_key) if hasattr(storage, "fs_path") else None
    except Exception:  # noqa: BLE001
        fs_path = None
    if fs_path and os.path.exists(fs_path):
        return fs_path, False
    dst = os.path.join(tmp_dir, os.path.basename(doc.storage_key or "") or doc.id)
    await storage.get_to_path(doc.storage_key, dst)
    return dst, True


_DOC_SQL = """
    SELECT id, tenant_id, knowledge_base_id, file_name, mime_type,
           storage_backend, storage_key
      FROM knowledge_base.knowledge_documents
     WHERE is_deleted = 0
       AND status = 'ready'
       AND storage_key IS NOT NULL
"""


async def _load_docs(args: argparse.Namespace) -> list:
    """按条件读文档行。

    刻意用原始 SQL 而不是 ORM：import capabilities.knowledge_base.* 会连带拉起
    capability → repository → ontology_embedding（openai 等重依赖），而回填脚本
    只需要 DB + 对象存储 + raganything，不该被解析侧之外的东西绑住。
    """
    from sqlalchemy import bindparam, text

    from jonex_core.common.database import get_db_session

    clauses, params = [], {}
    if args.tenant:
        clauses.append("tenant_id = :tenant")
        params["tenant"] = args.tenant
    if args.kb_id:
        clauses.append("knowledge_base_id = :kb_id")
        params["kb_id"] = args.kb_id
    if args.doc_id:
        clauses.append("id IN :doc_ids")
        params["doc_ids"] = list(args.doc_id)
    sql = _DOC_SQL + (" AND " + " AND ".join(clauses) if clauses else "")
    sql += " ORDER BY created_at"
    if args.limit:
        sql += " LIMIT :limit"
        params["limit"] = args.limit

    stmt = text(sql)
    if args.doc_id:
        stmt = stmt.bindparams(bindparam("doc_ids", expanding=True))
    async with get_db_session() as session:
        return list((await session.execute(stmt, params)).fetchall())


async def run(args: argparse.Namespace) -> int:
    # 延迟 import：必须在 env 文件加载之后（settings 在 import 期读环境变量）
    from jonex_core.common.file_source_util import classify_media
    from jonex_core.common.object_storage import (
        build_preview_key,
        get_object_storage,
        get_object_storage_for,
    )
    from raganything.pipeline.stages import upload_office_preview

    if LOADED_ENV_FILE:
        logger.info("已加载环境变量文件: %s", LOADED_ENV_FILE)

    docs = await _load_docs(args)

    office_docs = [
        d for d in docs if classify_media(d.mime_type, d.file_name) == "office"
    ]
    logger.info(
        "扫描到文档 %d 篇，其中 office %d 篇（租户=%s）",
        len(docs), len(office_docs), args.tenant or "<全部>",
    )

    # preview 的读写口径：上传走全局后端（get_object_storage），原件读取按文档自身后端
    upload_storage = get_object_storage()
    output_dir = args.output_dir or os.getenv("OUTPUT_DIR") or "./output"
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    stats = {"done": 0, "skipped": 0, "failed": 0}
    tmp_dir = tempfile.mkdtemp(prefix="office-preview-backfill-")
    try:
        for doc in office_docs:
            key = build_preview_key(doc.tenant_id, doc.knowledge_base_id, doc.id)
            label = doc.file_name or doc.id
            if not args.force and await _preview_exists(upload_storage, key):
                logger.info("跳过（preview 已存在）: %s", label)
                stats["skipped"] += 1
                continue
            if args.dry_run:
                logger.info("待处理（dry-run）: %s doc=%s", label, doc.id)
                stats["done"] += 1
                continue
            try:
                storage = get_object_storage_for(doc.storage_backend)
                local_path, is_temp = await _resolve_local_path(storage, doc, tmp_dir)
                logger.info("转换中: %s <- %s", label, local_path)
                ok = await upload_office_preview(
                    file_path=local_path,
                    file_name=doc.file_name,
                    tenant_id=doc.tenant_id,
                    kb_id=doc.knowledge_base_id,
                    doc_anchor=doc.id,
                    output_dir=output_dir,
                    logger=logger,
                )
                if ok:
                    stats["done"] += 1
                    logger.info("完成: %s", label)
                else:
                    stats["failed"] += 1
                    logger.warning("失败（详见上方 office-preview 日志）: %s", label)
                if is_temp and os.path.exists(local_path):
                    os.remove(local_path)
            except Exception as exc:  # noqa: BLE001 —— 单篇失败不影响其余
                stats["failed"] += 1
                logger.warning("异常: %s: %s", label, exc)
    finally:
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass

    logger.info(
        "回填结束: 成功 %d / 跳过 %d / 失败 %d",
        stats["done"], stats["skipped"], stats["failed"],
    )
    return 1 if stats["failed"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="[jonex] 存量 office 文档 preview.pdf 回填",
    )
    parser.add_argument("--tenant", default="", help="租户 id（缺省=全部租户）")
    parser.add_argument("--kb-id", default="", help="知识库 id 过滤")
    parser.add_argument(
        "--doc-id", action="append", default=[],
        help="只处理指定文档 id（可重复）",
    )
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少篇（0=不限）")
    parser.add_argument(
        "--output-dir", default="",
        help="转换 PDF 的输出目录（缺省 OUTPUT_DIR）；填解析产物目录可让后续 reparse 复用同一 PDF",
    )
    parser.add_argument("--force", action="store_true", help="preview 已存在也重新生成")
    parser.add_argument("--dry-run", action="store_true", help="只列出待处理文档")
    parser.add_argument(
        "--env-file", default=str(_REPO_ROOT / ".env.local"),
        help="环境变量文件（缺省 仓库根/.env.local）",
    )
    parser.add_argument("--no-env-file", action="store_true", help="不加载任何 env 文件")
    args = parser.parse_args()

    if not args.no_env_file and args.env_file:
        load_env_file(args.env_file)

    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
