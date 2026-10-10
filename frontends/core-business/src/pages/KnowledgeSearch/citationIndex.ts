import type { KnowledgeReference, KnowledgeReferenceLocation } from '@/types/knowledgeSearch';

/** i18n 翻译函数的最小签名（只取本模块用到的形态） */
type Translator = (key: string, opts?: Record<string, unknown>) => string;

/**
 * hast 节点最小结构 —— 项目未安装 `@types/hast`，只声明本模块用到的字段，
 * 避免为插件测试引入新依赖。
 */
export interface HastNode {
  type: string;
  value?: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
}

/** 引用角标 class，`components.sup` 据此区分「引用角标」与普通上标 */
export const CITE_BADGE_CLASS = 'jonex-cite-badge';

/**
 * [jonex] rehype 插件：react-markdown v10 的 `components` 只映射 hast element（如 p/strong），
 * 纯文本节点不会经过 `components`，所以无法用 `components.text` 拦截引用编号。
 * 这里在 hast 树阶段把正文里的 `[n]`（单个引用）或 `[n-m]`（连续引用范围）拆成
 * `<sup class="jonex-cite-badge" data-cite-index="n">` 元素，交给 `components.sup`
 * 渲染成可点击角标；范围 `[n-m]` 拆成 n、横杠、m 三个节点，使起止编号各自独立成圆标。
 *
 * 放在本模块（而非 SessionCard）是为了让角标切分逻辑脱离 React 依赖、可被单测直接覆盖。
 */
export function rehypeCitationBadge() {
  return (tree: HastNode) => {
    const walk = (node: HastNode) => {
      if (!node || !Array.isArray(node.children)) return;
      const next: HastNode[] = [];
      for (const child of node.children) {
        if (child && child.type === 'text') {
          const value = String(child.value ?? '');
          const parts = value.split(/(\[\d+(?:-\d+)?\])/g);
          if (parts.length <= 1) {
            next.push(child);
            continue;
          }
          for (const part of parts) {
            if (part === '') continue;
            const m = /^\[(\d+)(?:-(\d+))?\]$/.exec(part);
            if (m) {
              // 编号随节点下传（`data-cite-index`），供 `components.sup` 渲染可点击角标；
              // hast 的 data-* 属性按原样键名存放，不做驼峰转换
              const badge = (num: string): HastNode => ({
                type: 'element',
                tagName: 'sup',
                properties: { className: [CITE_BADGE_CLASS], 'data-cite-index': num },
                children: [{ type: 'text', value: num }],
              });
              if (m[2]) {
                // 范围 [n-m]：n、横杠、m 三个节点，起止编号各自独立成圆标
                next.push(badge(m[1]));
                next.push({
                  type: 'element',
                  tagName: 'sup',
                  properties: {},
                  children: [{ type: 'text', value: '-' }],
                });
                next.push(badge(m[2]));
              } else {
                next.push(badge(m[1]));
              }
            } else {
              next.push({ type: 'text', value: part });
            }
          }
        } else {
          walk(child);
          next.push(child);
        }
      }
      node.children = next;
    };
    walk(tree);
  };
}

