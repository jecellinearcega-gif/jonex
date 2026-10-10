import type { KnowledgeBaseType } from '../types/domainKnowledge';

/**
 * [jonex] LLM Wiki 型知识库判定（对照后端 LLM_WIKI_TYPES = {openkb, jonex_wiki}）。
 *
 * 红线：禁止用否定式判 kb_type（如 `kbType !== 'openkb'`）——
 * 三引擎下会把 jonex_wiki 误兜进 lightrag 桶。一律显式三分支或用本 helper。
 */
export function isLlmWikiType(kbType: KnowledgeBaseType | string | undefined | null): boolean {
  return kbType === 'openkb' || kbType === 'jonex_wiki';
}
