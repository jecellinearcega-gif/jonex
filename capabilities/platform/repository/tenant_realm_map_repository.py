"""tenant_realm_map 仓储（租户 ↔ Keycloak Realm 映射表）。

独立于 SSO 反查逻辑（realm_resolver.py 用裸 select），为身份集成配置域提供
按 tenant_id 读取映射行的标准仓储入口。继承 BaseRepository，租户校验遵循
项目统一惯例（require_tenant 拒绝默认/系统租户）。
"""
from typing import Optional

from sqlalchemy import select

from capabilities.platform.models.tenant_realm_map import TenantRealmMap
from capabilities.platform.repository.base import BaseRepository
from jonex_core.common.tenant import require_tenant


class TenantRealmMapRepository(BaseRepository[TenantRealmMap]):
    model = TenantRealmMap

    async def get_by_tenant(self, tenant_id: str) -> Optional[TenantRealmMap]:
        """按 tenant_id 读取映射行；未开通 Realm 时返回 None。"""
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(TenantRealmMap).where(TenantRealmMap.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()
