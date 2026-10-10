/**
 * [jonex] Wiki 列式图谱 = 「列式浏览」（Miller columns）。
 *
 * 形态与平台既有链路（领域本体 → 列式浏览）**完全一致**：点一张卡片就向右追加一列，
 * 列间用贝塞尔曲线连起来，已出现过的节点置灰去环。基础件直接复用 `columnBoard/`，
 * 不另写一套视觉。
 *
 * 列的内容按「读者在 Wiki 里怎么找东西」安排：
 *   ① 目录主题（无目录时退化为分类）→ ② 该主题的章节条目（按目录顺序编号）
 *   → ③ 这一页的关联（页间链接 / 正文提及 / 链入 / 源文档，带方向徽标）
 *   → ④+ 继续下钻：关联页 → 它自己的关联；源文档 → 它支撑的断言；
 *        表格页 → 行/品类 → 该行在各日期的取值。
 *
 * 旧的「对比表 / 时间轴矩阵 / 溯源审计」三个独立表格界面已并入这条下钻路径
 * （矩阵与审计只保留在本体链路的表格视图里，Wiki 侧不再重复造表格）。
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Segmented, Spin } from 'antd';
import { getWikiBacklinks, getWikiClaims, getWikiOutline, getWikiReaderPage } from '@/api/wikiReader';
import type {
  WikiClaim,
  WikiOutlineView,
  WikiPageContent,
  WikiReaderPage,
} from '@/types/domainKnowledge';
import {
  ColumnEmpty,
  ColumnError,
  ColumnLoading,
  ColumnShell,
  ColumnStage,
  EMPTY_SET,
  NodeCard,
  useAppearedKeys,
  useColumnConnectors,
  type ColumnItem,
} from '../columnBoard/parts';
import { buildLinkGroups } from './wikiRelations';
import { LINK_BADGE_KEY, LINK_KIND_COLOR } from './wikiLinkStyle';
import { analyzePageLinks } from './wikiMarkdown';
import { ROOT_ID, buildOutlineTree, type OutlineNodeLike } from './wikiOutlineData';
import { buildTimelineMatrix, type TimelineMatrix } from './wikiColumnar';

const FETCH_CONCURRENCY = 6;
/** 关联列最多展示的条目数（超出的按分组截断，避免一列上百张卡） */
const LINKS_PER_GROUP = 30;
/** 断言列上限 */
const CLAIM_LIMIT = 200;

type WikiColumnPayload =
  | { kind: 'topics' }
  | { kind: 'entries'; topicId: string }
  | { kind: 'links'; path: string }
  | { kind: 'rows'; path: string }
  | { kind: 'cells'; path: string; rowLabel: string }
  | { kind: 'claims'; documentId: string };

type WikiColumnDesc = WikiColumnPayload & { key: string; title: string };

interface WikiColumnBoardProps {
  kbId: string;
  documentId?: string;
  /** 已加载的目录视图（阅读页首屏已有，不必重复请求） */
  view?: WikiOutlineView | null;
  height?: string | number;
  onOpenPage?: (path: string) => void;
  onOpenDocument?: (documentId: string) => void;
}

/** 目录节点 → 扁平的可下钻条目（含小节，按目录顺序） */
function entriesOf(node: OutlineNodeLike | undefined): { path: string; title: string; role?: string; note?: string }[] {
  if (!node) return [];
  const out: { path: string; title: string; role?: string; note?: string }[] = [];
  const walk = (n: OutlineNodeLike) => {
    for (const entry of n.entries || []) {
      out.push({
        path: entry.path,
        title: entry.title || entry.path,
        role: (entry as { role?: string }).role,
        note: (entry as { note?: string }).note,
      });
    }
    for (const child of n.children || []) walk(child);
  };
  walk(node);
  return out;
}

