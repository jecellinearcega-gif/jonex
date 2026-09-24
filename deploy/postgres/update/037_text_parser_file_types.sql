-- ============================================================
-- 文本解析器（text_parse）file_types 扩展：TXT/MD → +MARKDOWN/JSON/JSONL
-- 版本: 037
-- 对应全量种子: deploy/postgres/migrations/006_seed_data.sql（parser_configs 的 text_parse 行）
-- 依据: 与上一代 Rag-anything 对照后对齐 —— 其 Parser.TEXT_FORMATS =
--   .txt .md .markdown .json .jsonl（Reference/Rag-anything/raganything/parsers/base.py:51），
--   5 个后缀上一代**全部**走本地 parse_text、不进 MinerU；引擎侧同批已扩。
--
-- 用途：`business_domain.parser_configs.file_types` 是「后缀 → 解析器」的唯一事实
--   来源（capabilities/knowledge_base/services/document_service.py::_resolve_parser_preset，
--   上传与重解析共三处调用）。新后缀若不同步落到本行，上传会**先在平台门被拒**
--   （err.kb.no_parser_for_ext），引擎侧的本地快路径永远收不到文件。
--
-- 幂等：只在当前值仍是旧口径（TXT/MD）时更新；已是新口径、或该行被人工改过，
--   一律不动（可重跑）。fresh 库走 006 全量种子即得新值，本脚本自动空转。
-- ============================================================

UPDATE business_domain.parser_configs
SET file_types = '["TXT","MD","MARKDOWN","JSON","JSONL"]'::jsonb
WHERE id = 'text_parse'
  AND file_types = '["TXT","MD"]'::jsonb;
