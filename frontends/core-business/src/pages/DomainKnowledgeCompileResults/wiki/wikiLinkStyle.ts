/**
 * [jonex] 「本页关联」四类关系的**配色与文案**（列表、星形图、列式图谱共用）。
 *
 * 单独成一个模块的原因有两个：
 * 1. 列表与星形图都要用同一套颜色，颜色定义在两个组件里必然走样；
 * 2. 组件文件之间互相 import 容易在大小写不敏感的文件系统上解析串味
 *    （`WikiLinkGroups.tsx` 与 `wikiLinkGroups.ts` 只差大小写），这里放常量最安全。
 */
import type { WikiLinkGroupKind } from './wikiRelations';

/** 关系配色：链接=蓝 / 提及=紫 / 链入=青 / 源文档=绿 */
export const LINK_KIND_COLOR: Record<WikiLinkGroupKind, string> = {
  link: '#3478ee',
  mention: '#7c5cd6',
  backlink: '#0d9488',
  source: '#16a34a',
};

/** 图例文案 key */
export const LINK_KIND_KEY: Record<WikiLinkGroupKind, string> = {
  link: 'domainKnowledge.wikiReader.linkKind.link',
  mention: 'domainKnowledge.wikiReader.linkKind.mention',
  backlink: 'domainKnowledge.wikiReader.linkKind.backlink',
  source: 'domainKnowledge.wikiReader.linkKind.source',
};

/** 列式图谱卡片右上角的徽标：方向必须写清楚（出去 / 回来 / 来源） */
export const LINK_BADGE_KEY: Record<WikiLinkGroupKind, string> = {
  link: 'domainKnowledge.wikiReader.linkBadge.link',
  mention: 'domainKnowledge.wikiReader.linkBadge.mention',
  backlink: 'domainKnowledge.wikiReader.linkBadge.backlink',
  source: 'domainKnowledge.wikiReader.linkBadge.source',
};

export const LINK_KIND_ORDER: WikiLinkGroupKind[] = ['link', 'mention', 'backlink', 'source'];