export default function WikiColumnBoard({
  kbId,
  documentId = '',
  view: viewProp,
  height = 'calc(100vh - 400px)',
  onOpenPage,
  onOpenDocument,
}: WikiColumnBoardProps) {
  const { t } = useTranslation();
  const seqRef = useRef(1);
  const [innerView, setInnerView] = useState<WikiOutlineView | null>(viewProp ?? null);
  const [loadError, setLoadError] = useState('');
  const [columns, setColumns] = useState<WikiColumnDesc[]>([]);
  const [colItemsMap, setColItemsMap] = useState<Record<string, Set<string>>>({});
  const [docs, setDocs] = useState<Record<string, WikiPageContent>>({});

  const connectors = useColumnConnectors(columns.length);
  const scheduleConnectors = connectors.scheduleConnectors;

  const view = viewProp ?? innerView;
  const pages = useMemo(() => view?.pages || [], [view]);
  const pagesByPath = useMemo(() => new Map(pages.map((p) => [p.path, p])), [pages]);

  // 没有外部传入目录时自己取（文档级面板与知识库级共用同一个接口，document_id 决定范围）
  useEffect(() => {
    if (viewProp) {
      setInnerView(viewProp);
      return;
    }
    let alive = true;
    getWikiOutline(kbId, documentId || undefined)
      .then((res) => {
        if (alive) {
          setInnerView(res);
          setLoadError('');
        }
      })
      .catch((err) => {
        if (alive) setLoadError((err as Error)?.message || t('common.loadFailed'));
      });
    return () => {
      alive = false;
    };
  }, [kbId, documentId, viewProp, t]);

  // 首列：目录主题（无目录时退化为分类）
  useEffect(() => {
    if (!view || columns.length) return;
    setColumns([{ key: 'col-0', kind: 'topics', title: t('domainKnowledge.wikiColumns.topics') }]);
  }, [view, columns.length, t]);

  /** 批量读页面（带缓存 + 并发上限），关联列与表格列共用 */
  const loadDocs = useCallback(
    async (paths: string[]): Promise<void> => {
      const todo = paths.filter((p) => p && !docs[p]);
      for (let i = 0; i < todo.length; i += FETCH_CONCURRENCY) {
        const chunk = todo.slice(i, i + FETCH_CONCURRENCY);
        const results = await Promise.all(chunk.map((p) => getWikiReaderPage(kbId, p).catch(() => null)));
        const fetched: Record<string, WikiPageContent> = {};
        results.forEach((res, idx) => {
          if (res) fetched[chunk[idx]] = res;
        });
        if (Object.keys(fetched).length) setDocs((prev) => ({ ...prev, ...fetched }));
      }
    },
    [kbId, docs],
  );

  const handleDrill = useCallback((index: number, payload: WikiColumnPayload, title: string) => {
    const key = `col-${seqRef.current++}`;
    setColumns((prev) => [...prev.slice(0, index + 1), { ...payload, key, title }]);
  }, []);

  const handleColItems = useCallback((colKey: string, keys: Set<string>) => {
    setColItemsMap((prev) => ({ ...prev, [colKey]: keys }));
  }, []);

  const appearedByCol = useAppearedKeys(
    columns.map((c) => c.key),
    colItemsMap,
  );

  // top-level 目录主题 -> 节点映射（entries 列按 topicId 取内容）
  const topicById = useMemo(() => {
    const map = new Map<string, OutlineNodeLike>();
    for (const node of view?.outline?.topics || []) map.set(node.id, node as unknown as OutlineNodeLike);
    return map;
  }, [view]);

  if (loadError) {
    return (
      <Alert
        type="error"
        showIcon
        message={t('common.loadFailed')}
        description={loadError}
        style={{ margin: 24 }}
      />
    );
  }
  if (!view) {
    return (
      <div style={{ padding: 48, textAlign: 'center' }}>
        <Spin />
      </div>
    );
  }

  return (
    <ColumnStage connectors={connectors} height={height}>
      {columns.map((col, i) => {
        const common = {
          kbId,
          documentId,
          colKey: col.key,
          grayedKeys: appearedByCol[i] ?? EMPTY_SET,
          onItemsChange: handleColItems,
          onDrill: (payload: WikiColumnPayload, title: string) => handleDrill(i, payload, title),
          onChanged: scheduleConnectors,
        };
        if (col.kind === 'topics') {
          return (
            <TopicsColumn
              key={col.key}
              {...common}
              outline={view.outline}
              pages={pages}
            />
          );
        }
        if (col.kind === 'entries') {
          const node = topicById.get(col.topicId);
          return (
            <EntriesColumn
              key={col.key}
              {...common}
              title={col.title}
              entries={node ? entriesOf(node) : entriesOfFallback(pages, col.topicId)}
              pagesByPath={pagesByPath}
            />
          );
        }
        if (col.kind === 'links') {
          return (
            <LinksColumn
              key={col.key}
              {...common}
              path={col.path}
              title={col.title}
              pages={pages}
              pagesByPath={pagesByPath}
              docs={docs}
              loadDocs={loadDocs}
              onOpenPage={onOpenPage}
              onOpenDocument={onOpenDocument}
            />
          );
        }
        if (col.kind === 'rows') {
          return (
            <RowsColumn
              key={col.key}
              {...common}
              path={col.path}
              title={col.title}
              docs={docs}
              loadDocs={loadDocs}
            />
          );
        }
        if (col.kind === 'cells') {
          return (
            <CellsColumn
              key={col.key}
              {...common}
              path={col.path}
              rowLabel={col.rowLabel}
              title={col.title}
              docs={docs}
              loadDocs={loadDocs}
            />
          );
        }
        return (
          <ClaimsColumn
            key={col.key}
            {...common}
            documentId={col.documentId}
            title={col.title}
            onOpenPage={onOpenPage}
          />
        );
      })}
    </ColumnStage>
  );
}

