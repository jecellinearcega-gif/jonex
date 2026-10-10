"""Compile-time page graph + link/mention index for the reading API.

The platform used to build "what links here" at request time by scanning the
published ``wiki/*.md`` tree and re-parsing every frontmatter.  This module
moves that work into the compile step so the result becomes part of the
published bundle (``indexes/links.json`` and the ``page_graph`` block of
``query/navigation.json``) and is shared by the standalone service and the
platform integration.

The rendered frontmatter is the engine's own machine-written record (see
``rendering._render_page``): ``links``/``related``/``source_ids`` are
single-line JSON arrays and ``version`` is a bare integer, so they are parsed
with ``json.loads`` exactly like ``page_source_ids_from_markdown`` already
does.  Titles and aliases come from the structured ``page_index`` instead.

Two edge kinds, matching the platform frontend blue-link contract
(``wikiMarkdown.autoLinkMentions``):

- ``link``: a page's ``links[].target_id`` (compile-time navigation edges),
  reversed into in-links.
- ``mention``: the first occurrence of another page's title/alias inside a
  page body (>= 3 chars; ASCII names require word boundaries).

Raw content pages (keep-and-search) never enter the reading tree, so they are
excluded from the backlink index — the same prefixes the platform adapter used
to filter at runtime, now centralized for compile + read reuse.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple

from jonex_wiki.domain.engine.models.query_models import PageIndexEntry

# Raw content pages produced by the parser/compiler (full-article splits,
# complete-table cells, source-evidence spans).  They are searchable but do not
# appear in the Wiki reading tree, so they are excluded from backlinks.
RAW_PAGE_PREFIXES: Tuple[str, ...] = (
    "source-articles/",
    "raw-tables/",
    "source-evidence/",
)

_MENTION_MIN_LEN = 3

_FIELD_RE = re.compile(r"^(links|related|source_ids|version):\s*(.*?)\s*$", re.MULTILINE)


def is_raw_page(path: str) -> bool:
    """True for directory pages and raw keep-and-search content pages."""
    return (
        path == "index.md"
        or path.endswith("/index.md")
        or path.startswith(RAW_PAGE_PREFIXES)
    )


def _parse_frontmatter_fields(md: str) -> Dict[str, Any]:
    """Extract ``links``/``related``/``source_ids``/``version`` from the
    rendered frontmatter.  Unparseable fields fall back to empty values."""
    result: Dict[str, Any] = {
        "links": [],
        "related": [],
        "source_ids": [],
        "version": 0,
    }
    if not md.startswith("---"):
        return result
    fence = md.find("\n---", 3)
    if fence < 0:
        return result
    for key, raw_value in _FIELD_RE.findall(md, 0, fence):
        try:
            result[key] = json.loads(raw_value)
        except (ValueError, TypeError):
            continue
    return result


def _strip_frontmatter(md: str) -> str:
    """Return the body after the first YAML frontmatter block (``---``)."""
    if not md.startswith("---"):
        return md
    lines = md.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1 :])
    return md


def build_page_graph_and_links(
    page_index: Dict[str, PageIndexEntry],
    rendered: Dict[str, str],
    *,
    previous: Dict[str, Any] | None = None,
    previous_page_index: Dict[str, PageIndexEntry] | None = None,
    changed_paths: set[str] | None = None,
) -> Dict[str, Any]:
    """Build ``{"page_graph", "in_links", "mentions"}`` from the corpus.

    ``page_index`` is the full published page index (raw pages included).
    ``rendered`` is ``path -> rendered markdown (with frontmatter)``.
    """
    reading: Dict[str, PageIndexEntry] = {
        path: entry for path, entry in page_index.items() if not is_raw_page(path)
    }
    old_reading = {
        path: entry for path, entry in (previous_page_index or {}).items()
        if not is_raw_page(path)
    }
    candidates_unchanged = (
        previous is not None and changed_paths is not None
        and {path: (entry.title, tuple(entry.aliases)) for path, entry in reading.items()}
        == {path: (entry.title, tuple(entry.aliases)) for path, entry in old_reading.items()}
    )
    scan_paths = set(reading)
    if candidates_unchanged:
        scan_paths &= changed_paths
    from jonex_wiki.kernel.telemetry import count_work
    count_work("reading_pages_reused", len(reading) - len(scan_paths))
    count_work("reading_pages_scanned", len(scan_paths))

    # 1. page_graph covers every page (raw pages remain readable), rebuilt from
    #    the engine's own rendered frontmatter.
    page_graph: Dict[str, Dict[str, Any]] = {}
    for path in page_index:
        if (candidates_unchanged and path not in changed_paths
                and path in previous.get("page_graph", {})):
            page_graph[path] = previous["page_graph"][path]
            continue
        fields = _parse_frontmatter_fields(rendered.get(path, ""))
        page_graph[path] = {
            "version": fields["version"],
            "related_pages": fields["related"],
            "links": fields["links"],
            "source_ids": fields["source_ids"],
        }

    # 2. in_links: reverse compile-time navigation edges, reading tree only.
    if candidates_unchanged:
        in_links: Dict[str, List[Dict[str, str]]] = {
            target: [entry for entry in entries if entry.get("path") not in scan_paths]
            for target, entries in previous.get("in_links", {}).items()
            if target in reading
        }
    else:
        in_links = {}
    for path in reading:
        if path not in scan_paths:
            continue
        for link in page_graph[path]["links"]:
            if not isinstance(link, dict):
                continue
            target = link.get("target_id")
            if not target or target == path or target not in reading:
                continue
            entry = {"path": path}
            why_visit = link.get("why_visit")
            if why_visit:
                entry["why_visit"] = why_visit
            in_links.setdefault(target, []).append(entry)

    # 3. Raw-only changes do not rescan the reading tree. In that common case
    # avoid compiling a regex for every title/alias in the entire KB.
    anchored = defaultdict(list)
    if scan_paths:
        names = defaultdict(set)
        for path, entry in reading.items():
            for name in [entry.title, *entry.aliases]:
                name = (name or "").strip()
                if len(name) >= _MENTION_MIN_LEN:
                    names[name].add(path)
        # Every literal match contains its anchor. A rare three-character
        # substring narrows candidates without changing boundary verification.
        grams = {name: {name[i:i + 3] for i in range(len(name) - 2)} for name in names}
        counts = Counter(gram for keys in grams.values() for gram in keys)
        for name, targets in names.items():
            anchor = min(grams[name], key=lambda gram: (counts[gram], gram))
            escaped = re.escape(name)
            if all(ord(ch) < 128 for ch in name):
                escaped = rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
            anchored[anchor].append((re.compile(escaped), targets))
        del names, grams, counts

    protected_re = re.compile(r"\[[^\]]*\]\([^)]*\)|`[^`]*`")
    fence_re = re.compile(r"^\s*```")
    heading_re = re.compile(r"^\s*#{1,6}\s")
    wikilink_re = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")

    mentions: Dict[str, List[str]] = {}
    if candidates_unchanged:
        mentions = {
            target: [source for source in sources
                     if source in reading and source not in scan_paths]
            for target, sources in previous.get("mentions", {}).items()
            if target in reading
        }
    for path in reading:
        if path not in scan_paths:
            continue
        body = _strip_frontmatter(rendered.get(path, ""))
        used: set = set()
        in_fence = False
        for line in body.splitlines():
            if fence_re.match(line):
                in_fence = not in_fence
                continue
            if in_fence or heading_re.match(line):
                continue
            for segment in protected_re.split(line):
                if not segment:
                    continue
                for gram in {segment[i:i + 3] for i in range(len(segment) - 2)}:
                    for pattern, targets in anchored.get(gram, ()):
                        available = targets - used - {path}
                        if available and pattern.search(segment):
                            used.update(available)
                            for target in available:
                                mentions.setdefault(target, []).append(path)
        # wikilinks (e.g. "## Related pages") also count as mentions, matching
        # the frontend contract.
        for m in wikilink_re.finditer(body):
            target = m.group(1).strip().split("#")[0]
            if target and not target.endswith(".md"):
                target = f"{target}.md"
            if target in reading and target not in used:
                used.add(target)
                mentions.setdefault(target, []).append(path)

    # Replace one target at a time instead of duplicating the whole reverse graph.
    for target, paths in mentions.items():
        mentions[target] = sorted(set(paths))
    in_links = {
        target: sorted(entries, key=lambda e: e["path"])
        for target, entries in in_links.items() if entries
    }
    return {"page_graph": page_graph, "in_links": in_links, "mentions": mentions}
