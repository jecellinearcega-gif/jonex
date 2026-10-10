/**
 * 文档处理状态 —— 线性 phase 派生（唯一事实来源）
 *
 * 后端两个字段 (status, ontology_status) → 单一线性状态。前端所有展示 / 菜单 / 筛选
 * 都基于此，避免规则散落。与后端 document_service._PHASE_PREDICATE、设计文档
 * docs/document-status-display-design.md §2.2 / §8.1 保持同源同义。
 */

import { isLlmWikiType } from './kbType';

export type DocPhase =
  | 'pending_parse'
  | 'parsing'
  | 'parsed'
  | 'ingesting'
  | 'parse_failed'
  | 'pending_compile'
  | 'compile_paused'
  | 'compiling'
  | 'compiled'
  | 'compile_failed'
  | 'deleting'; // 正交删除态，单独处理，不进主线

/** 徽章图标语义键（由 DocumentStatusBadge 映射到具体 antd 图标）。 */
export type PhaseIcon = 'clock' | 'spinner' | 'check' | 'error' | 'warning' | 'hourglass';

export interface PhaseDisplay {
  label: string;
  color: string;
  icon: PhaseIcon;
  /** 是否已可搜索（待编译起为 true）。 */
  searchable: boolean;
  canReparse: boolean;
  canRecompile: boolean;
  canDelete: boolean;
  /** 副提示，如「已可搜索」。 */
  hint?: string;
}

/**
 * 后端两字段 → 单一线性 phase。传入的是后端原始值（status / ontology_status）。
 * llm-wiki 文档额外传 kbType + llm_wiki_compile_status：其 ontology_status 被强制
 * ready 表示「不需要本体抽取」，不能当「编译完成」——llm-wiki 编译状态以
 * llm_wiki 为准（kbType 显式判断，避免依赖「llmWikiCompileStatus 有值」的隐式约定）。
 * 返回 null 表示列表不展示（如 deleted 软删除）。
 */
export function deriveDocPhase(
  status: string | undefined | null,
  ontologyStatus: string | undefined | null,
  llmWikiCompileStatus?: string | null,
  kbType?: string | null,
  compilationPaused = false,
): DocPhase | null {
  switch (status) {
    case 'pending':
      return 'pending_parse';
    case 'parsing':
      return 'parsing';
    // [jonex] 解析产物已落盘、切片还没推入库（lightrag 三步链第 1 步的收口态）。
    // 只可能出现在 lightrag 库：两种 llm-wiki 库没有 ingest 那一段，解析完直接 ready。
    case 'parsed':
      return 'parsed';
    case 'ingesting':
      return 'ingesting';
    case 'failed':
      return 'parse_failed';
    case 'deleting':
      return 'deleting';
    case 'deleted':
      return null;
    case 'ready':
      // [jonex] llm-wiki 文档：llm_wiki_compile_status 是编译状态唯一事实来源
      // （NULL/compiling/compiled/stale/failed；ontology_status 恒 ready）
      if (isLlmWikiType(kbType)) {
        if (compilationPaused && (!llmWikiCompileStatus || ['compiling', 'stale', 'paused'].includes(llmWikiCompileStatus))) return 'compile_paused';
        switch (llmWikiCompileStatus) {
          case 'compiled':
            return 'compiled';
          case 'paused':
            return 'compile_paused';
          case 'compiling':
            return 'compiling';
          case 'failed':
            return 'compile_failed';
          case 'stale':
            return 'pending_compile'; // 已过期，需重新编译
          default:
            return 'pending_compile'; // NULL：未编译
        }
      }
      switch (ontologyStatus) {
        case 'pending':
          return 'pending_compile';
        case 'extracting':
          return 'compiling';
        case 'ready':
          return 'compiled';
        case 'failed':
          return 'compile_failed';
        default:
          return 'pending_compile';
      }
    default:
      return null;
  }
}

export const PHASE_DISPLAY: Record<DocPhase, PhaseDisplay> = {
  pending_parse: {
    label: 'docPhase.pendingParse',
    color: '#94a3b8',
    icon: 'clock',
    searchable: false,
    canReparse: false,
    canRecompile: false,
    canDelete: false,
  },
  parsing: {
    label: 'docPhase.parsing',
    color: '#3b82f6',
    icon: 'spinner',
    searchable: false,
    canReparse: false,
    canRecompile: false,
    canDelete: false,
  },
  parsed: {
    // 「待抽取」：解析完了但**没入库**，所以和 parsing 一样不可搜、也**没有 hint**
    // （不写「已可搜索」—— 那时候搜不到）。
    // 但两个操作必须放开：这正是它存在的意义。文档停在这个相位**只可能**是因为
    // 任务中心那条任务被停了（或后面那步没派下来），此刻没有后台任务在跑，重新解析
    // 和删除都是安全的；照抄 parsing 把按钮全禁掉，用户就彻底没出路了。
    label: 'docPhase.parsed',
    color: '#8b5cf6',
    icon: 'hourglass',
    searchable: false,
    canReparse: true,
    canRecompile: false, // 还没入库，编译无从谈起
    canDelete: true,
  },
  ingesting: {
    label: 'docPhase.ingesting',
    color: '#3b82f6',
    icon: 'spinner',
    searchable: false,
    canReparse: false,
    canRecompile: false,
    canDelete: false,
  },
  parse_failed: {
    label: 'docPhase.parseFailed',
    color: '#ef4444',
    icon: 'error',
    searchable: false,
    canReparse: true,
    canRecompile: false,
    canDelete: true,
  },
  pending_compile: {
    label: 'docPhase.pendingCompile',
    color: '#64748b',
    icon: 'hourglass',
    searchable: true,
    canReparse: true,
    canRecompile: true,
    canDelete: true,
    hint: 'docPhase.searchable',
  },
  compiling: {
    label: 'docPhase.compiling',
    color: '#3b82f6',
    icon: 'spinner',
    searchable: true,
    canReparse: false,
    canRecompile: false,
    canDelete: true,
    hint: 'docPhase.searchable',
  },
  compile_paused: {
    label: 'docPhase.compilePaused',
    color: '#d97706',
    icon: 'hourglass',
    searchable: false,
    canReparse: false,
    canRecompile: false,
    canDelete: true,
  },
  compiled: {
    label: 'docPhase.compiled',
    color: '#10b981',
    icon: 'check',
    searchable: true,
    canReparse: true,
    canRecompile: true,
    canDelete: true,
  },
  compile_failed: {
    label: 'docPhase.compileFailed',
    color: '#f59e0b',
    icon: 'warning',
    searchable: true,
    canReparse: true,
    canRecompile: true,
    canDelete: true,
    hint: 'docPhase.stillSearchable',
  },
  deleting: {
    label: 'docPhase.deleting',
    color: '#94a3b8',
    icon: 'spinner',
    searchable: false,
    canReparse: false,
    canRecompile: false,
    canDelete: false,
  },
};

/** 列表筛选顺序（不含 deleting；它是正交删除态）。 */
export const FILTER_PHASES: DocPhase[] = [
  'pending_parse',
  'parsing',
  'parsed',
  'ingesting',
  'parse_failed',
  'pending_compile',
  'compiling',
  'compiled',
  'compile_failed',
];
