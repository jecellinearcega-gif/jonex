-- ============================================================
-- Wiki 阅读目录下沉引擎：退休平台库 knowledge_base.wiki_outlines
-- 版本: 033
-- 依据: docs/jonex_wiki/jonex-wiki-outline-sinkdown-plan.md §7 阶段 6
--
-- 目录功能（LLM 起草/调整/存储/自动触发）已整体下沉到 jonex_wiki 引擎，
-- 目录落引擎库 jonex_wiki.wiki_outlines（alembic 0005），平台侧仅保留透传门面
-- （capabilities/knowledge_base/services/wiki_outline_service.py）不再读写本表。
-- 存量数据已被引擎侧重新生成取代（见阶段 5 批量 draft），本迁移 DROP 平台旧表。
-- ============================================================

DROP TABLE IF EXISTS knowledge_base.wiki_outlines CASCADE;
