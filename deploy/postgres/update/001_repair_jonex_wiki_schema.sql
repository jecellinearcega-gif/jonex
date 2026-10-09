-- 001: 修复 jonex_wiki KB 的存量 llm_wiki_schemas 行（被 openkb 分支误写）
--
-- 背景：
--   jonex_wiki 编译设置的默认 schema 应为「空实体/概念词表 + 空编译偏好 suffix」
--   （entity_types/concept_types 硬编码 []，agents_md = 编译偏好 suffix 默认空串）。
--   早期创建的 jonex_wiki KB 因 _create_default 未正确按 kb_type 分流，schema 行被
--   写成 openkb 内容：7 项实体词表 + 完整 AGENTS.md（3571 字符）+ config 带
--   entity_types 键。这会让 GET /llm-wiki/schema 返回与 kb_type 不符的内容。
--
-- 判定特征（精确、不误伤）：
--   jonex_wiki 的 entity_types 永远是空数组（_create_default / save_schema 的
--   jonex_wiki 分支均硬编码 []），因此「kb_type=jonex_wiki 但 entity_types 非空」
--   即 = openkb 误写。用户已自定义的非空 suffix（agents_md）不影响判定——正常
--   jonex_wiki 保存流程 entity_types 恒为 []，不会命中本迁移。
--
-- 幂等性：
--   修复后 entity_types=[]，不再命中 WHERE 条件；apply.sh 的 schema_migrations
--   版本表对已应用文件跳过，双重保证可重跑。

UPDATE knowledge_base.llm_wiki_schemas AS lws
SET entity_types = '[]'::jsonb,
    concept_types = '[]'::jsonb,
    agents_md = '',
    agents_md_extra = '',
    config_snapshot = lws.config_snapshot - 'entity_types',
    updated_at = NOW()
FROM knowledge_base.knowledge_info AS ki
WHERE ki.id = lws.knowledge_base_id
  AND ki.kb_type = 'jonex_wiki'
  AND ki.is_deleted = 0
  AND lws.status = 'active'
  AND jsonb_array_length(lws.entity_types) > 0;
