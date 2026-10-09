#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""空间归属守卫（§四）。

任务中心不拥有「空间」，但 §四 硬要求：**凡是接口里带 `space_id` 的**，都得校验
「这个空间确实属于当前租户」——不能拿别的租户的空间 ID 来建定义、建触发器、起任务。

**五处入口**共用这一份实现与这一份文案（`fire` / `trig.create` / `trig.list` /
`def.create` / **`task.list` 的 `space_id` 筛选**），免得同一个校验在几处各写一遍、
过一段时间后各自漂移。

判「不属于」时报**参数非法**，不是「找不到」：§四原文明确要的是「你给的参数不对」，
而不是「你要的东西不存在」。归属查询本身是跨 schema 只读，见 `SpaceRepository`。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.exceptions import InvalidParameterError
from jonex_core.common.i18n import translate
from jonex_core.common.tenant import require_tenant

from ..repository.space_repository import SpaceRepository


async def require_owned_space(
    session: AsyncSession, tenant_id: str, space_id: str
) -> str:
    """校验 `space_id` 是当前租户名下的空间，返回它（便于链式收口）。"""
    tenant_id = require_tenant(tenant_id)
    if not await SpaceRepository(session).exists_for_tenant(tenant_id, space_id):
        raise InvalidParameterError(
            message=translate(
                "err.task_center.space.not_owned",
                params={"space_id": space_id},
                fallback=f"空间 {space_id} 不存在，或者不属于当前租户",
            ),
            details={"space_id": space_id},
        )
    return space_id


__all__ = ["require_owned_space"]
