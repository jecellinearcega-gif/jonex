import apiClient from './request';

/**
 * 知识库空间列表。任务中心的「知识库」筛选项用它拉选项。
 *
 * 与 core-business 的 `getDomainKnowledgeSpaces` 是同一个接口，但**不能跨子应用 import** ——
 * 子应用之间各自独立，共享代码只走 `@jonex/shell-sdk` 与 `@jonex/platform-theme`。
 */

export interface KnowledgeBaseSpace {
  id: string;
  name: string;
}

/**
 * 拉全部知识库空间。`limit: 100` 与 core-business 同口径：该接口不回总数、不做真分页，
 * 下拉一次拉满即可。
 *
 * 不吞异常 —— 调用方要把「拉不到」和「拉到了空」分开处理（前者下拉退化成只有「全部」）。
 */
export async function listKnowledgeBaseSpaces(): Promise<KnowledgeBaseSpace[]> {
  const res = await apiClient.get<{ items: KnowledgeBaseSpace[] }>('/knowledge-base/spaces', {
    params: { limit: 100 },
  });
  return res.items ?? [];
}
