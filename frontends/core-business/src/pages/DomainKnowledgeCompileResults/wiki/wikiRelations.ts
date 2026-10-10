/**
 * [jonex] 「本页关联」的分组模型（纯函数，可单测）。
 *
 * 同一份数据两处用：
 * - 条目页右栏只有 240px 宽 —— 画图必丑，用**分组列表**（本文件产出的组）；
 * - 图谱 Tab 有空间 —— 画**星形小图**，邻居上限内才画，超限自动退回列表。
 *
 * 分组顺序固定为「最权威 → 最弱」：页间链接（编译期建立）→ 正文提及（阅读侧派生）
 * → 链入本页（反向）→ 源文档（证据）。同一目标只出现一次，按最高优先级归组。
 */
import type { WikiBacklink, WikiPageContent, WikiReaderPage } from '@/types/domainKnowledge';

export type WikiLinkGroupKind = 'link' | 'mention' | 'backlink' | 'source';

export interface WikiLinkItem {
  key: string;
  /** 关系类型：决定颜色与图例 */
  kind: WikiLinkGroupKind;
  /** 目标页面路径（源文档项为 document_id） */
  path: string;
  title: string;
  /** 副标题：分类 / 文档类型 */
  sub?: string;
  /** 关系说明（引擎给的 why_visit 或方向提示） */
  note?: string;
}

export interface WikiLinkGroup {
  kind: WikiLinkGroupKind;
  items: WikiLinkItem[];
}

export interface BuildLinkGroupsInput {
  page: WikiPageContent;
  pagesByPath: Map<string, WikiReaderPage>;
  /** 正文首次提及（与正文蓝链同源） */
  mentionPaths: string[];
  /** 链入本页 */
  backlinks: WikiBacklink[];
  /** 同分类补齐（保证条目不是死胡同） */
  seeAlso?: WikiReaderPage[];
  /**
   * 是否包含「链入本页」。条目页右栏传 false —— 文章正文里已经有一段完整的
   * 「链入本页」小节（带提及次数），右栏再来一遍纯属重复；图谱 Tab 的场景是
   * 关系总览，四类关系都要，所以默认 true。
   */
  includeBacklinks?: boolean;
}

const GROUP_ORDER: WikiLinkGroupKind[] = ['link', 'mention', 'backlink', 'source'];

export function buildLinkGroups(input: BuildLinkGroupsInput): WikiLinkGroup[] {
  const { page, pagesByPath, mentionPaths, backlinks, seeAlso = [], includeBacklinks = true } = input;
  const canonical = page.enginePath || page.path;
  const seen = new Set<string>([canonical]);
  const buckets: Record<WikiLinkGroupKind, WikiLinkItem[]> = {
    link: [],
    mention: [],
    backlink: [],
    source: [],
  };

  const pushPage = (
    kind: WikiLinkGroupKind,
    path: string,
    note?: string,
  ): void => {
    if (!path || seen.has(path)) return;
    const target = pagesByPath.get(path);
    if (!target) return; // 目标不是可见页面（raw/evidence）→ 不列，避免死链
    seen.add(path);
    buckets[kind].push({
      key: `${kind}:${path}`,
      kind,
      path,
      title: target.title || path,
      sub: target.category,
      note,
    });
  };

  // 1) 引擎页间链接（最权威，带 why_visit / 链接类型）
  for (const meta of page.linkMeta || []) pushPage('link', meta.path, meta.whyVisit);
  for (const raw of page.links || []) pushPage('link', `${raw}`.replace(/^\//, ''));
  for (const raw of page.related || []) pushPage('link', `${raw}`.replace(/^\//, ''));
  // 2) 正文提及
  for (const path of mentionPaths) pushPage('mention', path);
  // 3) 链入本页（反向）
  if (includeBacklinks) {
    for (const back of backlinks) {
      pushPage('backlink', back.path, back.title === back.path ? undefined : back.title);
    }
  }
  // 4) 源文档（证据）——点击打开原始文档
  for (const ref of page.references || []) {
    // 缺 document_id 的引用无法打开原文，直接跳过（不让一条脏数据毁掉整页关联）
    if (!ref?.documentId) continue;
    if (seen.has(ref.documentId)) continue;
    const key = `source:${ref.documentId}`;
    seen.add(ref.documentId);
    buckets.source.push({
      key,
      kind: 'source',
      path: ref.documentId,
      title: ref.fileName || ref.documentId.slice(0, 12),
      sub: ref.mimeType,
      note: undefined,
    });
  }
  // 5) 同分类补齐：只在「页间关系一个都没有」时兜底，避免把列表灌满弱关系
  const hasStrong = buckets.link.length + buckets.mention.length + buckets.backlink.length > 0;
  if (!hasStrong) {
    for (const sibling of seeAlso) pushPage('link', sibling.path);
  }

  return GROUP_ORDER.map((kind) => ({ kind, items: buckets[kind] })).filter(
    (group) => group.items.length > 0,
  );
}

/** 分组总数（右栏标题与星图提示共用） */
export function countLinkItems(groups: WikiLinkGroup[]): number {
  return groups.reduce((sum, group) => sum + group.items.length, 0);
}

/**
 * 星形图是否值得画：节点数超过上限时，图会比列表更难读（中文长标题尤其如此），
 * 所以由调用方按同一个上限决定「画图」还是「退回列表」。
 */
export const STAR_MAX_NODES = 10;

export function shouldDrawStar(groups: WikiLinkGroup[]): boolean {
  return countLinkItems(groups) > 0 && countLinkItems(groups) <= STAR_MAX_NODES;
}
