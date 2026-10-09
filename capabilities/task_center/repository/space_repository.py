#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""空间归属只读探针 —— 跨 schema 查 `knowledge_base.spaces`。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §四（「`space_id`
也要校验归属」）。

任务中心并不拥有「空间」这个对象：它只把 `space_id` 当外部身份收着（§3.5）。
但 §四 明确要求「凡是接口里带 `space_id` 的，都要校验**这个空间确实属于当前租户**」，
而空间的权威归属在知识库 schema 的 `knowledge_base.spaces` 里。所以这里破一次
「能力只碰自己 schema」的分层，**只读**跨过去看一眼——写路径一概不碰。

为什么不走知识库的能力接口（`get_space`）：那个接口要 `user_id` 做空间成员过滤
（`knowledge_base/capability.py`），任务中心手上只有租户、没有用户身份，够不着。

为什么不用 ORM 映射那张表：它不属于本 capability，没有本侧的迁移与模型来保证列不漂移；
用 `sa.text()` 原始 SQL 把要用的两列显式写出来，反而更稳。

⚠️ 同 `capabilities/platform/repository/mcp_service_repository.py` 的做法：跨 schema
只读必须走 `sqlalchemy.text()` 原始 SQL，不 import 别的 capability 的 ORM 模型。
"""

import logging

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.tenant import require_tenant

logger = logging.getLogger(__name__)


class SpaceRepository:
    """空间归属查询 —— 不继承 BaseRepository（跨 schema 只读查询不适合通用基类）。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def exists_for_tenant(self, tenant_id: str, space_id: str) -> bool:
        """这个空间是不是**当前租户名下**的、且没被删。

        已软删的空间按「不属于」处理——空间删了就不该再往里面建触发器、起任务。
        租户 / 空间任一为空都直接判否（`require_tenant` 会拦掉默认租户与非法字符）。
        """
        if not tenant_id or not space_id:
            return False
        tenant_id = require_tenant(tenant_id)
        row = await self.session.execute(
            sa_text(
                """
                SELECT 1
                FROM knowledge_base.spaces
                WHERE id = :space_id
                  AND tenant_id = :tenant_id
                  AND is_deleted = 0
                LIMIT 1
                """
            ),
            {"space_id": space_id, "tenant_id": tenant_id},
        )
        return row.first() is not None


__all__ = ["SpaceRepository"]
