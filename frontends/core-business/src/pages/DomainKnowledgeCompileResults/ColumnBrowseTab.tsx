import { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, message } from 'antd';
import type {
  OntologyInstanceSummary,
  OntologyInstanceRow,
  OntologyGraphEdge,
  OntologyGraphNode,
} from '@/types/domainKnowledge';
import {
  getOntologyEntityTypes,
  getOntologyInstances,
  expandOntologyNeighbors,
} from '@/api/domainKnowledge';
// [jonex] 列式浏览的视觉与交互基础件：与 Wiki 列式图谱共用一份实现（见 columnBoard/）
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
  type ColumnItem as ColumnItemBase,
} from './columnBoard/parts';

const PAGE_SIZE = 100;
const NEIGHBOR_LIMIT = 100;
const MAX_NEIGHBOR_LEVELS = 10; // 最大邻居下钻层数（类型列/实例列不计入）

// ── 下钻路径描述（列数随下钻动态增长，不设上限）────────────
type ColumnPayload =
  | { kind: 'types' }
  | { kind: 'instances'; entityType: string }
  | { kind: 'neighbors'; entityType: string; name: string };

type ColumnDesc = ColumnPayload & { key: string };

// ── 列内卡片：展示字段见 columnBoard 的 ColumnItem，本体链路额外带下钻标识 ──
interface ColumnItem extends ColumnItemBase {
  /** 继续下钻用的实体类型 + 显示名 */
  entityType: string;
  name: string;
}

interface ColumnBrowseTabProps {
  kbId: string;
  docId?: string;
  /** 容器高度；两级宿主都已定高，跟着容器走即可（照视口算 calc(100vh - N) 在 hosted 下会偏大） */
  height?: string;
  /** 实体类型聚合，用于把实体的 type 映射成 display_name（与类型列同源） */
  entityTypes?: OntologyInstanceSummary[] | null;
}

export default function ColumnBrowseTab({
  kbId,
  docId,
  height = '100%',
  entityTypes,
}: ColumnBrowseTabProps) {
  const { t } = useTranslation();
  const seqRef = useRef(1);
  const [columns, setColumns] = useState<ColumnDesc[]>(() => [{ key: 'col-0', kind: 'types' }]);
  // 每列（按 col key）出现过的实例/节点 type:name 集合，由列组件加载后上报
  const [colItemsMap, setColItemsMap] = useState<Record<string, Set<string>>>({});

  // 列间连线 + 舞台（横向滚动 / 网格底 / 滚动缩放重画 / 新列自动滚到最右）
  const connectors = useColumnConnectors(columns.length);
  const scheduleConnectors = connectors.scheduleConnectors;

  // 标签统一查实体类型自己的 display_name（与类型列同源），键是实体的 type 字段：
  // 实例列查实例自身的 type，关联节点列查对端实体的 type。
  // 查不到时由调用方回退成原始类型名，标签不会空掉。
  const tagLabels = useMemo<ReadonlyMap<string, string>>(() => {
    const map = new Map<string, string>();
    for (const et of entityTypes ?? []) {
      if (et.name) map.set(et.name, et.display_name || et.name);
    }
    return map;
  }, [entityTypes]);

  // 点击第 index 列某项 → 截断其后所有列，右侧追加新列（重新下钻）
  const handleDrill = useCallback(
    (index: number, payload: ColumnPayload) => {
      if (payload.kind === 'neighbors') {
        // 去环：该节点已作为中心节点展开过，静默拦截（置灰已阻止点击，这里兜底）
        const visited = columns.some(
          (c) =>
            c.kind === 'neighbors' &&
            c.entityType === payload.entityType &&
            c.name === payload.name,
        );
        if (visited) return;
        // 层数上限：邻居列达到 MAX_NEIGHBOR_LEVELS 后提示，不再下钻
        const neighborLevels = columns.filter((c) => c.kind === 'neighbors').length;
        if (neighborLevels >= MAX_NEIGHBOR_LEVELS) {
          message.info(t('compile.columnBrowse.maxLevelReached'));
          return;
        }
      }
      const key = `col-${seqRef.current++}`;
      setColumns((prev) => [...prev.slice(0, index + 1), { ...payload, key } as ColumnDesc]);
    },
    [columns, t],
  );

  // 列组件加载后上报本列出现过的节点（实例列报实例、邻居列报对端实体）
  const handleColItems = useCallback((colKey: string, keys: Set<string>) => {
    setColItemsMap((prev) => ({ ...prev, [colKey]: keys }));
  }, []);

  // 按列顺序累积：第 i 列之前所有列出现过的节点集合（供第 i 列判断「第几次出现」）
  const appearedByCol = useAppearedKeys(
    columns.map((c) => c.key),
    colItemsMap,
  );

  return (
    <ColumnStage connectors={connectors} height={height}>
      {columns.map((col, i) => (
        <ColumnView
          key={col.key}
          desc={col}
          kbId={kbId}
          docId={docId}
          colKey={col.key}
          tagLabels={tagLabels}
          grayedKeys={appearedByCol[i] ?? EMPTY_SET}
          onItemsChange={handleColItems}
          onDrill={(payload) => handleDrill(i, payload)}
          onChanged={scheduleConnectors}
        />
      ))}
    </ColumnStage>
  );
}

