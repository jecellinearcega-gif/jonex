from typing import Mapping, Optional, Sequence

from sqlalchemy import select

from capabilities.platform.models.tenant import Tenant
from capabilities.platform.repository.base import BaseRepository


class TenantRepository(BaseRepository[Tenant]):
    model = Tenant

    async def get_including_deleted_by_id(self, id_val: str) -> Optional[Tenant]:
        """查指定 id 的租户（含软删除），供创建时复用已软删的同 id 记录。

        软删除只是把 is_deleted 置 1，主键 id 仍占用；创建前若只按未删除口径
        查重（get_by_id_shared）会漏判，insert 撞 tenants_pkey。这里补查软删记录，
        让 create 走「恢复复用」而不是报 500。
        """
        result = await self.session.execute(
            select(Tenant).where(Tenant.id == id_val)
        )
        return result.scalar_one_or_none()

    async def list_active_by_ids(self, tenant_ids: Sequence[str]) -> Mapping[str, Tenant]:
        if not tenant_ids:
            return {}

        result = await self.session.execute(
            select(Tenant).where(
                Tenant.id.in_(tenant_ids),
                Tenant.status == 1,
                Tenant.is_deleted == 0,
            )
        )
        tenants = result.scalars().all()
        return {tenant.id: tenant for tenant in tenants}