interface ColumnCommonProps {
  kbId: string;
  documentId: string;
  colKey: string;
  grayedKeys: ReadonlySet<string>;
  onItemsChange: (colKey: string, keys: Set<string>) => void;
  onDrill: (payload: WikiColumnPayload, title: string) => void;
  onChanged: () => void;
}

/** 无目录时的主题列：按分类分组（与主题地图的兜底同源，顺序也一致） */
function entriesOfFallback(pages: WikiReaderPage[], topicId: string) {
  const category = topicId.startsWith('cat:') ? topicId.slice(4) : topicId;
  return pages
    .filter((p) => (p.category || 'unclassified') === category)
    .sort((a, b) => (a.title || '').localeCompare(b.title || '', 'zh-CN'))
    .map((p) => ({ path: p.path, title: p.title || p.path, role: p.type }));
}

/** 渲染完成后重画连线 + 上报本列节点（置灰去环的依据） */
function useColumnItems(
  items: ColumnItem[],
  colKey: string,
  onItemsChange: ColumnCommonProps['onItemsChange'],
  onChanged: () => void,
  extraKey?: string,
) {
  useEffect(() => {
    onItemsChange(colKey, new Set(items.map((it) => it.key)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [colKey, items]);
  useEffect(() => {
    onChanged();
  }, [items, extraKey, onChanged]);
}

// ── ① 目录主题（无目录 → 分类）─────────────────────────────

function TopicsColumn({
  outline,
  pages,
  colKey,
  grayedKeys,
  onItemsChange,
  onDrill,
  onChanged,
}: ColumnCommonProps & { outline: WikiOutlineView['outline']; pages: WikiReaderPage[] }) {
  const { t } = useTranslation();
  const [keyword, setKeyword] = useState('');
  const [selected, setSelected] = useState<string | null>(null);

  const items = useMemo<ColumnItem[]>(() => {
    const tree = buildOutlineTree({
      outline: outline ? { topics: outline.topics as unknown as OutlineNodeLike[] } : null,
      pages,
      expanded: new Set([ROOT_ID]),
    });
    return (tree.children || []).map((node) => ({
      key: node.id,
      label: node.label,
      sub: t('domainKnowledge.wikiColumns.pageCount', { count: node.count ?? 0 }),
      isType: true,
    }));
  }, [outline, pages, t]);

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return kw ? items.filter((it) => it.label.toLowerCase().includes(kw)) : items;
  }, [items, keyword]);

  useColumnItems(items, colKey, onItemsChange, onChanged, keyword);

  return (
    <ColumnShell
      kicker={t('domainKnowledge.wikiColumns.outline')}
      title={t('domainKnowledge.wikiColumns.topics')}
      count={filtered.length}
      searchable
      search={keyword}
      onSearch={setKeyword}
    >
      {filtered.length === 0 ? (
        <ColumnEmpty text={t('domainKnowledge.wikiColumns.emptyTopics')} />
      ) : (
        filtered.map((it) => (
          <NodeCard
            key={it.key}
            selected={selected === it.key}
            grayed={grayedKeys.has(it.key)}
            onClick={() => {
              setSelected(it.key);
              onDrill({ kind: 'entries', topicId: it.key }, it.label);
            }}
            item={it}
          />
        ))
      )}
    </ColumnShell>
  );
}

// ── ② 章节条目（按目录顺序编号）───────────────────────────

function EntriesColumn({
  title,
  entries,
  pagesByPath,
  colKey,
  grayedKeys,
  onItemsChange,
  onDrill,
  onChanged,
}: ColumnCommonProps & {
  title: string;
  entries: { path: string; title: string; role?: string; note?: string }[];
  pagesByPath: Map<string, WikiReaderPage>;
}) {
  const { t } = useTranslation();
  const [keyword, setKeyword] = useState('');
  const [selected, setSelected] = useState<string | null>(null);

  const items = useMemo<ColumnItem[]>(
    () =>
      entries
        // 目录可能引用了已不存在的页面（编译产物换代）→ 这里以页面清单为准
        .filter((entry) => pagesByPath.has(entry.path))
        .map((entry, index) => {
          const page = pagesByPath.get(entry.path);
          return {
            key: entry.path,
            label: entry.title,
            // 序号来自目录顺序：这是「像课本」的那条线索，列式视图也必须保留
            sub: `${String(index + 1).padStart(2, '0')} · ${page?.type || entry.role || ''}`,
            tag: page?.category,
            desc: entry.note,
          };
        }),
    [entries, pagesByPath],
  );

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return kw
      ? items.filter(
          (it) => it.label.toLowerCase().includes(kw) || (it.tag || '').toLowerCase().includes(kw),
        )
      : items;
  }, [items, keyword]);

  useColumnItems(items, colKey, onItemsChange, onChanged, keyword);

  return (
    <ColumnShell
      kicker={t('domainKnowledge.wikiColumns.entries')}
      title={title}
      count={filtered.length}
      searchable
      search={keyword}
      onSearch={setKeyword}
    >
      {filtered.length === 0 ? (
        <ColumnEmpty text={t('domainKnowledge.wikiColumns.emptyEntries')} />
      ) : (
        filtered.map((it) => (
          <NodeCard
            key={it.key}
            selected={selected === it.key}
            grayed={grayedKeys.has(it.key)}
            onClick={() => {
              setSelected(it.key);
              onDrill({ kind: 'links', path: it.key }, it.label);
            }}
            item={it}
          />
        ))
      )}
    </ColumnShell>
  );
}