/** 秒 → mm:ss（与 SessionCard 卡片内时间锚点展示同口径） */
export function formatTimestamp(sec?: number | null): string {
  if (sec == null || Number.isNaN(sec)) return '';
  const s = Math.max(0, Math.floor(sec));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`;
}

/**
 * [jonex] 解析答案内联引用角标的编号（1-based）。
 *
 * 编号来源优先级：rehype 插件下传的 `data-cite-index`（字符串）→ 角标文本子节点。
 * 契约：答案里第 n 个角标 ⇒ `references[n - 1]`（后端 `normalize_citations` /
 * `normalize_evidence_handles` 已按 `final_references` 首现下标 +1 重排）。
 *
 * 非法输入（`'-'`、空串、`'a'`、undefined 等）返回 null，调用方据此把角标降级为纯展示。
 */
export function parseCiteIndex(raw: unknown): number | null {
  const text = Array.isArray(raw) ? raw[0] : raw;
  if (text == null) return null;
  const s = String(text).trim();
  if (!/^\d+$/.test(s)) return null;
  const n = Number(s);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

/**
 * 取引用的「首选定位」——与「关联原文」卡片「查看原文」按钮同口径：
 * 纯图片文档优先可直开的图片 location，其余按 时间锚点 → 页码 → 首个 location 依次回落。
 *
 * 抽成纯函数是为了让内联角标、引用详情面板、卡片按钮三处共用同一套规则，
 * 避免口径漂移（此前该三元表达式内联在卡片 onClick 里）。
 */
export function preferredLocation(ref: KnowledgeReference): KnowledgeReferenceLocation | undefined {
  const locs = ref.locations || [];
  if (locs.length === 0) return undefined;
  if (ref.media_type === 'image') {
    const imageLoc = locs.find((l) => l.type === 'image' && l.asset_url && l.image_idx != null);
    return imageLoc || locs[0];
  }
  return (
    locs.find((l) => l.type === 'timestamp' && l.time_start != null) ||
    locs.find((l) => l.type === 'page' && l.page_no != null) ||
    locs[0]
  );
}

/**
 * 命中片段正文所在的 location —— 规则是「**首个带非空 text 的 location**」。
 *
 * 与「关联原文」卡片的 snippet 同口径。此前该规则内联在卡片里，而引用详情面板另按
 * `preferredLocation(...).text` 取正文，多 location 引用下会分叉：一条引用同时有不带
 * 正文的页码 location 和带正文的表格 location 时，卡片显示正文、面板却显示
 * 「该引用暂无命中片段原文」。抽成同一个函数后两处不可能再漂移。
 *
 * ⚠️ 与 `preferredLocation` 职责不同、**不可互换**：本函数回答「正文在哪」，
 * `preferredLocation` 回答「点『定位原文』该落到哪」（图片文档优先可直开的图片 location）。
 */
export function snippetLocation(ref: KnowledgeReference): KnowledgeReferenceLocation | undefined {
  return (ref.locations || []).find((l) => !!l.text);
}

/** md 扩展名：老口径，不看内容 */
const isMarkdownFile = (fileName: string) => /\.(md|markdown)$/i.test(fileName || '');

/**
 * 块级结构判据。只认行首形态，**刻意不收** `**粗体**` / `` `代码` `` / `> 引用` 这类行内语法：
 * 它们换不来排版收益，却把含 `_`、`*` 的纯文本拖进 markdown 解析器。
 * `MarkdownContent` 没有 `white-space: pre-wrap`，误判的代价是**换行被折成空格**，所以宁窄勿宽。
 *
 * 无 `g` 标志：带 `g` 会让 `lastIndex` 在多次 `test()` 之间残留，判据时真时假。
 */
const MARKDOWN_BLOCK_PATTERNS: RegExp[] = [
  /^ {0,3}#{1,6} /m, // 标题（`#` 后必须跟空格，`#话题` 不算）
  /^ {0,3}\|.*\|\s*$/m, // 表格行（行首行尾都是竖线才认）
  /^ {0,3}[-*+] /m, // 无序列表
  /^ {0,3}\d+\. /m, // 有序列表（只认 `1.` 不认 `1)`：后者在中文正文里太常见）
  /^ {0,3}```/m, // 代码围栏
];

/**
 * [jonex] 命中片段是否该走 `MarkdownContent`。
 *
 * 此前只看扩展名，pdf / office 片段哪怕含 `## 标题` 也只能按纯文本显示，与 md 引用观感割裂；
 * 这里对非 md 文件改按**内容**判断，纯文本一律原样输出。
 *
 * 与 `snippetLocation` 同住本模块：两个展示点（「关联原文」卡片 / 引用详情面板）共用同一份判据。
 */
export function isMarkdownSnippet(fileName: string, text: string): boolean {
  if (isMarkdownFile(fileName)) return true;
  if (!text) return false;
  return MARKDOWN_BLOCK_PATTERNS.some((p) => p.test(text));
}

/**
 * 定位 → 位置文案（引用详情面板「位置」行）。
 *
 * 页码保持既有口径：后端 `page_no` 为 0-based（MinerU page_idx），展示层 +1。
 * 无法产出有效文案（如 page 类型但 `page_no` 为空）返回 null，调用方不渲染该行。
 */
export function describeLocation(loc: KnowledgeReferenceLocation | undefined, t: Translator): string | null {
  if (!loc) return null;
  switch (loc.type) {
    case 'chunk':
      return loc.chunk_index != null ? t('knowledgeSearch.citationChunk', { index: loc.chunk_index }) : null;
    case 'page':
      return loc.page_no != null ? t('knowledgeSearch.docPage', { page: loc.page_no + 1 }) : null;
    case 'timestamp': {
      if (loc.time_start == null) return null;
      const start = formatTimestamp(loc.time_start);
      if (!start) return null;
      const end = loc.time_end != null ? formatTimestamp(loc.time_end) : '';
      return end ? `${start} - ${end}` : start;
    }
    case 'image': {
      if (loc.image_idx == null) return null;
      const image = t('knowledgeSearch.citationImage', { index: loc.image_idx });
      return loc.page_no != null ? `${image} · ${t('knowledgeSearch.docPage', { page: loc.page_no + 1 })}` : image;
    }
    case 'document':
      return t('knowledgeSearch.citationDocument');
    case 'char':
      return t('knowledgeSearch.citationFragment');
    default:
      return null;
  }
}
