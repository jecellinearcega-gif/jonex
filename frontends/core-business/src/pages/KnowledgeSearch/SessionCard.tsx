import React from 'react';
import { useTranslation } from 'react-i18next';
import { Card, Button, Popconfirm, Radio, Input, Spin, message } from 'antd';
import {
  SearchOutlined,
  StopOutlined,
  ReloadOutlined,
  CloseCircleOutlined,
  CaretDownOutlined,
  CaretRightOutlined,
  NodeIndexOutlined,
  BulbOutlined,
  FileTextOutlined,
  PictureOutlined,
  LikeOutlined,
  LikeFilled,
  DislikeOutlined,
  DislikeFilled,
  LoadingOutlined,
} from '@ant-design/icons';
import ReactMarkdown from 'react-markdown';
import MarkdownContent from '@/components/MarkdownContent';
import type {
  KnowledgeReference,
  KnowledgeReferenceLocation,
  ReasoningStep,
  SearchFeedbackType,
  FeedbackReason,
} from '@/types/knowledgeSearch';
import type { OntologyInstanceRow } from '@/types/domainKnowledge';
import type { SearchSession } from './index';
import { getChunkDetail } from '@/api/domainKnowledge';
import OntologyEntityDrawer from './OntologyEntityDrawer';
import RagRecallDrawer, { type RagRecallItem } from './RagRecallDrawer';
import CitationDetailPanel from './CitationDetailPanel';
import {
  CITE_BADGE_CLASS,
  formatTimestamp,
  isMarkdownSnippet,
  parseCiteIndex,
  preferredLocation,
  rehypeCitationBadge,
  snippetLocation,
} from './citationIndex';
import styles from './Citation.module.scss';
// ── Helper: 媒体类型标签 ──
function getMediaLabel(type: string, t: (key: string) => string): string {
  const map: Record<string, string> = {
    text: 'knowledgeSearch.mediaTypeText',
    pdf: 'knowledgeSearch.mediaTypePdf',
    audio: 'knowledgeSearch.mediaTypeAudio',
    video: 'knowledgeSearch.mediaTypeVideo',
    image: 'knowledgeSearch.mediaTypeImage',
    office: 'knowledgeSearch.mediaTypeOffice',
  };
  return t(map[type] || 'knowledgeSearch.mediaTypeOther');
}

// ── [jonex] §image-refs P3-2: 图片资产（answer 图片条 / 卡片缩略图共用） ──
interface ImageAsset {
  docId: string;
  imageIdx: number;
  assetUrl: string;
  pageNo?: number | null;
  description?: string;
}

/** 从引用集合提取图片资产：筛 locations[].type === 'image' 且 asset_url
 *  非空，按 (doc_id, image_idx) 去重；描述文本剥「图片描述：」前缀。
 *  与答案同源（_rag_platform_answer 的 references 与 prompt 同源，见
 *  image-reference-chain-execution-plan.md P3 设计决策）。 */
