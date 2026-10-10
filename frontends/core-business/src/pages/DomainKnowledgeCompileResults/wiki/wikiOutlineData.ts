/**
 * [jonex] 「主题地图」= 目录树的数据构建（纯函数，可单测）。
 *
 * 目录本来就是**一棵有顺序的树**（主题 → 小节 → 条目），这正是「像课本」的价值所在。
 * 之前把它当网络画（共享标签连边 + 力导向）是形态错配：真实产物里主题间共享边
 * 只有 0–6 条、且多来自 `navigation-only` 这类结构标签，画布上只剩几个孤立圆点。
 * 所以主题地图回归树的形态：**顺序与层级都在**，可折叠。
 *
 * 这里只负责把「目录 + 页面清单」变成树（含折叠）。当前唯一渲染方是列式图谱
 * （`WikiColumnBoard`）；知识库级/文档级的「主题地图」卡片已随该 tab 一并下线，
 * 因为它的信息与阅读界面左侧目录栏完全重复。
 */
import type { WikiReaderPage } from '@/types/domainKnowledge';

/** 目录的**最小结构**（真实目录节点比这更宽，这里只依赖渲染需要的字段） */
export interface OutlineNodeLike {
  id: string;
  title: string;
  entries?: { path: string; title?: string }[];
  children?: OutlineNodeLike[];
}

export interface OutlineLike {
  topics?: OutlineNodeLike[];
}

export type WikiTreeNodeKind = 'root' | 'topic' | 'section' | 'page';

export interface WikiTreeNode {
  id: string;
  label: string;
  kind: WikiTreeNodeKind;
  /** 该分支下的页面数（折叠时也显示，读者不必展开就知道有多少内容） */
  count?: number;
  /** 页面叶子才有：canonical 引擎路径 */
  path?: string;
  /** 主题/小节才有：点击时用于「跳到该主题」 */
  topicId?: string;
  /** 是否还有未展开的子节点（用于画 ▸ / ▾） */
  collapsible?: boolean;
  expanded?: boolean;
  children?: WikiTreeNode[];
}

export interface BuildOutlineTreeOptions {
  /** LLM 目录（可能为 null：还没生成目录） */
  outline?: OutlineLike | null;
  pages: WikiReaderPage[];
  kbName?: string;
  /** 已展开的节点 id 集合 */
  expanded: ReadonlySet<string>;
  /** 紧凑模式（首页小图）：不展开页面叶子，只显示每个分组的页面数 */
  compact?: boolean;
  /** 根节点 id（默认 'root'） */
  rootId?: string;
}

export const ROOT_ID = 'root';
export const UNCLASSIFIED_TITLE = '未归类';

interface Counted {
  node: WikiTreeNode;
  paths: string[];
}

/** 目录主题 → 树节点（递归 children），并统计每个分组的页面数 */
function fromTopic(
  topic: OutlineNodeLike,
  depth: number,
  expanded: ReadonlySet<string>,
  compact: boolean,
): Counted {
  const children: WikiTreeNode[] = [];
  const paths: string[] = [];

  for (const entry of topic.entries || []) {
    paths.push(entry.path);
    if (!compact) {
      children.push({
        id: `page:${entry.path}`,
        label: entry.title || entry.path,
        kind: 'page',
        path: entry.path,
      });
    }
  }
  for (const child of topic.children || []) {
    const counted = fromTopic(child, depth + 1, expanded, compact);
    children.push(counted.node);
    paths.push(...counted.paths);
  }

  const isOpen = expanded.has(topic.id);
  const node: WikiTreeNode = {
    id: topic.id,
    label: topic.title,
    kind: depth === 1 ? 'topic' : 'section',
    topicId: topic.id,
    count: paths.length,
    collapsible: children.length > 0,
    expanded: isOpen,
    // 折叠 = 不挂 children（树布局据此只画当前层级）
    children: isOpen ? children : undefined,
  };
  return { node, paths };
}

/** 无目录时的兜底：按引擎分类目录分组（未归类排最后），仍然是一棵能读的树 */
function fallbackTopics(pages: WikiReaderPage[]): OutlineNodeLike[] {
  const buckets = new Map<string, WikiReaderPage[]>();
  for (const page of pages) {
    const category = page.category || 'unclassified';
    const list = buckets.get(category) || [];
    list.push(page);
    buckets.set(category, list);
  }
  return [...buckets.entries()]
    .sort((a, b) => {
      if (a[0] === 'unclassified') return 1;
      if (b[0] === 'unclassified') return -1;
      if (b[1].length !== a[1].length) return b[1].length - a[1].length;
      return a[0].localeCompare(b[0], 'zh-CN');
    })
    .map(([category, list]) => ({
      id: `cat-${category}`,
      title: category === 'unclassified' ? UNCLASSIFIED_TITLE : category,
      entries: list
        .slice()
        .sort((a, b) => (a.title || '').localeCompare(b.title || '', 'zh-CN'))
        .map((p) => ({ path: p.path, title: p.title })),
    }));
}

export function buildOutlineTree(options: BuildOutlineTreeOptions): WikiTreeNode {
  const { outline, pages, kbName, expanded, compact = false, rootId = ROOT_ID } = options;
  const topics = outline?.topics?.length ? outline.topics : fallbackTopics(pages);
  const children: WikiTreeNode[] = [];
  let total = 0;
  for (const topic of topics) {
    const counted = fromTopic(topic, 1, expanded, compact);
    children.push(counted.node);
    total += counted.paths.length;
  }
  return {
    id: rootId,
    label: kbName || '目录',
    kind: 'root',
    count: total,
    collapsible: children.length > 0,
    expanded: expanded.has(rootId),
    children: expanded.has(rootId) ? children : undefined,
  };
}

/** 默认展开集合：根 + 一级主题（小节先收起，读者按需下钻） */
export function defaultExpanded(
  outline: OutlineLike | null | undefined,
  pages: WikiReaderPage[],
  rootId = ROOT_ID,
): Set<string> {
  const ids = new Set<string>([rootId]);
  const topics = outline?.topics?.length ? outline.topics : fallbackTopics(pages);
  topics.forEach((topic) => ids.add(topic.id));
  return ids;
}

/** 全部展开：连同小节一起（页面叶子由 compact 与树构建控制） */
export function allExpanded(
  outline: OutlineLike | null | undefined,
  pages: WikiReaderPage[],
  rootId = ROOT_ID,
): Set<string> {
  const ids = defaultExpanded(outline, pages, rootId);
  const walk = (topics?: OutlineNodeLike[]) => {
    for (const topic of topics || []) {
      ids.add(topic.id);
      walk(topic.children);
    }
  };
  walk(outline?.topics?.length ? outline.topics : fallbackTopics(pages));
  return ids;
}

/** 树 → 缩进列表（g6 加载失败时的降级渲染，仍然可点可读） */
export function flattenTree(
  node: WikiTreeNode,
  depth = 0,
): { node: WikiTreeNode; depth: number }[] {
  const out: { node: WikiTreeNode; depth: number }[] = [{ node, depth }];
  for (const child of node.children || []) out.push(...flattenTree(child, depth + 1));
  return out;
}
