import React, { useEffect, useRef, useState, useCallback } from 'react';
import { Button, Tag, Space, Spin, Empty, Input, message, Select, Tooltip } from 'antd';
import {
  SearchOutlined,
  ReloadOutlined,
  ZoomInOutlined,
  ZoomOutOutlined,
  ExpandAltOutlined,
  FilterOutlined,
  NodeExpandOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { getOntologyGraph, getOntologyStatistics, expandOntologyNeighbors } from '@/api/domainKnowledge';
import type { OntologyGraphNode, OntologyGraphEdge, OntologyGraphData } from '@/types/domainKnowledge';

// 自动为 entity_type 分配颜色
const TYPE_COLORS: Record<string, string> = {};
// 统一 S≈45% / L≈54%，只换色相。预设那组的毛病是明度不齐（#f59e0b 过亮、#8b5cf6 过暗、
// #ef4444 刺眼），明度拉平后就不再有哪个色抢眼。
// 注意别再往下压饱和度：画布上节点是小圆点且密集，靠色相区分类型，
// 饱和度压到 30% 时 12 个色相会挤成同一片灰蓝。（页头那种大色块静态标签可以更低。）
// 顺序按色相跳跃排布：类型按首次出现顺序取色，相邻色相排一起会让相邻类型几乎同色
const COLOR_PALETTE = [
  '#559bbe', // 湖蓝
  '#be7855', // 陶土
  '#55be9b', // 青绿
  '#9255be', // 雾紫
  '#be9b55', // 沙
  '#555ebe', // 雾靛
  '#be558a', // 藕荷
  '#9bbe55', // 橄榄
  '#5581be', // 雾蓝
  '#be55be', // 洋红
  '#be5e55', // 陶红
  '#6f55be', // 蓝紫
];

// 搜索命中节点的高亮色（不在 entity_type 调色板内，大红色高亮，保证视觉上唯一可区分）
const SEARCH_HIGHLIGHT_FILL = '#FF0000'; // 大红色填充
const SEARCH_HIGHLIGHT_STROKE = '#b91c1c'; // 深红描边

// 画布上下留给浮层的高度：G6 容器从这两条线之间开始，
// autoFit 出来的节点才不会跑到工具条、状态栏底下被压住
const CANVAS_TOOLBAR_OFFSET = 60;
const CANVAS_STATUS_OFFSET = 48;

// 底部提示是纯文字，画布渐变底 + 密集节点会让它糊掉，所以垫一层白底
const CANVAS_OVERLAY_STYLE: React.CSSProperties = {
  pointerEvents: 'auto',
  padding: '6px 8px',
  borderRadius: 8,
  background: 'rgba(255,255,255,0.9)',
  border: '1px solid #eef2f6',
  boxShadow: '0 2px 8px rgba(15,23,42,0.06)',
  backdropFilter: 'blur(4px)',
};

// 顶部三块并排一行，必须等高，否则看起来是三级台阶。
// 它们的内容本身就参差：Select 27px、Tag 24px、Button 28px（各自的行高与边框不同）。
// 定值取内容最大值 28 + padding 6×2 = 40。
// 用 minHeight 而非 height：将来某块内容真超过 40 时它自己长高，好过被裁掉。
const CANVAS_PILL_MIN_HEIGHT = 40;

// 顶部三块不垫底：里面的 Input / Select / Button 各自带着白底与边框，
// 外面再套一层就是白底叠白底，画布上平白多出三块色块。
// 只有数量组是纯文字、自己没有底——它落在右上角那片空白上，靠布局位置兜住可读性。
const CANVAS_TOP_PILL_STYLE: React.CSSProperties = {
  pointerEvents: 'auto',
  padding: '6px 8px',
  minHeight: CANVAS_PILL_MIN_HEIGHT,
};

let _colorIdx = 0;
// 本体抽取没识别出类型的实体（兜底分类 unknown）在真实数据里占比过半，
// 让它占一个彩色色位的话，整张图会被染成同一种颜色、其余类型全被压掉。
// 给中性灰：让「未识别」退成背景，彩色才重新成为信息。
// 大小写与空值都归到这里，不消耗彩色色位
const UNKNOWN_TYPE_COLOR = '#a8b6c4';

/** 本体抽取没识别出类型的兜底分类，大小写与空值一并归入 */
const isUnknownType = (type: string) => !type || type.toLowerCase() === 'unknown';

function getTypeColor(type: string): string {
  if (isUnknownType(type)) return UNKNOWN_TYPE_COLOR;
  if (!TYPE_COLORS[type]) {
    TYPE_COLORS[type] = COLOR_PALETTE[_colorIdx % COLOR_PALETTE.length];
    _colorIdx++;
  }
  return TYPE_COLORS[type];
}

// 大图性能阈值
const NODE_LABEL_LIMIT = 500;
const EDGE_LABEL_LIMIT = 1000;
// 标签密度开关：节点多到这个数以上，就不再给每个节点打标签，只留枢纽。
// 500 个标签必然糊成一片；而顺着高连接度的节点往下读，本来就是看图的方式。
// 阈值按度数实测定的：500 节点里度数 ≥5 的约 140 个，铺开正好读得清
const LABEL_CROWD_THRESHOLD = 300;
const LABEL_MIN_DEGREE = 5;
const LARGE_GRAPH_THRESHOLD = 200;
const WEBGL_RENDER_THRESHOLD = 200;
const NODE_LIMIT = 500;

// degree 只服务布局：孤立点要单独用弱斥力，见 renderGraph 里的 manyBody
function mapNode(n: OntologyGraphNode, showLabel = true, degree = 0) {
  const label = n.name.length > 12 ? n.name.slice(0, 12) + '…' : n.name;
  const style: Record<string, any> = {
    fill: getTypeColor(n.type),
    size: 30 + Math.min((n.doc_ids?.length || 0) * 3, 30),
    lineWidth: 2,
    stroke: getTypeColor(n.type),
  };
  // unknown 节点不给标签：它们占实体六成以上、名字多是噪音，
  // 一起铺在图上会把真正有类型的节点标签压得读不出来
  if (showLabel && !isUnknownType(n.type)) {
    style.labelText = label;
    style.labelFill = '#475569';
    style.labelFontSize = 11;
    style.labelOffsetY = 8;
    // 文字描白边（halo）：标签压在节点、边或别的标签上时仍能读出来
    style.labelStroke = '#ffffff';
    style.labelLineWidth = 3;
  }
  return {
    id: n.id,
    data: {
      label,
      fullLabel: n.name,
      type: n.type,
      description: n.description || '',
      aliases: n.aliases || [],
      attributes: n.attributes || {},
      confidence: n.confidence ?? 1,
      docIds: n.doc_ids || [],
      degree,
    },
    style,
  };
}

function mapEdge(e: OntologyGraphEdge, showLabel = true) {
  const style: Record<string, any> = {
    stroke: '#cbd5e1',
    lineWidth: 1,
    endArrow: true,
  };
  if (showLabel) {
    style.labelText = e.label;
    style.labelFill = '#94a3b8';
    style.labelFontSize = 9;
  }
  return {
    id: edgeKey(e),
    source: `${e.source_type}:${e.source}`,
    target: `${e.target_type}:${e.target}`,
    data: {
      label: e.label,
      confidence: e.confidence ?? 1,
    },
    style,
  };
}

function edgeKey(e: OntologyGraphEdge): string {
  return `${e.source_type}:${e.source}__${e.label}__${e.target_type}:${e.target}`;
}

function buildGraphData(raw: { nodes: OntologyGraphNode[]; edges: OntologyGraphEdge[] }) {
  const showNodeLabels = raw.nodes.length <= NODE_LABEL_LIMIT;
  const showEdgeLabels = raw.edges.length <= EDGE_LABEL_LIMIT;

  // 节点连接度：拥挤时只有枢纽配得上标签
  const degree = new Map<string, number>();
  for (const e of raw.edges) {
    const s = `${e.source_type}:${e.source}`;
    const t = `${e.target_type}:${e.target}`;
    degree.set(s, (degree.get(s) || 0) + 1);
    degree.set(t, (degree.get(t) || 0) + 1);
  }
  const minDegree = raw.nodes.length > LABEL_CROWD_THRESHOLD ? LABEL_MIN_DEGREE : 0;

  const seenNodes = new Set<string>();
  const nodes = raw.nodes
    .filter((n) => {
      if (seenNodes.has(n.id)) return false;
      seenNodes.add(n.id);
      return true;
    })
    .map((n) => {
      const deg = degree.get(n.id) || 0;
      return mapNode(n, showNodeLabels && deg >= minDegree, deg);
    });

  const seenEdges = new Set<string>();
  const edges = raw.edges
    .filter((e) => {
      const k = edgeKey(e);
      if (seenEdges.has(k)) return false;
      seenEdges.add(k);
      return true;
    })
    .map((e) => mapEdge(e, showEdgeLabels));

  return { nodes, edges };
}

interface KnowledgeGraphPanelProps {
  kbId: string;
  /** 按来源文档过滤图谱节点，不传则返回 KB 级全库图谱 */
  docId?: string;
  /** 去掉自带的卡面（白底 / 边框 / 圆角 / 阴影）。嵌在已有卡片的容器里时，
   *  这层壳会与父卡片叠成「卡中卡」；独立图谱页没有父卡片，靠它成卡片，不传 */
  bare?: boolean;
}

export default function KnowledgeGraphPanel({ kbId, docId, bare }: KnowledgeGraphPanelProps) {
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<any>(null);
  const currentDataRef = useRef<OntologyGraphData | null>(null);
  /** 当前被放大显示的搜索节点 id（搜索新实体时先恢复旧节点图标） */
  const enlargedNodeIdRef = useRef<string | null>(null);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<any>(null);
  const [typeDistribution, setTypeDistribution] = useState<{ type: string; count: number }[]>([]);
  const [searchKeyword, setSearchKeyword] = useState('');
  const [selectedTypes, setSelectedTypes] = useState<string[]>([]);
  const [meta, setMeta] = useState<{
    total_nodes: number;
    total_relations: number;
    returned_nodes: number;
    returned_edges: number;
    truncated: boolean;
  } | null>(null);
  const [expanding, setExpanding] = useState(false);
  const [degraded, setDegraded] = useState(false);
  const [degradedReason, setDegradedReason] = useState('');

  // 渲染力导向图
  const renderGraph = useCallback(async (raw: OntologyGraphData) => {
    if (!containerRef.current) return;

    if (graphRef.current) {
      graphRef.current.destroy();
      graphRef.current = null;
    }

    const gData = buildGraphData(raw);

    const isLarge = gData.nodes.length > LARGE_GRAPH_THRESHOLD;
    const layout = isLarge
      ? {
          type: 'd3-force',
          linkDistance: 80,
          manyBody: {
            // 孤立点（度数 0）只给弱斥力：它们之间互推纯粹在撑大包围盒，
            // autoFit 之后整个主体被迫缩小，反而看不清
            strength: (d: { data?: { degree?: number } }) => ((d?.data?.degree ?? 0) > 0 ? -80 : -10),
          },
          // 大图分支同样要有碰撞力：节点 size 在 30–60，半径按各自 size 算，
          // 固定半径的话小节点会留出过多空隙、大节点仍会叠
          collide: { radius: (d: { style?: { size?: number } }) => (d?.style?.size ?? 30) / 2 + 6, strength: 1 },
          animate: false,
        }
      : {
          type: 'd3-force',
          linkDistance: 120,
          collide: { radius: 40 },
          manyBody: { strength: -400 },
          animate: false,
        };

    try {
      const { Graph: G6Graph } = await import('@antv/g6');

      let renderer: (() => any) | undefined;
      if (gData.nodes.length > WEBGL_RENDER_THRESHOLD) {
        try {
          const webgl: any = await import('@antv/g-webgl');
          const WebGLRenderer = webgl.Renderer;
          renderer = () => new WebGLRenderer();
        } catch {
          // 回退默认 Canvas
        }
      }

      const graph = new G6Graph({
        container: containerRef.current,
        width: containerRef.current.clientWidth,
        height: containerRef.current.clientHeight,
        data: gData,
        ...(renderer ? { renderer } : {}),
        layout,
        // 不设图级 node.type：否则会覆盖数据里的 type，搜索命中的五角星节点无法生效
        node: {
          style: { cursor: 'pointer' },
        },
        edge: {
          // 曲线而非直线：密集区的直线会织成网格，且双向关系（A→B 与 B→A）完全重叠
          type: 'quadratic',
          style: { cursor: 'default', endArrow: true },
        },
        behaviors: ['zoom-canvas', 'drag-canvas', 'drag-element'],
        autoFit: 'view',
        animation: false,
      });

      graph.on('node:click', (evt: any) => {
        const nodeId = evt?.target?.id;
        if (!nodeId) return;
        const data = currentDataRef.current;
        if (!data) return;
        const nodeData = data.nodes.find((n) => n.id === nodeId);
        if (!nodeData) return;

        const connectedEdges = data.edges.filter(
          (e) => `${e.source_type}:${e.source}` === nodeId || `${e.target_type}:${e.target}` === nodeId,
        );

        setSelectedNode({
          ...nodeData,
          connectedEdges,
          typeColor: getTypeColor(nodeData.type),
        });
      });

      graph.on('node:dblclick', (evt: any) => {
        const nodeId = evt?.target?.id;
        if (!nodeId) return;
        const nodeData = currentDataRef.current?.nodes.find((n) => n.id === nodeId);
        if (nodeData) void expandNode(nodeData);
      });

      graph.on('canvas:click', () => setSelectedNode(null));

      graphRef.current = graph;
      await graph.render();
    } catch (e: any) {
      setError(t('knowledgeGraph.graphRenderFailed', { message: e?.message || e }));
    }
  }, []);

  // 展开邻居
  const expandNode = useCallback(
    async (node: OntologyGraphNode) => {
      if (!kbId || !graphRef.current) return;
      setExpanding(true);
      try {
        const res = await expandOntologyNeighbors(kbId, node.type, node.name);
        if (res.degraded) {
          message.warning(res.degraded_reason || t('knowledgeGraph.dbUnavailable'));
          return;
        }
        const cur = currentDataRef.current;
        if (!cur) return;
        const existIds = new Set(cur.nodes.map((n) => n.id));
        const newNodes = res.nodes.filter((n) => {
          if (existIds.has(n.id)) return false;
          existIds.add(n.id);
          return true;
        });
        const existEdgeKeys = new Set(cur.edges.map(edgeKey));
        const newEdges = res.edges.filter((e) => {
          const k = edgeKey(e);
          if (existEdgeKeys.has(k)) return false;
          existEdgeKeys.add(k);
          return true;
        });
        if (newNodes.length === 0 && newEdges.length === 0) {
          message.info(t('knowledgeGraph.noMoreNeighbors'));
          return;
        }
        cur.nodes.push(...newNodes);
        cur.edges.push(...newEdges);
        const showNodeLabels = cur.nodes.length <= NODE_LABEL_LIMIT;
        const showEdgeLabels = cur.edges.length <= EDGE_LABEL_LIMIT;
        if (newNodes.length) graphRef.current.addNodeData(newNodes.map((n) => mapNode(n, showNodeLabels)));
        if (newEdges.length) graphRef.current.addEdgeData(newEdges.map((e) => mapEdge(e, showEdgeLabels)));
        await graphRef.current.render();
        setMeta((m) => (m ? { ...m, returned_nodes: cur.nodes.length, returned_edges: cur.edges.length } : m));
        message.success(t('knowledgeGraph.expandCount', { newNodes: newNodes.length, newEdges: newEdges.length }));
      } catch (e: any) {
        message.error(e?.message || t('knowledgeGraph.expandFailed'));
      } finally {
        setExpanding(false);
      }
    },
    [kbId],
  );

  // 加载数据
  const loadData = useCallback(async () => {
    if (!kbId) return;
    setLoading(true);
    setError(null);
    try {
      const [graphData, statsData] = await Promise.all([
        getOntologyGraph(kbId, {
          limit: NODE_LIMIT,
          entityTypes: selectedTypes.length ? selectedTypes : undefined,
          documentId: docId || undefined,
        }),
        getOntologyStatistics(kbId),
      ]);
      currentDataRef.current = graphData;
      setDegraded(!!graphData.degraded);
      setDegradedReason(graphData.degraded_reason || '');
      setMeta({
        total_nodes: graphData.total_nodes,
        total_relations: graphData.total_relations,
        returned_nodes: graphData.returned_nodes,
        returned_edges: graphData.returned_edges,
        truncated: graphData.truncated,
      });

      const dist = Object.entries(graphData.type_counts || {})
        .map(([type, count]) => ({ type, count }))
        .sort((a, b) => b.count - a.count);
      setTypeDistribution(dist);

      await renderGraph(graphData);
    } catch (e: any) {
      setError(e?.message || t('knowledgeGraph.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [kbId, docId, selectedTypes, renderGraph]);

  // 初始化和清理
  useEffect(() => {
    loadData();
    return () => {
      if (graphRef.current) {
        graphRef.current.destroy();
        graphRef.current = null;
      }
    };
  }, [loadData]);

  // 缩放
  const handleZoomIn = () => graphRef.current?.zoomTo(graphRef.current.getZoom() * 1.3);
  const handleZoomOut = () => graphRef.current?.zoomTo(graphRef.current.getZoom() * 0.7);
  const handleFitView = () => graphRef.current?.fitView();

  // 搜索节点：命中后图标放大 + 居中放大显示
  const handleSearch = async () => {
    if (!searchKeyword.trim() || !currentDataRef.current) return;
    const kw = searchKeyword.toLowerCase();
    const found = currentDataRef.current.nodes.find(
      (n) => n.name.toLowerCase().includes(kw) || n.id.toLowerCase().includes(kw),
    );
    const graph = graphRef.current;
    if (!found || !graph) {
      message.info(t('knowledgeGraph.nodeNotFound'));
      return;
    }
    // 先恢复上一个被放大显示的节点图标（回到 mapNode 默认尺寸与类型色）
    if (enlargedNodeIdRef.current && enlargedNodeIdRef.current !== found.id) {
      const prevNode = currentDataRef.current.nodes.find((n) => n.id === enlargedNodeIdRef.current);
      if (prevNode) {
        graph.updateNodeData([
          {
            id: prevNode.id,
            type: 'circle',
            style: {
              size: 30 + Math.min((prevNode.doc_ids?.length || 0) * 3, 30),
              lineWidth: 2,
              fill: getTypeColor(prevNode.type),
              stroke: getTypeColor(prevNode.type),
            },
          },
        ]);
      }
    }
    // 搜索到的实体图标放大为五角星形状，并赋予唯一高亮色
    graph.updateNodeData([
      {
        id: found.id,
        type: 'star',
        style: {
          size: 90,
          lineWidth: 3,
          fill: SEARCH_HIGHLIGHT_FILL,
          stroke: SEARCH_HIGHLIGHT_STROKE,
        },
      },
    ]);
    await graph.draw();
    // 居中放大显示：先聚焦到节点（居中），再以视口中心放大
    await graph.focusElement(found.id, { duration: 500 });
    await graph.zoomTo(graph.getZoom() * 2.5, { duration: 500 });
    enlargedNodeIdRef.current = found.id;
  };

  return (
    <div
      style={{
        flex: 1,
        // 内边距与外层卡面解耦：bare 时卡面交给父卡片，但内容仍需要离边框的呼吸空间
        padding: 20,
        display: 'flex',
        flexDirection: 'column',
        overflowY: 'scroll',
        ...(bare
          ? {}
          : {
              background: '#fff',
              borderRadius: 10,
              border: '1px solid #eef2f6',
              boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
            }),
      }}
    >
      {/* G6 画布 */}
      <div
        style={{
          flex: 1,
          borderRadius: 10,
          border: '1px solid #eef2f6',
          background: 'radial-gradient(ellipse at center, #f8fafc 0%, #f0f4f8 100%)',
          overflow: 'hidden',
          position: 'relative',
          // 加上上下浮层占的那两条，图谱可视区不小于原来的 480
          minHeight: 480 + CANVAS_TOOLBAR_OFFSET + CANVAS_STATUS_OFFSET,
        }}
      >
        {/* 工具条与状态栏浮在画布上：原先两者各占画布上下各一行，画布因此矮两截。
            外层不接事件，空白处仍能拖动/缩放画布；只有控件本身吃掉指针 */}
        <div
          style={{
            position: 'absolute',
            top: 12,
            left: 12,
            right: 12,
            display: 'flex',
            alignItems: 'flex-start',
            justifyContent: 'space-between',
            flexWrap: 'wrap',
            gap: 8,
            zIndex: 5,
            pointerEvents: 'none',
          }}
        >
          <Space wrap style={CANVAS_TOP_PILL_STYLE}>
            <Input.Search
              placeholder={t('knowledgeGraph.searchPlaceholder')}
              size="small"
              style={{ width: 180 }}
              value={searchKeyword}
              onChange={(e) => setSearchKeyword(e.target.value)}
              onSearch={handleSearch}
              enterButton={<SearchOutlined />}
            />
            <Select
              mode="multiple"
              allowClear
              size="small"
              placeholder={t('knowledgeGraph.filterByType')}
              style={{ minWidth: 200, maxWidth: 320 }}
              value={selectedTypes}
              onChange={(vals) => setSelectedTypes(vals)}
              maxTagCount="responsive"
              suffixIcon={<FilterOutlined />}
              options={typeDistribution.map((t) => ({
                label: `${t.type} (${t.count})`,
                value: t.type,
              }))}
            />
          </Space>
          {/* 数量与操作按钮同处右上：先读「画了什么」，再决定「怎么操作」，
              两者是同一段视线里的先因后果，拆到画布两端就断了 */}
          <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8, justifyContent: 'flex-end' }}>
            {meta && (
              <Space size={12} wrap style={{ ...CANVAS_TOP_PILL_STYLE, fontSize: 12, color: '#94a3b8' }}>
                <span>
                  {t('knowledgeGraph.showingEntities', { returned: meta.returned_nodes, total: meta.total_nodes })}
                </span>
                <span>
                  {t('knowledgeGraph.showingRelations', { returned: meta.returned_edges, total: meta.total_relations })}
                </span>
                {meta.truncated && (
                  <Tooltip title={t('knowledgeGraph.truncatedTooltip', { limit: NODE_LIMIT })}>
                    <Tag color="orange" style={{ margin: 0, cursor: 'help' }}>
                      {t('knowledgeGraph.truncatedDisplay')}
                    </Tag>
                  </Tooltip>
                )}
                {expanding && <Spin size="small" />}
              </Space>
            )}
            <Space style={CANVAS_TOP_PILL_STYLE}>
              <Button size="small" icon={<ZoomInOutlined />} style={{ borderRadius: 8 }} onClick={handleZoomIn} />
              <Button size="small" icon={<ZoomOutOutlined />} style={{ borderRadius: 8 }} onClick={handleZoomOut} />
              <Button size="small" icon={<ExpandAltOutlined />} style={{ borderRadius: 8 }} onClick={handleFitView} />
              <Button size="small" icon={<ReloadOutlined />} style={{ borderRadius: 8 }} onClick={loadData} />
            </Space>
          </div>
        </div>

        <div
          ref={containerRef}
          style={{
            position: 'absolute',
            top: CANVAS_TOOLBAR_OFFSET,
            left: 0,
            right: 0,
            bottom: CANVAS_STATUS_OFFSET,
          }}
        />

        {/* 操作提示留在画布底部：扣掉高度是它唯一的作用，浮在画布下沿比另占一行更贴题 */}
        {meta && (
          <div
            style={{
              position: 'absolute',
              bottom: 12,
              left: 12,
              right: 12,
              display: 'flex',
              alignItems: 'center',
              // 数量搬去右上后这条只剩一项，靠右与上面的按钮列对齐成一条竖线
              justifyContent: 'flex-end',
              flexWrap: 'wrap',
              gap: 8,
              zIndex: 5,
              pointerEvents: 'none',
            }}
          >
            <Space style={{ ...CANVAS_OVERLAY_STYLE, fontSize: 12, color: '#94a3b8' }}>
              <NodeExpandOutlined />
              <span>{t('knowledgeGraph.dblClickHint')}</span>
            </Space>
          </div>
        )}
        {loading && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              background: 'rgba(255,255,255,0.6)',
              zIndex: 10,
            }}
          >
            <Spin
              description={<div style={{ width: 200, textAlign: 'center' }}>{t('knowledgeGraph.loadingGraph')}</div>}
            >
              <div style={{ padding: 16 }} />
            </Spin>
          </div>
        )}
        {error && !loading && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              zIndex: 10,
            }}
          >
            <Empty description={error}>
              <Button type="primary" onClick={loadData}>
                {t('common.retry')}
              </Button>
            </Empty>
          </div>
        )}
        {!loading && !error && degraded && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              zIndex: 10,
            }}
          >
            <Empty description={degradedReason || t('knowledgeGraph.degradedReason')}>
              <Button type="primary" onClick={loadData}>
                {t('common.retry')}
              </Button>
            </Empty>
          </div>
        )}
        {!loading && !error && !degraded && currentDataRef.current?.nodes.length === 0 && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              zIndex: 10,
            }}
          >
            <Empty description={t('knowledgeGraph.noGraphData')} />
          </div>
        )}
      </div>

      {/* 底部详情面板 */}
      {selectedNode && (
        <div
          style={{
            marginTop: 12,
            background: '#fff',
            borderRadius: 10,
            border: '1px solid #eef2f6',
            boxShadow: '0 1px 4px rgba(0,0,0,0.04)',
            padding: 20,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
            <div
              style={{
                width: 36,
                height: 36,
                borderRadius: 8,
                background: selectedNode.typeColor || '#8b5cf6',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: 16,
                color: '#fff',
              }}
            >
              {selectedNode.name?.charAt(0) || '?'}
            </div>
            <span style={{ fontSize: 15, fontWeight: 600, color: '#0b2b5c' }}>{selectedNode.name}</span>
            <Tag color={selectedNode.typeColor} style={{ margin: 0 }}>
              {selectedNode.type}
            </Tag>
            <Button
              size="small"
              type="primary"
              ghost
              icon={<NodeExpandOutlined />}
              loading={expanding}
              style={{ marginLeft: 'auto' }}
              onClick={() => expandNode(selectedNode)}
            >
              {t('knowledgeGraph.expandNeighbors')}
            </Button>
            <Button size="small" type="text" onClick={() => setSelectedNode(null)}>
              ✕
            </Button>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8 }}>
            <div>
              <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('domainKnowledge.entityType')}</span>
              <div style={{ fontSize: 13, color: '#475569', fontWeight: 500 }}>{selectedNode.type}</div>
            </div>
            <div>
              <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('domainKnowledge.alias')}</span>
              <div style={{ fontSize: 13, color: '#475569', fontWeight: 500 }}>
                {(() => {
                  const aliases = Array.isArray(selectedNode.aliases)
                    ? selectedNode.aliases
                    : typeof selectedNode.aliases === 'string'
                      ? [selectedNode.aliases]
                      : [];
                  return aliases.length > 0 ? aliases.join(', ') : '-';
                })()}
              </div>
            </div>
            <div>
              <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('knowledgeGraph.relationCount')}</span>
              <div style={{ fontSize: 13, color: '#475569', fontWeight: 500 }}>
                {selectedNode.connectedEdges?.length || 0}
              </div>
            </div>
            <div>
              <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('compile.confidence')}</span>
              <div style={{ fontSize: 13, color: '#475569', fontWeight: 500 }}>
                {selectedNode.confidence != null ? `${(selectedNode.confidence * 100).toFixed(0)}%` : '-'}
              </div>
            </div>
            <div>
              <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('domainKnowledge.attributeCount')}</span>
              <div style={{ fontSize: 13, color: '#475569', fontWeight: 500 }}>
                {selectedNode.attributes ? Object.keys(selectedNode.attributes).length : 0}
              </div>
            </div>
            <div>
              <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('domainKnowledge.sourceDocuments')}</span>
              <div style={{ fontSize: 13, color: '#475569', fontWeight: 500 }}>
                {(selectedNode.doc_ids || []).length}
              </div>
            </div>
          </div>
          {selectedNode.description && (
            <div style={{ marginTop: 8 }}>
              <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('domainKnowledge.description')}</span>
              <div style={{ fontSize: 12, color: '#64748b', marginTop: 2 }}>{selectedNode.description}</div>
            </div>
          )}
          {selectedNode.connectedEdges?.length > 0 && (
            <div style={{ marginTop: 8 }}>
              <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('domainKnowledge.relatedRelations')}</span>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 4 }}>
                {selectedNode.connectedEdges.map((e: any, i: number) => (
                  <Tag key={i} style={{ fontSize: 11 }}>
                    {`${e.source_type}:${e.source}` === selectedNode.id ? '→' : '←'} {e.label}{' '}
                    {`${e.source_type}:${e.source}` === selectedNode.id ? e.target : e.source}
                  </Tag>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