// ── ③ 本页关联（页间链接 / 提及 / 链入 / 源文档）────────────

function LinksColumn({
  kbId,
  path,
  title,
  pages,
  pagesByPath,
  docs,
  loadDocs,
  onOpenPage,
  onOpenDocument,
  colKey,
  grayedKeys,
  onItemsChange,
  onDrill,
  onChanged,
}: ColumnCommonProps & {
  path: string;
  title: string;
  pages: WikiReaderPage[];
  pagesByPath: Map<string, WikiReaderPage>;
  docs: Record<string, WikiPageContent>;
  loadDocs: (paths: string[]) => Promise<void>;
  onOpenPage?: (path: string) => void;
  onOpenDocument?: (documentId: string) => void;
}) {
  const { t } = useTranslation();
  const [backlinks, setBacklinks] = useState<Awaited<ReturnType<typeof getWikiBacklinks>> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<string | null>(null);

  const page = docs[path];
  const pageKey = path;

  useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all([loadDocs([pageKey]), getWikiBacklinks(kbId, path).catch(() => null)])
      .then(([, backs]) => {
        if (!alive) return;
        setBacklinks(backs);
        setError('');
      })
      .catch((err) => {
        if (alive) setError((err as Error)?.message || '');
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, kbId]);

  // 关系分组：与条目页右栏、星形图共用同一模型（顺序与配色都一致）
  const groups = useMemo(() => {
    if (!page) return [];
    const analysis = analyzePageLinks(page.content || '', pages, page.enginePath || page.path);
    const siblings = pages
      .filter((p) => p.category && p.category === pagesByPath.get(path)?.category && p.path !== path)
      .slice(0, 6);
    return buildLinkGroups({
      page,
      pagesByPath,
      mentionPaths: analysis.mentionPaths,
      backlinks: backlinks?.items || [],
      seeAlso: siblings,
    });
  }, [page, pages, pagesByPath, backlinks, path]);

  const rows = useMemo(() => {
    const seen = new Set<string>();
    const out: { item: ColumnItem; payload: WikiColumnPayload; open?: () => void }[] = [];
    for (const group of groups) {
      for (const link of group.items.slice(0, LINKS_PER_GROUP)) {
        if (seen.has(link.path)) continue;
        seen.add(link.path);
        const target = pagesByPath.get(link.path);
        const isTable = (target?.type || '') === 'table';
        // 源文档不是页面，下钻 = 看它支撑的断言；双击 = 打开原始文档
        const payload: WikiColumnPayload =
          group.kind === 'source'
            ? { kind: 'claims', documentId: link.path }
            : isTable
              ? { kind: 'rows', path: link.path }
              : { kind: 'links', path: link.path };
        const open =
          group.kind === 'source'
            ? () => onOpenDocument?.(link.path)
            : () => onOpenPage?.(link.path);
        out.push({
          item: {
            key: group.kind === 'source' ? `doc:${link.path}` : link.path,
            label: link.title,
            sub: target?.category,
            tag: target?.type,
            desc: link.note,
            relation: t(LINK_BADGE_KEY[group.kind]),
          },
          payload,
          open,
        });
      }
    }
    return out;
  }, [groups, pagesByPath, t, onOpenDocument, onOpenPage]);

  useColumnItems(
    rows.map((r) => r.item),
    colKey,
    onItemsChange,
    onChanged,
  );

  if (loading && !page) {
    return (
      <ColumnShell kicker={t('domainKnowledge.wikiColumns.relations')} title={title} count={0}>
        <ColumnLoading />
      </ColumnShell>
    );
  }
  if (error) {
    return (
      <ColumnShell kicker={t('domainKnowledge.wikiColumns.relations')} title={title} count={0}>
        <ColumnError message={error} onRetry={() => setError('')} />
      </ColumnShell>
    );
  }

  return (
    <ColumnShell
      kicker={t('domainKnowledge.wikiColumns.relations')}
      title={title}
      count={rows.length}
    >
      {rows.length === 0 ? (
        <ColumnEmpty text={t('domainKnowledge.wikiColumns.emptyRelations')} />
      ) : (
        <>
          {rows.map(({ item, payload, open }) => (
            <NodeCard
              key={item.key}
              selected={selected === item.key}
              grayed={grayedKeys.has(item.key)}
              onClick={() => {
                setSelected(item.key);
                onDrill(payload, item.label);
              }}
              onOpen={open}
              openHint={t('domainKnowledge.wikiColumns.openHint')}
              item={item}
            />
          ))}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, padding: '8px 2px 0' }}>
            {groups.map((group) => (
              <span key={group.kind} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                <span
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: '50%',
                    background: LINK_KIND_COLOR[group.kind],
                  }}
                />
                <span style={{ fontSize: 10, color: '#94a3b8' }}>
                  {t(LINK_BADGE_KEY[group.kind])} {group.items.length}
                </span>
              </span>
            ))}
          </div>
        </>
      )}
    </ColumnShell>
  );
}