// ── 列分发 ────────────────────────────────────────────────

function ColumnView({
  desc,
  kbId,
  docId,
  colKey,
  tagLabels,
  grayedKeys,
  onItemsChange,
  onDrill,
  onChanged,
}: {
  desc: ColumnDesc;
  kbId: string;
  docId?: string;
  colKey: string;
  tagLabels: ReadonlyMap<string, string>;
  grayedKeys: ReadonlySet<string>;
  onItemsChange: (colKey: string, keys: Set<string>) => void;
  onDrill: (payload: ColumnPayload) => void;
  onChanged: () => void;
}) {
  switch (desc.kind) {
    case 'types':
      return <TypeColumn kbId={kbId} docId={docId} onDrill={onDrill} onChanged={onChanged} />;
    case 'instances':
      return (
        <InstancesColumn
          kbId={kbId}
          docId={docId}
          entityType={desc.entityType}
          colKey={colKey}
          tagLabels={tagLabels}
          onItemsChange={onItemsChange}
          onDrill={onDrill}
          onChanged={onChanged}
        />
      );
    case 'neighbors':
      return (
        <NeighborsColumn
          kbId={kbId}
          docId={docId}
          entityType={desc.entityType}
          name={desc.name}
          colKey={colKey}
          tagLabels={tagLabels}
          grayedKeys={grayedKeys}
          onItemsChange={onItemsChange}
          onDrill={onDrill}
          onChanged={onChanged}
        />
      );
    default:
      return null;
  }
}

// ── 第 1 列：实体类型（入口）───────────────────────────────

function TypeColumn({
  kbId,
  docId,
  onDrill,
  onChanged,
}: {
  kbId: string;
  docId?: string;
  onDrill: (payload: ColumnPayload) => void;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const [items, setItems] = useState<OntologyInstanceSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [keyword, setKeyword] = useState('');
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    setError(null);
    getOntologyEntityTypes(kbId, undefined, docId)
      .then((res) => {
        if (mounted) setItems(res.items);
      })
      .catch((err: any) => {
        if (mounted) setError(err?.message || t('common.loadFailed'));
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, [kbId, docId, t, reload]);

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    if (!kw) return items;
    return items.filter(
      (it) => (it.display_name || it.name).toLowerCase().includes(kw) || it.name.toLowerCase().includes(kw),
    );
  }, [items, keyword]);

  // 卡片渲染完成后重画连线
  useEffect(() => {
    onChanged();
  }, [filtered, selected, onChanged]);

  return (
    <ColumnShell
      kicker={t('compile.columnBrowse.ontology')}
      title={t('compile.columnBrowse.entityTypes')}
      count={filtered.length}
      searchable
      search={keyword}
      onSearch={setKeyword}
    >
      {loading ? (
        <ColumnLoading />
      ) : error ? (
        <ColumnError message={error} onRetry={() => setReload((r) => r + 1)} />
      ) : filtered.length === 0 ? (
        <ColumnEmpty text={t('compile.columnBrowse.emptyTypes')} />
      ) : (
        filtered.map((it) => {
          const label = it.display_name || it.name;
          const active = selected === it.name;
          return (
            <NodeCard
              key={it.name}
              selected={active}
              onClick={() => {
                setSelected(it.name);
                onDrill({ kind: 'instances', entityType: it.name });
              }}
              item={{
                key: it.name,
                label,
                sub: t('compile.columnBrowse.instanceCount', { count: it.instance_count }),
                isType: true,
              }}
            />
          );
        })
      )}
    </ColumnShell>
  );
}

