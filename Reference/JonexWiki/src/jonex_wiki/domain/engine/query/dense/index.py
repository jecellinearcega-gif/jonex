"""Validated, reusable exact dense matrix for one immutable query run."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

try:  # NumPy is the production fast path; the strict Python oracle stays valid.
    import numpy as _np
except ImportError:  # pragma: no cover - environment dependent
    _np = None  # type: ignore[assignment]


def _finite_vector(
    value: Any,
    *,
    empty_marker: str,
    value_marker: str,
) -> list[float]:
    if isinstance(value, (str, bytes, bytearray)):
        raise ValueError(value_marker)
    try:
        items = list(value)
    except TypeError as exc:
        raise ValueError(value_marker) from exc
    if not items:
        raise ValueError(empty_marker)
    if any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(float(item))
        for item in items
    ):
        raise ValueError(value_marker)
    return [float(item) for item in items]


def validate_page_vectors(
    page_vectors: Mapping[str, Sequence[float]],
) -> tuple[tuple[str, ...], tuple[tuple[float, ...], ...], int]:
    """Return canonical validated rows without silently truncating dimensions."""

    if not isinstance(page_vectors, Mapping) or not page_vectors:
        raise ValueError("DENSE_PAGE_VECTORS_EMPTY")
    paths: list[str] = []
    rows: list[tuple[float, ...]] = []
    dimension: int | None = None
    for path, vector in page_vectors.items():
        if not isinstance(path, str) or not path:
            raise ValueError("DENSE_PAGE_PATH_INVALID")
        row = _finite_vector(
            vector,
            empty_marker="DENSE_PAGE_VECTOR_EMPTY",
            value_marker="DENSE_PAGE_VALUE_INVALID",
        )
        if dimension is None:
            dimension = len(row)
        elif len(row) != dimension:
            raise ValueError("DENSE_PAGE_DIMENSION_MISMATCH")
        paths.append(path)
        rows.append(tuple(row))
    assert dimension is not None
    return tuple(paths), tuple(rows), dimension


def validate_query_vector(value: Sequence[float], *, dimension: int) -> list[float]:
    """Validate one query vector against the frozen page-vector dimension."""

    query = _finite_vector(
        value,
        empty_marker="DENSE_QUERY_VECTOR_EMPTY",
        value_marker="DENSE_QUERY_VALUE_INVALID",
    )
    if len(query) != dimension:
        raise ValueError("DENSE_QUERY_DIMENSION_MISMATCH")
    return query


@dataclass(frozen=True)
class PreparedDenseIndex:
    """One pre-normalized exact-scan index reused by every search hop."""

    paths: tuple[str, ...]
    dimension: int
    _storage: Any = field(repr=False, compare=False)
    _numpy_backed: bool = field(repr=False)

    @property
    def row_count(self) -> int:
        return len(self.paths)

    @property
    def layout(self) -> str:
        return (
            "contiguous-normalized-float32"
            if self._numpy_backed
            else "normalized-python-tuples"
        )

    @property
    def storage_contiguous(self) -> bool:
        return bool(
            self._numpy_backed
            and getattr(getattr(self._storage, "flags", None), "c_contiguous", False)
        )

    @property
    def storage_identity(self) -> int:
        """Expose object identity for reuse assertions, never matrix contents."""

        return id(self._storage)

    @property
    def storage_bytes(self) -> int | None:
        value = getattr(self._storage, "nbytes", None)
        return int(value) if value is not None else None

    def score(self, query_vector: Sequence[float]) -> dict[str, float]:
        query = validate_query_vector(query_vector, dimension=self.dimension)
        if self._numpy_backed:
            assert _np is not None
            query_array = _np.asarray(query, dtype=_np.float32)
            query_array = query_array / (
                float(_np.linalg.norm(query_array)) + 1e-9
            )
            scores = self._storage @ query_array
            return {
                path: float(scores[index])
                for index, path in enumerate(self.paths)
            }

        query_norm = math.sqrt(sum(item * item for item in query)) or 1.0
        normalized_query = tuple(item / query_norm for item in query)
        return {
            path: sum(
                self._storage[row_index][column] * normalized_query[column]
                for column in range(self.dimension)
            )
            for row_index, path in enumerate(self.paths)
        }


def prepare_dense_index(
    page_vectors: Mapping[str, Sequence[float]],
) -> PreparedDenseIndex:
    """Validate and normalize page vectors exactly once for a query run."""

    paths, rows, dimension = validate_page_vectors(page_vectors)
    if _np is not None:
        matrix = _np.ascontiguousarray(rows, dtype=_np.float32)
        norms = _np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
        normalized = _np.ascontiguousarray(matrix / norms, dtype=_np.float32)
        return PreparedDenseIndex(
            paths=paths,
            dimension=dimension,
            _storage=normalized,
            _numpy_backed=True,
        )

    normalized_rows: list[tuple[float, ...]] = []
    for row in rows:
        norm = math.sqrt(sum(item * item for item in row)) or 1.0
        normalized_rows.append(tuple(item / norm for item in row))
    return PreparedDenseIndex(
        paths=paths,
        dimension=dimension,
        _storage=tuple(normalized_rows),
        _numpy_backed=False,
    )


def prepare_published_dense_index(
    page_vectors: Mapping[str, Sequence[float]],
) -> PreparedDenseIndex:
    """Prepare vectors already validated by the canonical bundle decoder.

    The decoder has already rejected non-numeric JSON values.  This boundary
    still rechecks membership shape, one positive dimension, and finite values,
    but uses NumPy for those repeated checks instead of walking every scalar in
    Python on every query.  Non-NumPy environments use the fully strict oracle.
    """

    if _np is None:
        return prepare_dense_index(page_vectors)
    if not isinstance(page_vectors, Mapping) or not page_vectors:
        raise ValueError("DENSE_PAGE_VECTORS_EMPTY")
    paths: list[str] = []
    raw_rows: list[Any] = []
    for path, vector in page_vectors.items():
        if not isinstance(path, str) or not path:
            raise ValueError("DENSE_PAGE_PATH_INVALID")
        if isinstance(vector, (str, bytes, bytearray)):
            raise ValueError("DENSE_PAGE_VALUE_INVALID")
        try:
            if len(vector) == 0:
                raise ValueError("DENSE_PAGE_VECTOR_EMPTY")
        except TypeError as exc:
            raise ValueError("DENSE_PAGE_VALUE_INVALID") from exc
        paths.append(path)
        raw_rows.append(vector)
    try:
        matrix = _np.asarray(raw_rows, dtype=_np.float32)
    except (TypeError, ValueError) as exc:
        raise ValueError("DENSE_PAGE_DIMENSION_MISMATCH") from exc
    if matrix.ndim != 2 or matrix.shape[0] != len(paths):
        raise ValueError("DENSE_PAGE_DIMENSION_MISMATCH")
    if matrix.shape[1] < 1:
        raise ValueError("DENSE_PAGE_VECTOR_EMPTY")
    if not bool(_np.isfinite(matrix).all()):
        raise ValueError("DENSE_PAGE_VALUE_INVALID")
    matrix = _np.ascontiguousarray(matrix, dtype=_np.float32)
    norms = _np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    normalized = _np.ascontiguousarray(matrix / norms, dtype=_np.float32)
    return PreparedDenseIndex(
        paths=tuple(paths),
        dimension=int(matrix.shape[1]),
        _storage=normalized,
        _numpy_backed=True,
    )


__all__ = [
    "PreparedDenseIndex",
    "prepare_dense_index",
    "prepare_published_dense_index",
    "validate_page_vectors",
    "validate_query_vector",
]