// ── ④ 表格页 → 行/品类 ────────────────────────────────────

function RowsColumn({
  path,
  title,
  docs,
  loadDocs,
  colKey,
  grayedKeys,
  onItemsChange,
  onDrill,
  onChanged,
}: ColumnCommonProps & {
  path: string;
  title: string;
  docs: Record<string, WikiPageContent>;
  loadDocs: (paths: string[]) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState<string | null>(null);
  const page = docs[path];

  useEffect(() => {
    void loadDocs([path]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  const matrix = useMemo<TimelineMatrix | null>(() => {
    if (!page) return null;
    return buildTimelineMatrix([
      {
        path: page.enginePath || page.path,
        title: page.title || '',
        markdown: page.content || '',
        sheet: typeof page.scope?.sheet === 'string' ? page.scope.sheet : undefined,
      },
    ]);
  }, [page]);

  const items = useMemo<ColumnItem[]>(
    () =>
      (matrix?.rows || []).map((row) => {
        const dates = Object.keys(row.values);
        return {
          key: row.label,
          label: row.label,
          sub: t('domainKnowledge.wikiColumns.dateCount', { count: dates.length }),
          desc: dates.length ? row.values[dates[0]] : '',
        };
      }),
    [matrix, t],
  );

  useColumnItems(items, colKey, onItemsChange, onChanged);

  return (
    <ColumnShell
      kicker={t('domainKnowledge.wikiColumns.tableRows')}
      title={title}
      count={items.length}
    >
      {!page || !matrix ? (
        <ColumnLoading />
      ) : items.length === 0 ? (
        <ColumnEmpty text={t('domainKnowledge.wikiColumns.emptyRows')} />
      ) : (
        items.map((it) => (
          <NodeCard
            key={it.key}
            selected={selected === it.key}
            grayed={grayedKeys.has(it.key)}
            onClick={() => {
              setSelected(it.key);
              onDrill({ kind: 'cells', path, rowLabel: it.key }, it.label);
            }}
            item={it}
          />
        ))
      )}
    </ColumnShell>
  );
}

// ── ⑤ 行 → 各日期取值 ─────────────────────────────────────

function CellsColumn({
  path,
  rowLabel,
  title,
  docs,
  loadDocs,
  colKey,
  grayedKeys,
  onItemsChange,
  onChanged,
}: ColumnCommonProps & {
  path: string;
  rowLabel: string;
  title: string;
  docs: Record<string, WikiPageContent>;
  loadDocs: (paths: string[]) => Promise<void>;
}) {
  const { t } = useTranslation();
  const page = docs[path];

  useEffect(() => {
    void loadDocs([path]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  const items = useMemo<ColumnItem[]>(() => {
    if (!page) return [];
    const matrix = buildTimelineMatrix([
      {
        path: page.enginePath || page.path,
        title: page.title || '',
        markdown: page.content || '',
        sheet: typeof page.scope?.sheet === 'string' ? page.scope.sheet : undefined,
      },
    ]);
    const row = matrix.rows.find((r) => r.label === rowLabel);
    if (!row) return [];
    return Object.entries(row.values).map(([date, value]) => ({
      key: `${rowLabel}:${date}`,
      label: date,
      desc: value,
    }));
  }, [page, rowLabel]);

  useColumnItems(items, colKey, onItemsChange, onChanged);

  return (
    <ColumnShell
      kicker={t('domainKnowledge.wikiColumns.values')}
      title={title}
      count={items.length}
    >
      {!page ? (
        <ColumnLoading />
      ) : items.length === 0 ? (
        <ColumnEmpty text={t('domainKnowledge.wikiColumns.emptyValues')} />
      ) : (
        items.map((it) => <NodeCard key={it.key} grayed={grayedKeys.has(it.key)} item={it} onClick={() => undefined} />)
      )}
    </ColumnShell>
  );
}

// ── ⑥ 源文档 → 它支撑的断言 ───────────────────────────────

function ClaimsColumn({
  kbId,
  documentId,
  title,
  onOpenPage,
  colKey,
  onItemsChange,
  onChanged,
}: ColumnCommonProps & {
  documentId: string;
  title: string;
  onOpenPage?: (path: string) => void;
}) {
  const { t } = useTranslation();
  const [claims, setClaims] = useState<WikiClaim[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [groupByPage, setGroupByPage] = useState<'page' | 'claim'>('page');

  useEffect(() => {
    let alive = true;
    setLoading(true);
    getWikiClaims(kbId, { documentId, limit: CLAIM_LIMIT })
      .then((res) => {
        if (!alive) return;
        setClaims(res.items);
        setError('');
      })
      .catch((err) => {
        if (alive) setError((err as Error)?.message || '');
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [kbId, documentId]);

  // 一个源文档通常支撑几十条断言、但只落在几页上：默认按页面归组，读者先看「影响了哪些页」
  const items = useMemo<ColumnItem[]>(() => {
    if (groupByPage) {
      const byPage = new Map<string, { count: number; sample: string }>();
      for (const claim of claims) {
        const entry = byPage.get(claim.pagePath) || { count: 0, sample: claim.text };
        entry.count += 1;
        byPage.set(claim.pagePath, entry);
      }
      return [...byPage.entries()].map(([pagePath, info]) => ({
        key: pagePath,
        label: claims.find((c) => c.pagePath === pagePath)?.pageTitle || pagePath,
        sub: t('domainKnowledge.wikiReader.claimCount', { count: info.count }),
        desc: info.sample,
      }));
    }
    return claims.map((claim) => ({
      key: claim.claimId,
      label: (claim.text || '').slice(0, 40),
      sub: claim.pageTitle || claim.pagePath,
      desc: claim.excerpt,
    }));
  }, [claims, groupByPage, t]);

  useColumnItems(items, colKey, onItemsChange, onChanged, groupByPage);

  return (
    <ColumnShell
      kicker={t('domainKnowledge.wikiColumns.claims')}
      title={title}
      count={items.length}
      headerExtra={
        <Segmented
          size="small"
          value={groupByPage ? 'page' : 'claim'}
          onChange={(value) => setGroupByPage(value as 'page' | 'claim')}
          options={[
            { label: t('domainKnowledge.wikiColumns.byPage'), value: 'page' },
            { label: t('domainKnowledge.wikiColumns.byClaim'), value: 'claim' },
          ]}
        />
      }
    >
      {loading ? (
        <ColumnLoading />
      ) : error ? (
        <ColumnError message={error} onRetry={() => setError('')} />
      ) : items.length === 0 ? (
        <ColumnEmpty text={t('domainKnowledge.wikiColumns.emptyClaims')} />
      ) : (
        items.map((it) => (
          <NodeCard
            key={it.key}
            item={it}
            onClick={groupByPage ? () => onOpenPage?.(it.key) : () => undefined}
            onOpen={groupByPage ? () => onOpenPage?.(it.key) : undefined}
            openHint={t('domainKnowledge.wikiColumns.openHint')}
          />
        ))
      )}
    </ColumnShell>
  );
}