function collectImageAssets(references?: KnowledgeReference[] | null): ImageAsset[] {
  const seen = new Set<string>();
  const out: ImageAsset[] = [];
  for (const ref of references || []) {
    for (const loc of ref.locations || []) {
      if (loc.type !== 'image' || !loc.asset_url || loc.image_idx == null) continue;
      const key = `${ref.doc_id}:${loc.image_idx}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({
        docId: ref.doc_id,
        imageIdx: loc.image_idx,
        assetUrl: loc.asset_url,
        pageNo: loc.page_no,
        description: (loc.text || '').replace(/^图片描述：/, '').trim(),
      });
    }
  }
  return out;
}

// ── [jonex] §image-refs P3-2: 单张图片资产缩略图（loading 骨架 / 失败降级 / 页码角标） ──
function ImageAssetThumb({
  asset,
  compact = false,
  t,
  onOpen,
}: {
  asset: ImageAsset;
  compact?: boolean;
  t: (key: string, opts?: any) => string;
  onOpen?: (asset: ImageAsset) => void;
}) {
  const [state, setState] = React.useState<'loading' | 'ok' | 'error'>('loading');
  const size = compact ? 72 : 96;
  return (
    <div
      style={{
        width: size,
        flexShrink: 0,
        border: '1px solid #e8edf5',
        borderRadius: 8,
        overflow: 'hidden',
        background: '#fff',
        cursor: state === 'ok' ? 'pointer' : 'default',
      }}
      title={asset.description || undefined}
    >
      <div
        style={{
          width: size,
          height: size,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          position: 'relative',
        }}
        onClick={state === 'ok' ? () => onOpen?.(asset) : undefined}
      >
        {state === 'loading' && <Spin size="small" />}
        {state !== 'error' && (
          <img
            src={asset.assetUrl}
            alt={asset.description || `img_${asset.imageIdx}`}
            style={{
              width: '100%',
              height: '100%',
              objectFit: 'cover',
              position: 'absolute',
              inset: 0,
              opacity: state === 'ok' ? 1 : 0,
            }}
            onLoad={() => setState('ok')}
            onError={() => setState('error')}
          />
        )}
        {state === 'error' && (
          <div style={{ fontSize: 11, color: '#94a3b8', textAlign: 'center', padding: '0 6px', lineHeight: 1.5 }}>
            {t('knowledgeSearch.imageLoadFailed')}
          </div>
        )}
        {state === 'ok' && asset.pageNo != null && (
          <span
            style={{
              position: 'absolute',
              bottom: 3,
              right: 3,
              fontSize: 10,
              color: '#fff',
              background: 'rgba(15,23,42,0.55)',
              borderRadius: 3,
              padding: '0 4px',
              lineHeight: 1.6,
            }}
          >
            {/* [jonex] 图片引用准确性修复：page_no 全链路 0-based（MinerU page_idx
                原样透传），展示层 +1 对齐用户认知的 PDF 真实页码 */}
            {t('knowledgeSearch.imagePage', { page: asset.pageNo + 1 })}
          </span>
        )}
      </div>
    </div>
  );
}

// ── Helper: 推理链状态元 ──
function reasoningStatusMeta(status: string, t: (key: string) => string): { color: string; bg: string; label: string } {
  switch (status) {
    case 'done':
      return { color: '#10b981', bg: '#ecfdf5', label: t('status.done') };
    case 'skipped':
      return { color: '#94a3b8', bg: '#f1f5f9', label: t('status.skipped') };
    case 'failed':
      return { color: '#ef4444', bg: '#fef2f2', label: t('status.failed') };
    case 'running':
      return { color: '#3b82f6', bg: '#eff6ff', label: t('status.running') };
    default:
      return { color: '#64748b', bg: '#f1f5f9', label: status };
  }
}

// ── Helper: 数值安全格式化 ──
// [jonex] 2026-09-23：**缺失 ≠ 0**。原实现对 null/undefined 走 `Number(v)`，
// 而 `Number(null) === 0` → 渲染成 "0.00"，把「这个字段没有值」显示成「分值是 0」。
// 线上实例：本体候选里**只被向量召回**的实体（融合产物没有 `score` 键）在面板上
// 显示成「score 0.00」，被读成「召回了但打分是 0」。与后端 `_route_trace` 修的是
// 同一类假 0，只是这一头在前端。缺失一律显示「—」。
function fmtScore(v: unknown): string {
  if (v === null || v === undefined) return '—';
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n.toFixed(2) : String(v);
}

// ── 推理状态小标签 ──
function ReasoningChip({
  children,
  tone = 'default',
}: {
  children: React.ReactNode;
  tone?: 'default' | 'primary' | 'success' | 'danger';
}) {
  const tones = {
    default: { bg: '#f1f5f9', color: '#64748b' },
    primary: { bg: '#f5f3ff', color: '#7c3aed' },
    success: { bg: '#ecfdf5', color: '#10b981' },
    danger: { bg: '#fef2f2', color: '#ef4444' },
  } as const;
  const t = tones[tone];
  return (
    <span
      style={{
        fontSize: 11,
        background: t.bg,
        color: t.color,
        borderRadius: 4,
        padding: '1px 7px',
        whiteSpace: 'nowrap',
      }}
    >
      {children}
    </span>
  );
}

// ── 推理步骤详情 ──
function StepDetail({
  step,
  t,
  onEntityClick,
  onRecallClick,
  references,
  onOpenReference,
}: {
  step: any;
  t: (key: string, opts?: any) => string;
  onEntityClick?: (hit: OntologyInstanceRow & { score?: number; kb_id?: string }) => void;
  onRecallClick?: (item: RagRecallItem, rank: number, score?: number | null) => void;
  references?: any[];
  onOpenReference?: (ref: any) => void;
}) {
  if (!step.detail) return null;
  const d = step.detail as Record<string, any>;

  const wrap = (children: React.ReactNode) => (
    <div
      style={{ marginTop: 8, background: '#fff', border: '1px solid #efeafc', borderRadius: 8, padding: '8px 10px' }}
    >
      {children}
    </div>
  );

  switch (step.stage) {
    case 'ontology_match': {
      const hits: any[] = Array.isArray(d.hits) ? d.hits : [];
      if (hits.length === 0) return null;
      return wrap(
        <>
          <div style={{ fontSize: 11, color: '#94a3b8', marginBottom: 6 }}>
            {t('knowledgeSearch.hitEntities', {
              count: hits.length,
              total: d.total_hits ?? hits.length,
              kbCount: d.kb_count ?? '—',
            })}
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {hits.map((h, idx) => (
              <Button
                key={idx}
                size="small"
                type="default"
                onClick={() => onEntityClick?.(h)}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 6,
                  fontSize: 12,
                  background: '#f7f5fe',
                  borderColor: '#ece9fb',
                  borderRadius: 6,
                  padding: '2px 8px',
                  height: 'auto',
                }}
                onMouseEnter={(e) => (e.currentTarget.style.background = '#efeafc')}
                onMouseLeave={(e) => (e.currentTarget.style.background = '#f7f5fe')}
              >
                <span style={{ fontWeight: 600, color: '#4c1d95' }}>{h.name}</span>
                {/* [jonex] 判据分（rerank）：rerank 生效时**决定路由的就是它**，
                    而它在匹配之后才算 → 由后端按**对象身份**回填进这一步的 hits
                    （F1：不能按实体名——同 KB 跨 entity_type 重名是常态，会张冠李戴）。
                    F2 补进来的项带 `_from_gate`（过 gate 后追加，正常渲染即可）。 */}
                {h.rerank_score != null && (
                  <span style={{ fontSize: 11, color: '#7c3aed' }}>
                    {t('knowledgeSearch.matchRerank', { score: fmtScore(h.rerank_score) })}
                  </span>
                )}
                {/* [jonex] 检索分：**两条召回路径各产出一个、且常常只有一个非零**
                    （全文命中 ft_score>0/vscore=0；纯向量命中反之），故哪个有值
                    显示哪个，并标明是全文还是向量——不再凑成无意义的 "score 0.00"。 */}
                {(h.ft_score || h.vscore) && (
                  <span style={{ fontSize: 11, color: '#a78bda' }}>
                    {[
                      h.ft_score
                        ? t('knowledgeSearch.matchFulltext', { score: fmtScore(h.ft_score) })
                        : null,
                      h.vscore
                        ? t('knowledgeSearch.matchVector', { score: fmtScore(h.vscore) })
                        : null,
                    ]
                      .filter(Boolean)
                      .join(' · ')}
                  </span>
                )}
                {h.kb_id && <span style={{ fontSize: 10, color: '#b6bdc7' }}>{h.kb_id}</span>}
              </Button>
            ))}
          </div>
        </>,
      );
    }
    case 'route_decision': {
      const route = d.route;
      const routeLabel =
        route === 'ontology'
          ? t('knowledgeSearch.ontologyRoute')
          : route === 'rag'
            ? t('knowledgeSearch.ragRoute')
            : String(route ?? '—');
      // [jonex] C（2026-09-23）：只展示**生效判据**那一套。rerank 生效时
      // vscore/ft_score 的阈值根本不参与判定——照旧展示会让人拿「全文分 14.65 ≫
      // 阈值 1.00」去质问「凭什么还降级」。两者仍在 detail 里（供日志/接口排查），
      // 只是不再当作判据摆在这里。
      // 后端旧版本无 `threshold_source` → 落回旧展示，前向兼容。
      const rerankMode = d.threshold_source === 'rerank';
      return wrap(
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
          <ReasoningChip tone="primary">{t('knowledgeSearch.routeTo', { route: routeLabel })}</ReasoningChip>
          {rerankMode ? (
            <ReasoningChip>
              {t('knowledgeSearch.rerankScore', {
                // fmtScore 现已把缺失渲染成「—」，无需再 ?? 兜底
                score: fmtScore(d.rerank_score),
                threshold: fmtScore(d.rerank_threshold),
              })}
            </ReasoningChip>
          ) : (
            <>
              {d.ft_score != null && (
                <ReasoningChip>
                  {t('knowledgeSearch.ftScore', { score: fmtScore(d.ft_score), threshold: fmtScore(d.ftscore_threshold) })}
                </ReasoningChip>
              )}
              {d.vscore != null && (
                <ReasoningChip>
                  {t('knowledgeSearch.vscore', { score: fmtScore(d.vscore), threshold: fmtScore(d.vscore_threshold) })}
                </ReasoningChip>
              )}
            </>
          )}
        </div>,
      );
    }
    case 'fact_lookup': {
      return wrap(
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
          {d.entity && <ReasoningChip tone="primary">{t('knowledgeSearch.entity', { name: d.entity })}</ReasoningChip>}
          {d.fact_count != null && (
            <ReasoningChip tone="success">{t('knowledgeSearch.factsCount', { count: d.fact_count })}</ReasoningChip>
          )}
          {d.kb_id && <ReasoningChip>{d.kb_id}</ReasoningChip>}
        </div>,
      );
    }
    // [jonex] 2026-09-22：原 case 名属已删除的旧链路 stage（多库生成 + 融合）。
    // 现路径（平台取回答作权）发 'context_retrieve'，detail 字段（kb_ok /
    // kb_failed / recalls）与原 case 兼容，故直接沿用它渲染召回明细 + 抽屉。
    // 注意 context_retrieve 有多个发送点（低质过滤 / 双路配额 / 召回明细 / 空命中），
    // 非召回明细那几条 detail 三项全空 → 下面的 hasContent 守卫返回 null，
    // 与改前落 `default: return null` 一致，不引入回归。
    case 'context_retrieve': {
      const ok: string[] = Array.isArray(d.kb_ok) ? d.kb_ok : [];
      const failed: string[] = Array.isArray(d.kb_failed) ? d.kb_failed : [];
      const recalls: RagRecallItem[] = Array.isArray(d.recalls) ? d.recalls : [];
      const hasContent = ok.length > 0 || failed.length > 0 || recalls.length > 0;
      if (!hasContent) return null;
      return wrap(
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {ok.length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
              <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('knowledgeSearch.hitKbs')}</span>
              {ok.map((k) => (
                <ReasoningChip key={k} tone="success">
                  {k}
                </ReasoningChip>
              ))}
            </div>
          )}
          {failed.length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
              <span style={{ fontSize: 11, color: '#94a3b8' }}>{t('knowledgeSearch.failedKbs')}</span>
              {failed.map((k) => (
                <ReasoningChip key={k} tone="danger">
                  {k}
                </ReasoningChip>
              ))}
            </div>
          )}
          {recalls.length > 0 && (
            <div>
              <div style={{ fontSize: 11, color: '#94a3b8', marginBottom: 6 }}>{t('knowledgeSearch.recallChunks')}</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {recalls.map((item, idx) => {
                  const rank = idx + 1;
                  // 每条自带分（后端 recalls[].score，走统一分、缺失退 relevance）。
                  // 旧实现是按位次对齐另一份 prellm 分数数组，后端已不再产出。
                  const score = item.score ?? null;
                  return (
                    <Button
                      key={item.chunk_id || idx}
                      size="small"
                      type="default"
                      onClick={() => onRecallClick?.(item, rank, score ?? null)}
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: 6,
                        fontSize: 12,
                        background: '#f0f9ff',
                        borderColor: '#bae6fd',
                        borderRadius: 6,
                        padding: '2px 8px',
                        height: 'auto',
                      }}
                      onMouseEnter={(e) => (e.currentTarget.style.background = '#e0f2fe')}
                      onMouseLeave={(e) => (e.currentTarget.style.background = '#f0f9ff')}
                    >
                      <span style={{ fontWeight: 600, color: '#0369a1' }}>
                        {t('knowledgeSearch.chunkTag', { rank: String(rank).padStart(3, '0') })}
                      </span>
                      <span style={{ fontSize: 11, color: '#38bdf8' }}>
                        {score != null ? `${t('knowledgeSearch.relevanceShort')} ${fmtScore(score)}` : ''}
                      </span>
                    </Button>
                  );
                })}
              </div>
            </div>
          )}
        </div>,
      );
    }
    case 'openkb_query': {
      const turns: any[] = Array.isArray(d.turns) ? d.turns : [];
      // [jonex] 构建 wiki_path → reference 索引，供路径可点开
      const refByPath: Record<string, any> = {};
      if (references && onOpenReference) {
        for (const r of references) {
          if (r.wiki_path) refByPath[r.wiki_path] = r;
        }
      }
      const renderCall = (c: any, idx: number) => {
        const wikiPath: string = c.args?.path || '';
        const ref = wikiPath ? refByPath[wikiPath] : undefined;
        const canOpen = Boolean(ref && onOpenReference);
        return (
          <div key={idx} style={{ display: 'flex', alignItems: 'baseline', gap: 6, fontSize: 12 }}>
            <ReasoningChip tone="primary">{c.tool}</ReasoningChip>
            {canOpen ? (
              <a
                onClick={(e) => {
                  e.preventDefault();
                  onOpenReference!(ref);
                }}
                style={{
                  color: '#4c1d95',
                  cursor: 'pointer',
                  wordBreak: 'break-all' as const,
                  textDecoration: 'underline',
                }}
              >
                {wikiPath}
              </a>
            ) : (
              <span style={{ color: '#4c1d95', wordBreak: 'break-all' as const }}>
                {c.args?.path || c.args?.doc_name || c.args?.image_path || '—'}
              </span>
            )}
            {c.output_chars != null && <span style={{ fontSize: 10, color: '#b6bdc7' }}>{c.output_chars} chars</span>}
          </div>
        );
      };
      if (turns.length === 0) {
        // fallback to flat tool_calls for backward compat
        const flat: any[] = Array.isArray(d.tool_calls) ? d.tool_calls : [];
        if (flat.length === 0) return null;
        return wrap(
          <>
            <div style={{ fontSize: 11, color: '#94a3b8', marginBottom: 6 }}>
              {t('knowledgeSearch.wikiBrowsed', { count: flat.length })}
            </div>
            <div
              style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 240, overflowY: 'auto' as const }}
            >
              {flat.map((c, idx) => renderCall(c, idx))}
            </div>
            {d.tool_calls_truncated && (
              <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 4 }}>
                {t('knowledgeSearch.wikiTruncated', { shown: flat.length, total: d.tool_calls_total })}
              </div>
            )}
          </>,
        );
      }
      return wrap(
        <>
          <div style={{ fontSize: 11, color: '#94a3b8', marginBottom: 6 }}>
            {t('knowledgeSearch.wikiTurns', { turns: turns.length, calls: d.tool_calls_total })}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 320, overflowY: 'auto' as const }}>
            {turns.map((turn: any, ti: number) => (
              <div key={ti}>
                <div
                  style={{ display: 'flex', alignItems: 'baseline', gap: 6, marginBottom: turn.calls?.length ? 2 : 0 }}
                >
                  <ReasoningChip tone="primary">
                    {/* [jonex] 后端 turn.index 从 0 开始（len(turns) 递增），
                        展示"第几轮"需 +1；无 index 时用数组位置 ti 兜底同样 +1。
                        注意不能写 turn.index ?? ti + 1——那会在有 index 时显示 0 起始。 */}
                    {t('knowledgeSearch.wikiTurn', { index: (turn.index ?? ti) + 1 })}
                  </ReasoningChip>
                  {turn.llm_ms != null && (
                    <span style={{ fontSize: 10, color: '#b6bdc7' }}>
                      {turn.llm_ms >= 1000 ? `${(turn.llm_ms / 1000).toFixed(1)}s` : `${turn.llm_ms}ms`}
                    </span>
                  )}
                </div>
                {turn.thinking && (
                  <div
                    style={{
                      fontSize: 11,
                      color: '#64748b',
                      marginBottom: turn.calls?.length ? 4 : 0,
                      lineHeight: 1.5,
                    }}
                    title={turn.thinking}
                  >
                    {turn.thinking.length > 120 ? turn.thinking.slice(0, 120) + '…' : turn.thinking}
                  </div>
                )}
                {(turn.calls || []).map((c: any, ci: number) => renderCall(c, ci))}
              </div>
            ))}
          </div>
          {d.tool_calls_truncated && (
            <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 4 }}>
              {t('knowledgeSearch.wikiTruncated', {
                shown: turns.reduce((s: number, t: any) => s + (t.calls?.length || 0), 0),
                total: d.tool_calls_total,
              })}
            </div>
          )}
        </>,
      );
    }
    case 'jonex_wiki_query': {
      // [jonex] jonex_wiki 引擎专用端点（/search/jonex_wiki）的检索链路：
      // 后端把引擎公开 trace 投影成 calls[]（wiki_search 带检索词 + 候选页、
      // wiki_read 带实读页、query_rewrite/query_constraints 带改写与约束），
      // 与 openkb 的「轮次 + 工具调用」不同——这里是一条平铺的调用序列。
      const calls: any[] = Array.isArray(d.calls) ? d.calls : [];
      if (calls.length === 0) return null;
      // 链路里的页路径若命中某条引用的 wiki_path，就渲染成可点开的链接
      const refByPath: Record<string, any> = {};
      (references || []).forEach((r: any) => {
        if (r?.wiki_path) refByPath[r.wiki_path] = r;
      });
      const labelOf = (tool: string) => {
        if (tool === 'wiki_search') return t('knowledgeSearch.jonexSearch');
        if (tool === 'wiki_read') return t('knowledgeSearch.jonexRead');
        if (tool === 'query_rewrite') return t('knowledgeSearch.jonexRewrite');
        if (tool === 'query_constraints') return t('knowledgeSearch.jonexConstraints');
        return tool;
      };
      // 引擎公开 span 的耗时（只投 tool/evidence/synthesis，见后端 _jonex_wiki_chain_spans）
      const spans: any[] = Array.isArray(d.spans) ? d.spans : [];
      const toolMs = spans.reduce(
        (sum: number, s: any) => sum + (typeof s?.duration_ms === 'number' ? s.duration_ms : 0),
        0,
      );
      return wrap(
        <>
          <div style={{ fontSize: 11, color: '#94a3b8', marginBottom: 6 }}>
            {t('knowledgeSearch.jonexChain', {
              count: d.tool_calls_total ?? calls.length,
              citations: d.citation_count ?? 0,
            })}
            {toolMs > 0 ? ' · ' + t('knowledgeSearch.jonexToolMs', { ms: toolMs }) : ''}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 320, overflowY: 'auto' as const }}>
            {calls.map((c: any, ci: number) => {
              const tool = String(c.tool || '');
              const paths: string[] = Array.isArray(c.paths) ? c.paths : [];
              const shown = paths.slice(0, 3);
              const queryText = c.query || c.from_query || '';
              const showQuery = tool === 'wiki_search' || tool === 'query_rewrite' || tool === 'query_constraints';
              return (
                <div
                  key={ci}
                  style={{ display: 'flex', alignItems: 'baseline', gap: 6, fontSize: 12, flexWrap: 'wrap' }}
                >
                  <ReasoningChip tone="primary">{labelOf(tool)}</ReasoningChip>
                  {showQuery && queryText && (
                    <span style={{ color: '#4c1d95', wordBreak: 'break-all' as const }}>{queryText}</span>
                  )}
                  {shown.length > 0 && (
                    <span style={{ color: '#64748b', wordBreak: 'break-all' as const }}>
                      {shown.map((p: string, pi: number) => {
                        const ref = refByPath[p];
                        return (
                          <span key={p + pi}>
                            {pi > 0 ? '、' : ''}
                            {ref && onOpenReference ? (
                              <a
                                onClick={(e) => {
                                  e.preventDefault();
                                  onOpenReference(ref);
                                }}
                                style={{ color: '#4c1d95', cursor: 'pointer' }}
                                title={p}
                              >
                                {p}
                              </a>
                            ) : (
                              <span title={p}>{p}</span>
                            )}
                          </span>
                        );
                      })}
                      {paths.length > shown.length ? ' +' + (paths.length - shown.length) : ''}
                    </span>
                  )}
                  {c.count != null && (
                    <span style={{ fontSize: 10, color: '#b6bdc7' }}>
                      {t('knowledgeSearch.jonexCandidates', { count: c.count })}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
          {d.tool_calls_truncated && (
            <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 4 }}>
              {t('knowledgeSearch.wikiTruncated', { shown: calls.length, total: d.tool_calls_total })}
            </div>
          )}
        </>,
      );
    }
    default:
      return null;
  }
}

// ── 反馈按钮 ──
const FEEDBACK_REASONS: FeedbackReason[] = [
  'inaccurate',
  'not_answered',
  'incomplete',
  'wrong_reference',
  'missing_knowledge',
  'other',
];

function FeedbackButtons({
  activeVote,
  loading,
  onVote,
  dislikeOpen,
  voteReason,
  voteComment,
  onDislikeOpenChange,
  onVoteReasonChange,
  onVoteCommentChange,
  onDislikeConfirm,
}: {
  activeVote: SearchFeedbackType | null;
  loading: SearchFeedbackType | null;
  onVote: (type: SearchFeedbackType) => void;
  dislikeOpen: boolean;
  voteReason: FeedbackReason | null;
  voteComment: string;
  onDislikeOpenChange: (open: boolean) => void;
  onVoteReasonChange: (reason: FeedbackReason) => void;
  onVoteCommentChange: (comment: string) => void;
  onDislikeConfirm: () => void;
}) {
  const { t } = useTranslation();
  const isLikeLoading = loading === 'like';
  const isDislikeLoading = loading === 'dislike';
  const isLikeSelected = activeVote === 'like';
  const isDislikeSelected = activeVote === 'dislike';

  return (
    <div
      style={{
        marginTop: 14,
        paddingTop: 12,
        borderTop: '1px solid #e8edf5',
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        fontSize: 13,
        color: '#94a3b8',
      }}
    >
      <span style={{ color: '#94a3b8' }}>{t('knowledgeSearch.helpfulQuestion')}</span>

      {/* 有帮助 */}
      <button
        type="button"
        disabled={!!loading}
        onClick={() => onVote('like')}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 5,
          border: 'none',
          fontSize: 13,
          borderRadius: 16,
          padding: '3px 14px',
          cursor: loading ? 'default' : 'pointer',
          lineHeight: '22px',
          fontWeight: isLikeSelected ? 600 : 400,
          color: isLikeSelected ? '#fff' : '#94a3b8',
          background: isLikeLoading ? '#bbf7d0' : isLikeSelected ? '#22c55e' : '#f1f5f9',
          boxShadow: isLikeSelected ? '0 1px 4px rgba(34,197,94,0.35)' : 'none',
          transition: 'all 0.25s ease',
          opacity: loading && !isLikeLoading ? 0.5 : 1,
        }}
        onMouseEnter={(e) => {
          if (!loading && !isLikeSelected) {
            e.currentTarget.style.background = '#dcfce7';
            e.currentTarget.style.color = '#16a34a';
          }
        }}
        onMouseLeave={(e) => {
          if (!loading && !isLikeSelected) {
            e.currentTarget.style.background = '#f1f5f9';
            e.currentTarget.style.color = '#94a3b8';
          }
        }}
      >
        {isLikeLoading ? (
          <LoadingOutlined style={{ fontSize: 14, color: '#16a34a' }} />
        ) : isLikeSelected ? (
          <LikeFilled style={{ fontSize: 14 }} />
        ) : (
          <LikeOutlined style={{ fontSize: 14 }} />
        )}
        {isLikeLoading ? t('knowledgeSearch.submitting') : t('knowledgeSearch.helpful')}
      </button>

      {/* 无帮助：点踩原因 + 备注走 Popconfirm（按钮下方弹出） */}
      <Popconfirm
        open={dislikeOpen}
        onOpenChange={onDislikeOpenChange}
        placement="bottom"
        icon={null}
        title={t('knowledgeSearch.feedbackReasonTitle')}
        description={
          <div style={{ width: 300 }}>
            <div style={{ marginBottom: 8, fontSize: 13, fontWeight: 600, color: '#334155' }}>
              {t('knowledgeSearch.feedbackReasonLabel')}
            </div>
            <Radio.Group
              value={voteReason}
              onChange={(e) => onVoteReasonChange(e.target.value)}
              style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 8 }}
            >
              {FEEDBACK_REASONS.map((reason) => (
                <Radio key={reason} value={reason} style={{ fontSize: 13, color: '#475569' }}>
                  {t(`knowledgeSearch.feedbackReason_${reason}`)}
                </Radio>
              ))}
            </Radio.Group>
            <div style={{ marginTop: 14, marginBottom: 6, fontSize: 13, fontWeight: 600, color: '#334155' }}>
              {t('knowledgeSearch.feedbackCommentLabel')}
            </div>
            <Input.TextArea
              value={voteComment}
              onChange={(e) => onVoteCommentChange(e.target.value)}
              maxLength={300}
              rows={3}
              placeholder={t('knowledgeSearch.feedbackCommentPlaceholder')}
            />
          </div>
        }
        okText={t('knowledgeSearch.feedbackSubmit')}
        cancelText={t('common.cancel')}
        okButtonProps={{ loading: isDislikeLoading }}
        onConfirm={onDislikeConfirm}
      >
        <button
          type="button"
          disabled={!!loading}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 5,
            border: 'none',
            fontSize: 13,
            borderRadius: 16,
            padding: '3px 14px',
            cursor: loading ? 'default' : 'pointer',
            lineHeight: '22px',
            fontWeight: isDislikeSelected ? 600 : 400,
            color: isDislikeSelected ? '#fff' : '#94a3b8',
            background: isDislikeLoading ? '#fecaca' : isDislikeSelected ? '#ef4444' : '#f1f5f9',
            boxShadow: isDislikeSelected ? '0 1px 4px rgba(239,68,68,0.35)' : 'none',
            transition: 'all 0.25s ease',
            opacity: loading && !isDislikeLoading ? 0.5 : 1,
          }}
          onMouseEnter={(e) => {
            if (!loading && !isDislikeSelected) {
              e.currentTarget.style.background = '#ffe4e6';
              e.currentTarget.style.color = '#e11d48';
            }
          }}
          onMouseLeave={(e) => {
            if (!loading && !isDislikeSelected) {
              e.currentTarget.style.background = '#f1f5f9';
              e.currentTarget.style.color = '#94a3b8';
            }
          }}
        >
          {isDislikeLoading ? (
            <LoadingOutlined style={{ fontSize: 14, color: '#e11d48' }} />
          ) : isDislikeSelected ? (
            <DislikeFilled style={{ fontSize: 14 }} />
          ) : (
            <DislikeOutlined style={{ fontSize: 14 }} />
          )}
          {isDislikeLoading ? t('knowledgeSearch.submitting') : t('knowledgeSearch.unhelpful')}
        </button>
      </Popconfirm>
    </div>
  );
}

// ── 解析 <think> 标签 ──
function parseThink(raw: string): { think: string; answer: string; thinking: boolean } {
  const start = raw.indexOf('<think>');
  if (start < 0) return { think: '', answer: raw.trim(), thinking: false };
  const before = raw.slice(0, start);
  const afterStart = raw.slice(start + '<think>'.length);
  const end = afterStart.indexOf('</think>');
  if (end < 0) return { think: afterStart.trim(), answer: before.trim(), thinking: true };
  const think = afterStart.slice(0, end).trim();
  const answer = `${before}${afterStart.slice(end + '</think>'.length)}`.trim();
  return { think, answer, thinking: false };
}

// ── 解析引用 ──
function parseReferences(text: string): { refs: string[]; body: string } {
  const refs: string[] = [];
  const normalized = text.replace(/\r\n/g, '\n');
  const sourcePathPattern = /(?:\/app\/inputs\/|\/app\/outputs\/|\/tmp\/rag_output\/)/;
  const normalizeReferenceLine = (line: string) =>
    line
      .replace(/^\s*(?:#{1,6}\s*)?(?:References|参考文献|原文引用|引用来源)\s*[:：]?\s*/i, '')
      .replace(/^\s*[-*+]\s*/, '')
      .replace(/^\s*\d+[.)]\s*/, '')
      .replace(/^\s*\[\d+\]\s*/, '')
      .trim();
  const collectRef = (line: string) => {
    const cleaned = normalizeReferenceLine(line);
    if (cleaned) refs.push(cleaned);
  };
  const isReferenceHeading = (line: string) =>
    /^\s{0,3}(?:#{1,6}\s*)?(?:References|参考文献|原文引用|引用来源)\s*[:：]?\s*/i.test(line);
  const hasReferencePayload = (line: string) => sourcePathPattern.test(normalizeReferenceLine(line));
  const isSourcePathLine = (line: string) => sourcePathPattern.test(normalizeReferenceLine(line));
  const bodyLines: string[] = [];
  let inReferenceBlock = false;
  normalized.split('\n').forEach((line) => {
    if (isReferenceHeading(line)) {
      inReferenceBlock = true;
      if (hasReferencePayload(line)) collectRef(line);
      return;
    }
    if (inReferenceBlock) {
      if (line.trim()) collectRef(line);
      return;
    }
    if (isSourcePathLine(line)) {
      collectRef(line);
      return;
    }
    bodyLines.push(line);
  });
  const body = bodyLines
    .join('\n')
    .replace(
      /^\s*(?:[-*+]\s*)?(?:\d+[.)]\s*)?(?:\[\d+\]\s*)?(?:\/app\/inputs\/|\/app\/outputs\/|\/tmp\/rag_output\/)[^\n]*/gm,
      (line) => {
        collectRef(line);
        return '';
      },
    )
    .replace(
      /^\s*(?:[-*+]\s*)?(?:\d+[.)]\s*)?(?:\[\d+\]\s*)?(?:References|参考文献|原文引用|引用来源)\s*[:：]\s*(?:\/app\/inputs\/|\/app\/outputs\/|\/tmp\/rag_output\/)[^\n]*/gim,
      (line) => {
        collectRef(line);
        return '';
      },
    )
    .trim();
  return { refs: Array.from(new Set(refs)), body };
}

function isSearchRunning(status?: string): boolean {
  return status === 'searching';
}

function getSearchStatusLabel(status: string, t: (key: string) => string): string {
  switch (status) {
    case 'searching':
      return t('status.searching');
    case 'done':
      return t('status.completed');
    case 'error':
      return t('status.searchFailed');
    case 'stopped':
      return t('status.stopped');
    case 'empty':
      return t('status.noResults');
    default:
      return t('status.pendingSearch');
  }
}

// ═══════════════════════════════════════════════════════════
// SessionCard 主组件
// ═══════════════════════════════════════════════════════════

interface SessionCardProps {
  session: SearchSession;
  domainName: string;
  thinkExpanded: boolean;
  reasoningExpanded: boolean;
  refsExpanded: boolean;
  sessionVote: SearchFeedbackType | null;
  feedbackLoading: SearchFeedbackType | null;
  thinkGenerating: boolean;
  onToggleThink: (sessionId: string) => void;
  onToggleReasoning: (sessionId: string) => void;
  onToggleRefs: (sessionId: string) => void;
  onStop: () => void;
  onReSearch: (query: string, domainId: string) => void;
  onClear: () => void;
  onVote: (type: SearchFeedbackType) => void;
  onOpenReference: (ref: KnowledgeReference, loc?: KnowledgeReferenceLocation) => void;
  /** 点踩 Popconfirm 受控状态与表单（由父级 KnowledgeSearch 持有） */
  dislikeOpen: boolean;
  voteReason: FeedbackReason | null;
  voteComment: string;
  onDislikeOpenChange: (open: boolean) => void;
  onVoteReasonChange: (reason: FeedbackReason) => void;
  onVoteCommentChange: (comment: string) => void;
  onDislikeConfirm: () => void;
}

export default function SessionCard({
  session,
  domainName,
  thinkExpanded,
  reasoningExpanded,
  refsExpanded,
  sessionVote,
  feedbackLoading,
  thinkGenerating,
  onToggleThink,
  onToggleReasoning,
  onToggleRefs,
  onStop,
  onReSearch,
  onClear,
  onVote,
  onOpenReference,
  dislikeOpen,
  voteReason,
  voteComment,
  onDislikeOpenChange,
  onVoteReasonChange,
  onVoteCommentChange,
  onDislikeConfirm,
}: SessionCardProps) {
  const { t } = useTranslation();

  const parsedAnswer = parseThink(session.rawAnswer);
  const { body: responseBody } = parseReferences(parsedAnswer.answer);
  const running = isSearchRunning(session.status);
  const hasThink = parsedAnswer.think.length > 0;
  const hasAnswerContent = responseBody.length > 0;
  const hasContent = hasThink || hasAnswerContent;
  const statusLabel = getSearchStatusLabel(session.status, t);

  // [jonex] §image-refs P3-2: 答案旁图片资产（与 prompt 同源的 references 中筛出）
  const imageAssets = React.useMemo(() => collectImageAssets(session.references), [session.references]);
  const openImageAsset = React.useCallback((asset: ImageAsset) => {
    window.open(asset.assetUrl, '_blank', 'noopener');
  }, []);

  const [entityDrawerOpen, setEntityDrawerOpen] = React.useState(false);
  const [drawerEntity, setDrawerEntity] = React.useState<OntologyInstanceRow | null>(null);

  const [ragRecallOpen, setRagRecallOpen] = React.useState(false);
  const [ragRecallItem, setRagRecallItem] = React.useState<RagRecallItem | null>(null);
  const [ragRecallRank, setRagRecallRank] = React.useState(1);
  const [ragRecallScore, setRagRecallScore] = React.useState<number | null>(null);
  const [chunkLoading, setChunkLoading] = React.useState(false);

  // [jonex] 引用角标激活下标（0-based，与 session.references 对齐）。
  // 归 SessionCard 自己持有：每会话一个实例，天然隔离，无需上提到 index.tsx。
  const [activeCitationIndex, setActiveCitationIndex] = React.useState<number | null>(null);
  const referenceCount = session.references?.length ?? 0;

  // 重新检索后归零，避免面板指向上一轮的下标。
  // 只依赖 session.id：每次检索都生成新 id（index.tsx `Date.now().toString()`），
  // 足以覆盖「重新检索复位」；不能依赖 session.rawAnswer —— 它随每个流式
  // delta 变化（index.tsx `prev.rawAnswer + delta`），会在流式期间把面板反复关掉。
  React.useEffect(() => {
    setActiveCitationIndex(null);
  }, [session.id]);

  // 激活的引用被后续流式更新移除时同步归零，防止面板挂着空引用
  React.useEffect(() => {
    if (activeCitationIndex !== null && activeCitationIndex >= referenceCount) {
      setActiveCitationIndex(null);
    }
  }, [activeCitationIndex, referenceCount]);

  const openEntityDrawer = (hit: OntologyInstanceRow & { score?: number; kb_id?: string }) => {
    // 本体实体信息直接取自问答接口返回的 ontology_match hits（含 description/attributes），不再二次查询
    setDrawerEntity(hit);
    setEntityDrawerOpen(true);
  };

  const entityAttributeEntries = React.useMemo(() => {
    if (!drawerEntity?.attributes) return [];
    return Object.entries(drawerEntity.attributes).map(([key, value]) => ({ key, value }));
  }, [drawerEntity]);

  const openRagRecallDrawer = async (item: RagRecallItem, rank: number, score?: number | null) => {
    setRagRecallItem(item);
    setRagRecallRank(rank);
    setRagRecallScore(score ?? null);
    setRagRecallOpen(true);
    setChunkLoading(true);
    try {
      const detail = await getChunkDetail(item.doc_id, item.chunk_id);
      setRagRecallItem((prev) =>
        prev && prev.chunk_id === item.chunk_id ? { ...prev, text: detail.content || prev.text } : prev,
      );
    } catch (err: any) {
      message.warning(err?.message || t('knowledgeSearch.chunkLoadError'));
    } finally {
      setChunkLoading(false);
    }
  };

  return (
    <Card style={{ borderRadius: 12, marginBottom: 20 }} styles={{ body: { padding: '20px 24px' } }}>
      {/* Header */}
      <div
        style={{ marginBottom: 16, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}
      >
        <div
          style={{
            minWidth: 0,
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            fontSize: 16,
            fontWeight: 600,
            color: '#0b2b5c',
          }}
        >
          <SearchOutlined style={{ color: '#3b82f6', flexShrink: 0 }} />
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{session.query}</span>
          <span
            style={{
              fontSize: 12,
              color: '#64748b',
              fontWeight: 400,
              background: '#f1f5f9',
              borderRadius: 999,
              padding: '2px 8px',
              flexShrink: 0,
            }}
          >
            {domainName}
          </span>
          {running && (
            <span style={{ fontSize: 12, color: '#94a3b8', fontWeight: 400, marginLeft: 8, flexShrink: 0 }}>
              <Spin size="small" style={{ marginRight: 4 }} />
              {t('knowledgeSearch.searching')}
            </span>
          )}
          {session.status === 'error' && (
            <span style={{ fontSize: 12, color: '#ef4444', fontWeight: 400, flexShrink: 0 }}>{statusLabel}</span>
          )}
          {session.status === 'stopped' && (
            <span style={{ fontSize: 12, color: '#f97316', fontWeight: 400, flexShrink: 0 }}>{statusLabel}</span>
          )}
          {session.status === 'done' && (
            <span style={{ fontSize: 12, color: '#10b981', fontWeight: 400, flexShrink: 0 }}>{statusLabel}</span>
          )}
          {session.source && (
            <span
              style={{
                fontSize: 11,
                color: '#8b5cf6',
                fontWeight: 400,
                background: '#f5f3ff',
                borderRadius: 999,
                padding: '2px 10px',
                flexShrink: 0,
                marginLeft: 'auto',
              }}
            >
              {t('knowledgeSearch.source', { source: session.source })}
            </span>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
          {running ? (
            <Button size="small" icon={<StopOutlined />} onClick={onStop}>
              {t('knowledgeSearch.stopGenerate')}
            </Button>
          ) : (
            <Button size="small" icon={<ReloadOutlined />} onClick={() => onReSearch(session.query, session.domainId)}>
              {t('knowledgeSearch.reSearch')}
            </Button>
          )}
          <Button size="small" icon={<CloseCircleOutlined />} onClick={onClear}>
            {t('knowledgeSearch.clear')}
          </Button>
        </div>
      </div>

      {/* Error */}
      {session.status === 'error' && (
        <div
          style={{
            padding: '12px 14px',
            background: '#fef2f2',
            border: '1px solid #fecaca',
            borderRadius: 10,
            color: '#dc2626',
            fontSize: 13,
            marginBottom: 16,
            lineHeight: 1.7,
          }}
        >
          {session.errorMessage || t('knowledgeSearch.searchFailed')}
        </div>
      )}

      {/* Thinking stream */}
      {hasThink && (
        <div
          style={{
            padding: '12px 14px',
            background: '#f8fafc',
            border: '1px solid #e2e8f0',
            borderRadius: 10,
            marginBottom: 12,
          }}
        >
          <button
            onClick={() => onToggleThink(session.id)}
            style={{
              width: '100%',
              padding: 0,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              color: '#64748b',
              textAlign: 'left',
              height: 'auto',
              lineHeight: 'inherit',
              border: 'none',
              background: 'transparent',
              cursor: 'pointer',
              fontSize: 13,
            }}
          >
            {thinkExpanded ? <CaretDownOutlined /> : <CaretRightOutlined />}
            <span style={{ fontSize: 13, fontWeight: 600, color: '#475569' }}>
              {t('knowledgeSearch.thinkingProcess')}
            </span>
            {thinkGenerating ? (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 12, color: '#94a3b8' }}>
                <Spin size="small" />
                {t('knowledgeSearch.generating')}
              </span>
            ) : (
              <span style={{ fontSize: 12, color: '#94a3b8' }}>
                {thinkExpanded ? t('knowledgeSearch.clickCollapse') : t('knowledgeSearch.collapsed')}
              </span>
            )}
          </button>
          {thinkExpanded ? (
            <div
              style={{
                marginTop: 10,
                padding: '10px 12px',
                background: '#ffffff',
                borderRadius: 8,
                fontSize: 13,
                color: '#64748b',
                lineHeight: 1.75,
                whiteSpace: 'pre-wrap',
              }}
            >
              {parsedAnswer.think}
            </div>
          ) : (
            <div
              style={{
                marginTop: 8,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                fontSize: 12,
                color: '#94a3b8',
              }}
            >
              {parsedAnswer.think.split('\n').find(Boolean) || t('knowledgeSearch.reasoningComplete')}
            </div>
          )}
        </div>
      )}

      {/* Reasoning chain */}
      {(() => {
        const reasoning = session.reasoning;
        return (
          (reasoning?.steps?.length ?? 0) > 0 && (
            <div
              style={{
                background: '#fbfaff',
                border: '1px solid #ece9fb',
                borderRadius: 10,
                padding: '12px 16px',
                marginBottom: 16,
              }}
            >
              <button
                onClick={() => onToggleReasoning(session.id)}
                style={{
                  width: '100%',
                  padding: 0,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  justifyContent: 'flex-start',
                  height: 'auto',
                  lineHeight: 'inherit',
                  border: 'none',
                  background: 'transparent',
                  cursor: 'pointer',
                  fontSize: 13,
                }}
              >
                {reasoningExpanded ? <CaretDownOutlined /> : <CaretRightOutlined />}
                <NodeIndexOutlined style={{ color: '#8b5cf6' }} />
                <span style={{ fontSize: 13, fontWeight: 600, color: '#6d28d9' }}>
                  {t('knowledgeSearch.reasoningProcess')}
                </span>
                <span style={{ fontSize: 12, color: '#a78bda' }}>
                  {t('knowledgeSearch.steps', {
                    count: reasoning!.steps.length,
                    source: reasoning!.final_source,
                  })}
                  {reasoning!.total_ms != null ? ` · ${reasoning!.total_ms}ms` : ''}
                </span>
              </button>
              {reasoningExpanded && (
                <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {reasoning!.steps.map((step: ReasoningStep, i: number) => {
                    const meta = reasoningStatusMeta(step.status, t);
                    return (
                      <div key={`${step.stage}-${i}`} style={{ display: 'flex', gap: 10 }}>
                        <div
                          style={{
                            width: 22,
                            height: 22,
                            borderRadius: '50%',
                            flexShrink: 0,
                            background: meta.bg,
                            color: meta.color,
                            fontSize: 11,
                            fontWeight: 700,
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                          }}
                        >
                          {i + 1}
                        </div>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                            <span style={{ fontSize: 13, fontWeight: 600, color: '#334155' }}>{step.title}</span>
                            <span
                              style={{
                                fontSize: 11,
                                color: meta.color,
                                background: meta.bg,
                                borderRadius: 4,
                                padding: '0 6px',
                              }}
                            >
                              {meta.label}
                            </span>
                            {step.duration_ms != null && (
                              <span style={{ fontSize: 11, color: '#a3adbb' }}>{step.duration_ms}ms</span>
                            )}
                          </div>
                          {step.summary && (
                            <div style={{ fontSize: 12, color: '#64748b', marginTop: 2, lineHeight: 1.6 }}>
                              {step.summary}
                            </div>
                          )}
                          <StepDetail
                            step={step}
                            t={t}
                            onEntityClick={openEntityDrawer}
                            onRecallClick={openRagRecallDrawer}
                            references={session.references}
                            onOpenReference={onOpenReference}
                          />
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )
        );
      })()}

      {/* Answer */}
      {(responseBody || running) && (
        <div className={`${styles.answerBlock} ${activeCitationIndex !== null ? styles.answerBlockSplit : ''}`}>
          {/* 左栏：标题行 + 正文 + 流式 loading（角标激活时与右栏「引用详情」并排） */}
          <div className={styles.answerMain}>
            <div
              style={{
                fontSize: 13,
                fontWeight: 600,
                color: '#6366f1',
                marginBottom: 10,
                display: 'flex',
                alignItems: 'center',
                gap: 6,
              }}
            >
              <BulbOutlined />
              {t('knowledgeSearch.searchResult')}
              <span style={{ fontWeight: 400, fontSize: 12, color: '#94a3b8' }}>
                {t('knowledgeSearch.knowledgeAnalysis', { domain: domainName })}
              </span>
            </div>
            <div style={{ fontSize: 14, color: '#334155', lineHeight: 1.8 }}>
              <ReactMarkdown
                rehypePlugins={[rehypeCitationBadge]}
                components={{
                  h1: ({ children }) => (
                    <h1 style={{ fontSize: 20, lineHeight: 1.45, margin: '12px 0 8px', color: '#0f172a' }}>
                      {children}
                    </h1>
                  ),
                  h2: ({ children }) => (
                    <h2 style={{ fontSize: 18, lineHeight: 1.5, margin: '12px 0 8px', color: '#0f172a' }}>
                      {children}
                    </h2>
                  ),
                  h3: ({ children }) => (
                    <h3 style={{ fontSize: 16, lineHeight: 1.5, margin: '10px 0 6px', color: '#0f172a' }}>
                      {children}
                    </h3>
                  ),
                  p: ({ children }) => <p style={{ margin: '0 0 10px' }}>{children}</p>,
                  ul: ({ children }) => <ul style={{ margin: '0 0 10px', paddingLeft: 20 }}>{children}</ul>,
                  ol: ({ children }) => <ol style={{ margin: '0 0 10px', paddingLeft: 20 }}>{children}</ol>,
                  li: ({ children }) => <li style={{ marginBottom: 4 }}>{children}</li>,
                  strong: ({ children }) => <strong style={{ color: '#0f172a' }}>{children}</strong>,
                  // [jonex] 引用编号 [n] 渲染为可点击角标：点击 → 右栏展开「引用详情」
                  // （对齐原型 openDomainSearchCitation）。编号越界 / 引用未回填时降级为
                  // 不可点，保持改动前的纯展示形态（圆形灰底角标、去方括号、缩小偏上）。
                  sup: ({ node, children }) => {
                    const props = (node as { properties?: Record<string, unknown> })?.properties ?? {};
                    const cls = props.className;
                    const isCite = Array.isArray(cls) ? cls.includes(CITE_BADGE_CLASS) : cls === CITE_BADGE_CLASS;
                    if (!isCite) return <sup>{children}</sup>;

                    const citeIndex = parseCiteIndex(props['data-cite-index'] ?? children);
                    // 契约：答案第 n 个角标 ⇒ references[n - 1]（后端 normalize_citations 重排）
                    if (citeIndex == null || citeIndex > referenceCount) {
                      return (
                        <sup className={styles.badgeSup}>
                          <span className={styles.badgeStatic}>{children}</span>
                        </sup>
                      );
                    }

                    const active = activeCitationIndex === citeIndex - 1;
                    return (
                      <sup className={styles.badgeSup}>
                        <button
                          type="button"
                          className={`${styles.badge} ${active ? styles.badgeActive : ''}`}
                          aria-label={t('knowledgeSearch.citationBadgeAria', { index: citeIndex })}
                          aria-current={active ? 'true' : undefined}
                          onClick={(e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            // 再点一次同一角标收起面板
                            setActiveCitationIndex(active ? null : citeIndex - 1);
                          }}
                        >
                          {children}
                        </button>
                      </sup>
                    );
                  },
                }}
              >
                {responseBody || (running ? t('knowledgeSearch.analyzing') : '')}
              </ReactMarkdown>
            </div>
            {running && hasContent && (
              <div style={{ marginTop: 8 }}>
                <Spin size="small" />
              </div>
            )}
          </div>

          {/* 右栏：引用详情（角标激活时出现；对齐原型 domainSearchCitationDetail） */}
          {activeCitationIndex !== null && session.references?.[activeCitationIndex] && (
            <CitationDetailPanel
              index={activeCitationIndex}
              total={referenceCount}
              reference={session.references[activeCitationIndex]}
              onPrev={() =>
                setActiveCitationIndex((prev) => (prev === null ? null : (prev - 1 + referenceCount) % referenceCount))
              }
              onNext={() => setActiveCitationIndex((prev) => (prev === null ? null : (prev + 1) % referenceCount))}
              onClose={() => setActiveCitationIndex(null)}
              onOpenReference={onOpenReference}
            />
          )}

          {session.status === 'done' && (
            <div className={styles.answerFooter}>
              <FeedbackButtons
                activeVote={sessionVote}
                loading={feedbackLoading}
                onVote={onVote}
                dislikeOpen={dislikeOpen}
                voteReason={voteReason}
                voteComment={voteComment}
                onDislikeOpenChange={onDislikeOpenChange}
                onVoteReasonChange={onVoteReasonChange}
                onVoteCommentChange={onVoteCommentChange}
                onDislikeConfirm={onDislikeConfirm}
              />
            </div>
          )}
        </div>
      )}

      {/* [jonex] §image-refs P3-2: 答案相关图片条（answer 下方、References 上方） */}
      {imageAssets.length > 0 && (
        <div
          style={{
            background: '#fdfdff',
            border: '1px solid #e8edf5',
            borderRadius: 10,
            padding: '12px 16px',
            marginBottom: 16,
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              fontSize: 13,
              fontWeight: 600,
              color: '#6366f1',
              marginBottom: 10,
            }}
          >
            <PictureOutlined />
            {t('knowledgeSearch.answerImages', { count: imageAssets.length })}
          </div>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            {imageAssets.map((a) => (
              <div
                key={`${a.docId}-${a.imageIdx}`}
                style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'center', maxWidth: 96 }}
              >
                <ImageAssetThumb asset={a} t={t} onOpen={openImageAsset} />
                {a.description && (
                  <div
                    style={{
                      fontSize: 11,
                      color: '#64748b',
                      textAlign: 'center',
                      lineHeight: 1.4,
                      maxWidth: 96,
                      overflow: 'hidden',
                    }}
                  >
                    {a.description.length > 48 ? `${a.description.slice(0, 48)}…` : a.description}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* References */}
      {(session.references?.length ?? 0) > 0 && (
        <div
          style={{
            background: '#f8fafc',
            borderLeft: '3px solid #3b82f6',
            borderRadius: '0 8px 8px 0',
            padding: '14px 18px',
            marginBottom: 16,
          }}
        >
          <button
            onClick={() => onToggleRefs(session.id)}
            style={{
              width: '100%',
              padding: 0,
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              justifyContent: 'flex-start',
              fontSize: 12,
              color: '#3b82f6',
              fontWeight: 600,
              height: 'auto',
              lineHeight: 'inherit',
              border: 'none',
              background: 'transparent',
              cursor: 'pointer',
            }}
          >
            {refsExpanded ? <CaretDownOutlined /> : <CaretRightOutlined />}
            <FileTextOutlined />
            {t('knowledgeSearch.relatedDocs', { count: session.references!.length })}
          </button>
          {refsExpanded && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 12 }}>
              {session.references!.map((ref: KnowledgeReference, i: number) => {
                const locs = ref.locations || [];
                const tsLoc = locs.find(
                  (l: KnowledgeReferenceLocation) => l.type === 'timestamp' && l.time_start != null,
                );
                const pageLoc = locs.find((l: KnowledgeReferenceLocation) => l.type === 'page' && l.page_no != null);
                // 命中片段正文：与引用详情面板同口径（citationIndex.snippetLocation）
                const snippet = snippetLocation(ref)?.text || '';
                // [jonex] §image-refs P3-2: 本卡片内可直开的图片 location
                // （有 asset_url 才给 location 级打开，无则回落文档级）
                const imageLocs = locs.filter(
                  (l: KnowledgeReferenceLocation) => l.type === 'image' && l.asset_url && l.image_idx != null,
                );
                // 命中片段含块级结构（或本身就是 md 文件）才走 MarkdownContent 保留格式，其余纯文本
                const renderAsMarkdown = isMarkdownSnippet(ref.file_name, snippet);
                return (
                  <div
                    key={`${ref.doc_id}-${i}`}
                    style={{ background: '#fff', border: '1px solid #e8edf5', borderRadius: 8, padding: '10px 12px' }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      {/* [jonex] 引用角标：编号 i+1 与答案内联角标一致
                          （后端 normalize_citations 按 final_references 下标 +1 重排） */}
                      <span
                        style={{
                          display: 'inline-flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          minWidth: 15,
                          height: 15,
                          padding: '0 3px',
                          borderRadius: 999,
                          background: '#e5e7eb',
                          color: '#6b7280',
                          fontSize: 10,
                          fontWeight: 600,
                          lineHeight: '15px',
                          flexShrink: 0,
                        }}
                      >
                        {i + 1}
                      </span>
                      <span
                        style={{
                          fontSize: 11,
                          color: '#64748b',
                          background: '#f1f5f9',
                          borderRadius: 4,
                          padding: '1px 7px',
                          flexShrink: 0,
                        }}
                      >
                        {getMediaLabel(ref.media_type, t)}
                      </span>
                      <span
                        style={{
                          fontSize: 13,
                          fontWeight: 600,
                          color: '#0b2b5c',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                          maxWidth: 360,
                        }}
                        title={ref.file_name}
                      >
                        {ref.file_name}
                      </span>
                      {(ref.kb_name || ref.kb_id) && (
                        <span style={{ fontSize: 12, color: '#64748b', flexShrink: 0 }}>
                          {t('knowledgeSearch.sourceKb', { name: ref.kb_name || ref.kb_id })}
                        </span>
                      )}
                      {tsLoc && (
                        <span style={{ fontSize: 12, color: '#8b5cf6', flexShrink: 0 }}>
                          {formatTimestamp(tsLoc.time_start)}
                          {tsLoc.time_end != null ? ` - ${formatTimestamp(tsLoc.time_end)}` : ''}
                        </span>
                      )}
                      {pageLoc && (
                        <span style={{ fontSize: 12, color: '#64748b', flexShrink: 0 }}>
                          {t('knowledgeSearch.docPage', { page: (pageLoc.page_no ?? 0) + 1 })}
                        </span>
                      )}
                      <a
                        className="yx-table-action"
                        style={{ marginLeft: 'auto', fontSize: 12, flexShrink: 0 }}
                        onClick={() => onOpenReference(ref, preferredLocation(ref))}
                      >
                        {ref.media_type === 'video'
                          ? tsLoc
                            ? t('knowledgeSearch.locatePlay')
                            : t('knowledgeSearch.viewVideo')
                          : ref.media_type === 'audio'
                            ? t('knowledgeSearch.playAudio')
                            : ref.media_type === 'image'
                              ? t('knowledgeSearch.viewImage')
                              : t('knowledgeSearch.viewOriginal')}
                      </a>
                    </div>
                    {/* [jonex] §image-refs P3-2: 卡片内图片缩略图（location 级打开原图） */}
                    {imageLocs.length > 0 && (
                      <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
                        {imageLocs.map((l: KnowledgeReferenceLocation) => (
                          <ImageAssetThumb
                            key={`${ref.doc_id}-${l.image_idx}`}
                            compact
                            asset={{
                              docId: ref.doc_id,
                              imageIdx: l.image_idx!,
                              assetUrl: l.asset_url!,
                              pageNo: l.page_no,
                              description: (l.text || '').replace(/^图片描述：/, '').trim(),
                            }}
                            t={t}
                            onOpen={openImageAsset}
                          />
                        ))}
                      </div>
                    )}
                    {snippet &&
                      (renderAsMarkdown ? (
                        <div
                          style={{
                            marginTop: 8,
                            background: '#fafcff',
                            borderRadius: 6,
                            padding: '8px 10px',
                            maxHeight: 240,
                            overflow: 'auto',
                          }}
                        >
                          <MarkdownContent content={snippet} />
                        </div>
                      ) : (
                        <div
                          style={{
                            marginTop: 8,
                            fontSize: 13,
                            color: '#475569',
                            lineHeight: 1.7,
                            background: '#fafcff',
                            borderRadius: 6,
                            padding: '8px 10px',
                            maxHeight: 120,
                            overflow: 'auto',
                            whiteSpace: 'pre-wrap',
                          }}
                        >
                          {snippet}
                        </div>
                      ))}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Searching spinner */}
      {running && !hasContent && (
        <div style={{ textAlign: 'center', padding: '30px 0' }}>
          <Spin size="default" />
          <div style={{ marginTop: 10, color: '#94a3b8', fontSize: 13 }}>{t('knowledgeSearch.searchingKnowledge')}</div>
        </div>
      )}

      <OntologyEntityDrawer
        open={entityDrawerOpen}
        loading={false}
        entity={drawerEntity}
        attributeEntries={entityAttributeEntries}
        onClose={() => setEntityDrawerOpen(false)}
      />

      <RagRecallDrawer
        open={ragRecallOpen}
        loading={chunkLoading}
        item={ragRecallItem}
        rank={ragRecallRank}
        score={ragRecallScore}
        onClose={() => setRagRecallOpen(false)}
      />
    </Card>
  );
}
