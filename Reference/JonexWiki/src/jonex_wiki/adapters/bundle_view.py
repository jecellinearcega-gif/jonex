"""BundleView 读取协议在 dev 目录布局上的实现。

dev 目录布局：``Layout.published`` symlink → ``compile/versions/{bundle_sha}``，
内含 ``indexes/manifest.json``（BundleManifest：bundle_sha/files/dense/pages/segmenter）
与 ``revision.json``（tenant_id/kb_id/page_count/claim_count/doc_fingerprints/...）。

本适配器把该布局映射成 ``kernel.ports.BundleView``：identity 里
``revision_id == manifest_hash == manifest.bundle_sha``；``list_entries`` 从
``manifest.files`` 派生；``read_bytes`` 校验 sha256；``open_sqlite`` 以只读
immutable URI 打开。core 引擎读 ``view.manifest``（容器 manifest 形状），这里
在 dev revision.json/manifest.json 之上合成一个兼容 dict（fingerprints/counts）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Mapping, Tuple

from jonex_wiki.kernel.ports import BundleView


def _media_type(path: str) -> str:
    if path.endswith(".md"):
        return "text/markdown"
    if path.endswith(".json"):
        return "application/json"
    if path.endswith(".sqlite"):
        return "application/vnd.sqlite3"
    if path.endswith(".bin"):
        return "application/octet-stream"
    return "application/octet-stream"


_REVISION_HEADER_LOCK = RLock()


@lru_cache(maxsize=32)
def _cached_revision_header(path: str, file_identity: tuple) -> Dict[str, Any]:
    # The identity argument invalidates a replaced/edited file. Published versions
    # are immutable; only their small header is retained, never compiler history.
    payload = json.loads(Path(path).read_text("utf-8"))
    return {key: value for key, value in payload.items() if key != "compiler_state"}


def read_revision_header(revision_dir: str | Path) -> Dict[str, Any]:
    """Share one bounded header read across task entry and directory views."""
    path = (Path(revision_dir) / "revision.json").resolve()
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {}
    identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    # Serialize cold reads too: simultaneous first queries must not decode the
    # same large compiler history twice before the cache has been populated.
    with _REVISION_HEADER_LOCK:
        return dict(_cached_revision_header(str(path), identity))


class DirectoryBundleView(BundleView):
    """Read-only, revision-pinned view over one published dev directory bundle."""

    def __init__(
        self,
        *,
        tenant_id: str,
        context_id: str,
        revision_dir: str | Path,
    ) -> None:
        self._dir = Path(revision_dir)
        manifest_path = self._dir / "indexes" / "manifest.json"
        self._manifest_raw: Dict[str, Any] = json.loads(manifest_path.read_text("utf-8"))
        self._revision_raw = read_revision_header(self._dir)

        bundle_sha = self._manifest_raw.get("bundle_sha") or self._dir.name
        self.tenant_id = self._revision_raw.get("tenant_id", tenant_id)
        self.context_id = self._revision_raw.get("kb_id", context_id)
        # 双重身份（§7 L773）：manifest_hash 保持 dev 目录哈希（bundle_sha），
        # revision_id 用 core 内容派生身份（revision.json.revision_id = rev_*），
        # 供 decode_previous_files 校验（其内容哈希 corpus_sha256/dense.sha256 都在
        # rev_* 之上算出，不能换成 bundle_sha）。
        self.revision_id = self._revision_raw.get("revision_id") or bundle_sha
        self.manifest_hash = bundle_sha

        files_raw = self._manifest_raw.get("files", {})
        counts = {
            "sources": self._revision_raw.get("source_count", 0),
            "pages": self._revision_raw.get("page_count", 0),
            "claims": self._revision_raw.get("claim_count", 0),
            "open_errors": self._revision_raw.get("open_errors", 0),
        }
        # core-compatible container-manifest shape (engine reads view.manifest).
        self.manifest: Dict[str, Any] = {
            "format": "jonex-wiki-directory",
            "schema_version": 1,
            "revision_id": self.revision_id,
            "tenant_id": self.tenant_id,
            "context_id": self.context_id,
            "parent_revision": self._revision_raw.get("parent_revision"),
            "files": [
                {"path": p, "sha256": s, "size": 0, "media_type": _media_type(p)}
                for p, s in sorted(files_raw.items())
            ],
            "fingerprints": self._revision_raw.get("fingerprints") or {},
            "counts": counts,
        }

    def list_entries(self, prefix: str = "") -> List[str]:
        keys = list(self._manifest_raw.get("files", {}).keys())
        # indexes/manifest.json 因自指被排除在 files 清单外，但 core decode_previous_files
        # 需要读取它（lexical/dense 身份）；补回以兼容两套读者。
        if (self._dir / "indexes" / "manifest.json").is_file():
            keys.append("indexes/manifest.json")
        if prefix:
            keys = [k for k in keys if k.startswith(prefix)]
        return sorted(set(keys))

    def read_bytes(self, path: str) -> bytes:
        data = (self._dir / path).read_bytes()
        expected = self._manifest_raw.get("files", {}).get(path)
        if expected and hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"bundle artifact checksum mismatch: {path}")
        return data

    def verify_integrity(self) -> None:
        """Verify every file and the directory's content-addressed identity."""
        listed = self._manifest_raw.get("files")
        if not isinstance(listed, dict):
            raise ValueError("bundle file manifest is missing")
        actual = {
            path.relative_to(self._dir).as_posix()
            for path in self._dir.rglob("*") if path.is_file()
        } - {"indexes/manifest.json"}
        if actual != set(listed):
            raise ValueError("bundle file manifest differs from directory contents")
        digest = hashlib.sha256()
        for path in sorted(actual):
            with (self._dir / path).open("rb") as stream:
                data_hash = hashlib.file_digest(stream, "sha256").digest()
            if data_hash.hex() != listed[path]:
                raise ValueError(f"bundle artifact checksum mismatch: {path}")
            digest.update(path.encode())
            digest.update(b"\0")
            digest.update(data_hash)
        if digest.hexdigest() != self.manifest_hash or self.manifest_hash != self._dir.name:
            raise ValueError("bundle directory identity mismatch")

    def open_sqlite(self, path: str):
        # dev search.py opens FTS read/write today; this protocol pins read-only
        # immutable so a published bundle can never be mutated by a query.
        target = (self._dir / path).resolve()
        uri = f"file:{target}?mode=ro&immutable=1"
        return sqlite3.connect(uri, uri=True)

    def resolve_evidence(self, ref: Mapping[str, Any]) -> Dict[str, Any]:
        # table_reader.py reads tables straight through open_sqlite; no live
        # call site yet, so keep the contract explicit rather than guessing.
        raise NotImplementedError("resolve_evidence is not wired for the dev layout yet")