// ── 第 2 列：实例（分页加载）───────────────────────────────

function InstancesColumn({
  kbId,
  docId,
  entityType,
  colKey,
  tagLabels,
  onItemsChange,
  onDrill,
  onChanged,
}: {
  kbId: string;
  docId?: string;
  entityType: string;
  colKey: string;
  tagLabels: ReadonlyMap<string, string>;
  onItemsChange: (colKey: string, keys: Set<string>) => void;
  onDrill: (payload: ColumnPayload) => void;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const [items, setItems] = useState<OntologyInstanceRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [keyword, setKeyword] = useState('');
  const pageRef = useRef(1);

  const fetchPage = useCallback(
    async (p: number, append: boolean) => {
      if (append) setLoadingMore(true);
      else setLoading(true);
      setError(null);
      try {
        const res = await getOntologyInstances({
          kbId,
          entityType,
          docId: docId || undefined,
          page: p,
          pageSize: PAGE_SIZE,
        });
        pageRef.current = p;
        setItems((prev) => (append ? [...prev, ...res.items] : res.items));
        setTotal(res.total);
      } catch (err: any) {
        setError(err?.message || t('common.loadFailed'));
      } finally {
        setLoading(false);
        setLoadingMore(false);
      }
    },
    [kbId, entityType, docId, t],
  );

  useEffect(() => {
    setItems([]);
    setTotal(0);
    fetchPage(1, false);
  }, [fetchPage]);

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    if (!kw) return items;
    return items.filter(
      (it) => it.name.toLowerCase().includes(kw) || (it.description || '').toLowerCase().includes(kw),
    );
  }, [items, keyword]);

  // 上报本列出现过的实例（供后续关联节点列判断「第几次出现」）
  useEffect(() => {
    onItemsChange(colKey, new Set(items.map((it) => `${it.type}:${it.name}`)));
  }, [colKey, items, onItemsChange]);

  useEffect(() => {
    onChanged();
  }, [filtered, selected, onChanged]);

  const hasMore = items.length < total;

  return (
    <ColumnShell
      // 列头标签与卡片标签同源：entityType 是内部类型名，直接展示会泄露英文标识
      kicker={tagLabels.get(entityType) ?? entityType}
      title={t('compile.columnBrowse.instances')}
      count={filtered.length}
      searchable
      search={keyword}
      onSearch={setKeyword}
    >
      {loading ? (
        <ColumnLoading />
      ) : error ? (
        <ColumnError message={error} onRetry={() => fetchPage(1, false)} />
      ) : filtered.length === 0 ? (
        <ColumnEmpty text={t('compile.columnBrowse.emptyInstances')} />
      ) : (
        <>
          {filtered.map((it) => {
            const active = selected === it.name;
            return (
              <NodeCard
                key={it.name}
                selected={active}
                onClick={() => {
                  setSelected(it.name);
                  onDrill({ kind: 'neighbors', entityType: it.type, name: it.name });
                }}
                item={{
                  key: `${it.type}:${it.name}`,
                  label: it.name,
                  // 该类型没有作为任何关系的源时查不到，回退成类型名，标签不空
                  tag: tagLabels.get(it.type) ?? it.type,
                  desc: it.description,
                }}
              />
            );
          })}
          {hasMore && (
            <div style={{ padding: '6px 2px 2px' }}>
              <Button size="small" block loading={loadingMore} onClick={() => fetchPage(pageRef.current + 1, true)}>
                {t('compile.columnBrowse.loadMore')}
              </Button>
            </div>
          )}
        </>
      )}
    </ColumnShell>
  );
}

// ── 第 3/4/… 列：关联节点（一跳邻居，继续下钻）──────────────

