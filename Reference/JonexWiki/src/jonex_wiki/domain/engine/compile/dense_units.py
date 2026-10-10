"""Bounded full-text embedding with deterministic page aggregation."""
import math
import hashlib
from jonex_wiki.kernel.runtime_env import current_env
from jonex_wiki.kernel.telemetry import count_work
from jonex_wiki.kernel.ports import CancelPort

PREPROCESSING = 'bounded-512-weighted-mean-v1'


def split_units(text):
    return [text[i:i + 512] for i in range(0, len(text), 512)] or [' ']


async def embed_page_units(page_text, model, model_name, *, cache=None, output_cache=None,
                           cancel: CancelPort | None = None, scene: str | None = None, **routing):
    units, by_page = {}, {}
    for path, text in sorted(page_text.items()):
        chunks = split_units(text)
        by_page[path] = chunks
        for chunk in chunks:
            units.setdefault(chunk, None)
    if not units:
        return {}
    cache = cache or {}
    key = lambda text: hashlib.sha256(text.encode()).hexdigest()
    for text in units:
        if key(text) in cache:
            units[text] = list(cache[key(text)])
    count_work('dense_text_units_reused', sum(v is not None for v in units.values()))
    texts = [text for text, value in units.items() if value is None]
    dimensions = {len(v) for v in units.values() if v is not None}
    try:
        batch_size = int(current_env().get('JONEX_WIKI_EMBED_BATCH_SIZE', '16'))
    except ValueError:
        batch_size = 16
    batch_size = max(1, min(50, batch_size))
    for start in range(0, len(texts), batch_size):
        if cancel:
            cancel.raise_if_cancelled()
        batch = texts[start:start + batch_size]
        count_work('embedding_rpc')
        count_work('embedding_text_units', len(batch))
        vectors = None
        try:
            retry_limit = int(current_env().get('JONEX_WIKI_EMBED_BATCH_ATTEMPTS', '3'))
        except ValueError:
            retry_limit = 3
        retry_limit = max(1, min(5, retry_limit))
        for _ in range(retry_limit):
            vectors = await model.embed(batch, model=model_name or None, scene=scene, **routing)
            if vectors is not None:
                break
        if vectors is None:
            return None
        if len(vectors) != len(batch):
            raise ValueError('embedding backend returned the wrong vector count')
        for text, vector in zip(batch, vectors):
            if (not isinstance(vector, list) or not vector or any(
                    isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                    for v in vector)):
                raise ValueError('embedding backend returned an invalid vector')
            dimensions.add(len(vector))
            units[text] = vector
            if output_cache is not None:
                # Preserve completed units even if a later remote batch fails;
                # the degraded Bundle can seed the next incremental attempt.
                output_cache[key(text)] = list(vector)
    if len(dimensions) != 1:
        raise ValueError('embedding backend returned mixed vector dimensions')
    if output_cache is not None:
        output_cache.update({key(t): list(v) for t, v in units.items()})
    dim = next(iter(dimensions))
    return {path: [sum(units[c][i] * len(c) for c in chunks) / sum(map(len, chunks))
                  for i in range(dim)] for path, chunks in by_page.items()}
