"""Tenant-bound immutable identity and atomic storage for dense query caches."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from jonex_wiki.domain.engine.query.dense.index import validate_page_vectors

CACHE_SCHEMA = "llmwiki.dense_cache.v1"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True)
class DenseCacheBaseIdentity:
    """All cache-key fields except the provider-reported vector dimension."""

    tenant_sha256: str
    context_sha256: str
    content_sha256: str
    model: str
    model_revision: str
    normalization: str
    _page_paths: tuple[str, ...] = field(repr=False, compare=False)

    def as_dict(self) -> dict[str, object]:
        return {
            "context_sha256": self.context_sha256,
            "content_sha256": self.content_sha256,
            "model": self.model,
            "model_revision": self.model_revision,
            "normalization": self.normalization,
            "tenant_sha256": self.tenant_sha256,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.as_dict())).hexdigest()


def build_dense_cache_identity(
    page_text: Mapping[str, str],
    *,
    tenant_id: str,
    context_id: str,
    model: str,
    model_revision: str | None = None,
    normalization: str = "raw-float-v1",
) -> DenseCacheBaseIdentity:
    """Hash the complete corpus and binding identity without truncation."""

    if not isinstance(page_text, Mapping) or not page_text:
        raise ValueError("DENSE_CACHE_CONTENT_EMPTY")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("DENSE_CACHE_MODEL_INVALID")
    revision = model_revision if model_revision is not None else model
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("DENSE_CACHE_MODEL_REVISION_INVALID")
    if not isinstance(normalization, str) or not normalization.strip():
        raise ValueError("DENSE_CACHE_NORMALIZATION_INVALID")
    content_rows: list[list[str]] = []
    for path in sorted(page_text):
        text = page_text[path]
        if not isinstance(path, str) or not path:
            raise ValueError("DENSE_CACHE_PATH_INVALID")
        if not isinstance(text, str):
            raise ValueError("DENSE_CACHE_TEXT_INVALID")
        content_rows.append([path, _sha256_text(text)])
    return DenseCacheBaseIdentity(
        tenant_sha256=_sha256_text(tenant_id),
        context_sha256=_sha256_text(context_id),
        content_sha256=hashlib.sha256(_canonical_bytes(content_rows)).hexdigest(),
        model=model.strip(),
        model_revision=revision.strip(),
        normalization=normalization.strip(),
        _page_paths=tuple(sorted(page_text)),
    )


def _cache_file(
    root: Path, identity: DenseCacheBaseIdentity, dimension: int
) -> Path:
    return root / identity.digest / "dimension-{}.json".format(dimension)


def load_dense_cache(
    root: Path,
    identity: DenseCacheBaseIdentity,
    *,
    dimension: int | None = None,
) -> dict[str, list[float]] | None:
    """Return one exactly matching cache entry, or ``None`` on any drift."""

    directory = root / identity.digest
    if dimension is not None:
        candidates = [_cache_file(root, identity, dimension)]
    else:
        try:
            candidates = sorted(directory.glob("dimension-*.json"))
        except OSError:
            return None
        if len(candidates) != 1:
            return None
    path = candidates[0]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != CACHE_SCHEMA:
            return None
        stored_identity = payload.get("identity")
        if not isinstance(stored_identity, dict):
            return None
        stored_dimension = stored_identity.get("dimension")
        if (
            not isinstance(stored_dimension, int)
            or isinstance(stored_dimension, bool)
            or stored_dimension < 1
        ):
            return None
        expected_identity = {**identity.as_dict(), "dimension": stored_dimension}
        if stored_identity != expected_identity:
            return None
        if dimension is not None and stored_dimension != dimension:
            return None
        vectors = payload.get("vectors")
        paths, rows, actual_dimension = validate_page_vectors(vectors)
        if paths != identity._page_paths or actual_dimension != stored_dimension:
            return None
        return {
            path_name: list(rows[index])
            for index, path_name in enumerate(paths)
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def store_dense_cache(
    root: Path,
    identity: DenseCacheBaseIdentity,
    page_vectors: Mapping[str, Sequence[float]],
) -> Path:
    """Atomically replace one exact identity+dimension cache entry."""

    paths, rows, dimension = validate_page_vectors(page_vectors)
    if paths != identity._page_paths:
        raise ValueError("DENSE_CACHE_PATHS_MISMATCH")
    path = _cache_file(root, identity, dimension)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "identity": {**identity.as_dict(), "dimension": dimension},
        "schema_version": CACHE_SCHEMA,
        "vectors": {
            path_name: list(rows[index])
            for index, path_name in enumerate(paths)
        },
    }
    serialized = _canonical_bytes(payload)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=".dense-cache-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass
    return path


__all__ = [
    "CACHE_SCHEMA",
    "DenseCacheBaseIdentity",
    "build_dense_cache_identity",
    "load_dense_cache",
    "store_dense_cache",
]
