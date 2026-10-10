# [jonex] Wiki 阅读目录透传门面（目录功能已下沉 jonex_wiki 引擎）
"""Wiki 阅读目录透传门面。

目录的 LLM 起草 / 调整 / 存储 / 自动触发已整体下沉到 jonex_wiki 引擎
（``Reference/JonexWiki`` 的 ``domain/outline`` + ``jonex_wiki.wiki_outlines`` 表）。
平台侧此服务只保留三个透传入口，保持能力 action 契约与前端响应形状不变：

- ``get_view``：读目录 + 页面清单（引擎 ``OutlineService.get_view``）
- ``draft``：LLM 起草（人工「重新生成」，无条件覆盖旧 active）
- ``adjust``：LLM 按自然语言指令调整（覆盖率护栏在引擎侧）

请求体字段仍用平台 DTO 校验（``Draft/AdjustWikiOutlineRequest``），
透传给引擎时丢弃 ``knowledge_base_id`` / ``expected_version`` 等平台专属字段
（plan §5：请求体不带 ``knowledge_base_id``）。
"""
from typing import Optional

from ..dtos.wiki_outline import AdjustWikiOutlineRequest, DraftWikiOutlineRequest
from .document_service import _payload
from .jonex_wiki_service import JonexWikiService


class WikiOutlineService:
    """Wiki 阅读目录透传门面（引擎侧实现，平台侧只透传）。"""

    def __init__(self, *, wiki_service=None):
        # 引擎客户端**延迟构造**：能力模式（remote/mock/local）在运行期解析，
        # 构造期实例化会在 LOCAL 兜底模式下直接抛 ValueError，连累能力容器起不来。
        # 口径与 parse_result_service._wiki_compiler 一致。
        self._wiki_override = wiki_service

    def _wiki(self) -> JonexWikiService:
        return self._wiki_override or JonexWikiService()

    async def get_view(self, tenant_id: str, kb_id: str, document_id: str = "") -> dict:
        """读目录 + 页面清单（透传引擎 ``OutlineService.get_view``）。

        未生成过目录时 ``outline=None``（前端显示「生成目录」引导），但 ``pages`` 一定返回。
        ``document_id`` 非空 = 文档级阅读视图：页面清单按该文档过滤 + 目录过滤投影。
        """
        return await self._wiki().get_outline(
            kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id, document_id=document_id,
        )

    async def draft(
        self, tenant_id: str, kb_id: str,
        request: DraftWikiOutlineRequest | dict,
        *, user_id: Optional[str] = None,
    ) -> dict:
        """LLM 起草目录并落库（透传引擎；人工「重新生成」无条件覆盖旧 active）。"""
        req = DraftWikiOutlineRequest(**_payload(request))
        return await self._wiki().draft_outline(
            kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id,
            request={"model": req.model, "extra_instruction": req.extra_instruction,
                     "unclassified_title": req.unclassified_title},
            user_id=user_id,
        )

    async def adjust(
        self, tenant_id: str, kb_id: str,
        request: AdjustWikiOutlineRequest | dict,
        *, user_id: Optional[str] = None,
    ) -> dict:
        """按自然语言指令调整现有目录（透传引擎；覆盖率护栏在引擎侧）。"""
        req = AdjustWikiOutlineRequest(**_payload(request))
        return await self._wiki().adjust_outline(
            kb_name=kb_id, tenant_id=tenant_id, kb_id=kb_id,
            request={"instruction": req.instruction, "model": req.model,
                     "unclassified_title": req.unclassified_title},
            user_id=user_id,
        )