function NeighborsColumn({
  kbId,
  docId,
  entityType,
  name,
  colKey,
  tagLabels,
  grayedKeys,
  onItemsChange,
  onDrill,
  onChanged,
}: {
  kbId: string;
  docId?: string;
  entityType: string;
  name: string;
  colKey: string;
  tagLabels: ReadonlyMap<string, string>;
  grayedKeys: ReadonlySet<string>;
  onItemsChange: (colKey: string, keys: Set<string>) => void;
  onDrill: (payload: ColumnPayload) => void;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const [edges, setEdges] = useState<OntologyGraphEdge[]>([]);
  // 邻居节点的描述只在 nodes 里（edges 不带 description），单独存一份供卡片显示
  const [nodes, setNodes] = useState<OntologyGraphNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [degraded, setDegraded] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [keyword, setKeyword] = useState('');
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    setError(null);
    setDegraded(false);
    expandOntologyNeighbors(kbId, entityType, name, NEIGHBOR_LIMIT, docId)
      .then((res) => {
        if (!mounted) return;
        setEdges(res.edges || []);
        setNodes(res.nodes || []);
        setDegraded(!!res.degraded);
      })
      .catch((err: any) => {
        if (mounted) setError(err?.message || t('common.loadFailed'));
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, [kbId, docId, entityType, name, t, reload]);

  // 每条边 → 判定方向，定位「对端」实体，映射为一张卡片（按 type:name 去重）
  const rows = useMemo(() => {
    const descByKey = new Map(nodes.map((n) => [`${n.type}:${n.name}`, n.description]));
    const map = new Map<string, ColumnItem>();
    for (const edge of edges) {
      const isOut = edge.source === name && edge.source_type === entityType;
      const isIn = edge.target === name && edge.target_type === entityType;
      if (!isOut && !isIn) continue;
      const otherType = isOut ? edge.target_type : edge.source_type;
      const otherName = isOut ? edge.target : edge.source;
      const via = isOut ? edge.label : `被${edge.label}`;
      const key = `${otherType}:${otherName}`;
      if (map.has(key)) continue;
      map.set(key, {
        key,
        label: otherName,
        // 标签取对端实体自己的 type → display_name（与实例列、类型列同源）
        tag: tagLabels.get(otherType) ?? otherType,
        desc: descByKey.get(key),
        // 右下角徽标记关系名本身（带方向），与标签互补
        relation: via,
        entityType: otherType,
        name: otherName,
      });
    }
    return [...map.values()].sort((a, b) => a.label.localeCompare(b.label, 'zh-CN'));
  }, [edges, nodes, entityType, name, tagLabels]);

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    if (!kw) return rows;
    return rows.filter((it) => it.label.toLowerCase().includes(kw) || (it.tag || '').toLowerCase().includes(kw));
  }, [rows, keyword]);

  // 上报本列出现过的关联节点（供后续列判断「第几次出现」）
  useEffect(() => {
    onItemsChange(colKey, new Set(rows.map((it) => it.key)));
  }, [colKey, rows, onItemsChange]);

  useEffect(() => {
    onChanged();
  }, [filtered, selected, onChanged]);

  return (
    <ColumnShell
      kicker={name}
      title={t('compile.columnBrowse.neighbors')}
      count={filtered.length}
      searchable={filtered.length > 8}
      search={keyword}
      onSearch={setKeyword}
    >
      {loading ? (
        <ColumnLoading />
      ) : error ? (
        <ColumnError message={error} onRetry={() => setReload((r) => r + 1)} />
      ) : degraded ? (
        <ColumnEmpty text={t('compile.columnBrowse.degraded')} />
      ) : filtered.length === 0 ? (
        <ColumnEmpty text={t('compile.columnBrowse.emptyNeighbors')} />
      ) : (
        filtered.map((it) => {
          const active = selected === it.key;
          // 第二次出现（此前某列已出现过同一 type:name）→ 置灰不可点
          const grayed = grayedKeys.has(it.key);
          return (
            <NodeCard
              key={it.key}
              selected={active}
              grayed={grayed}
              disabled={grayed}
              onClick={() => {
                if (grayed) return;
                setSelected(it.key);
                onDrill({ kind: 'neighbors', entityType: it.entityType, name: it.name });
              }}
              item={it}
            />
          );
        })
      )}
    </ColumnShell>
  );
}
