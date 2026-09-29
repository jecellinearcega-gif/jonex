from typing import Optional, Sequence

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from capabilities.platform.models.user import User
from capabilities.platform.repository.base import BaseRepository
from jonex_core.common.tenant import require_tenant


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_username(self, tenant_id: str, username: str) -> Optional[User]:
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(User).where(
                User.tenant_id == tenant_id,
                User.username == username,
                User.is_deleted == 0,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_external_id(self, tenant_id: str, external_id: str) -> Optional[User]:
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(User).where(
                User.tenant_id == tenant_id,
                User.external_id == external_id,
                User.is_deleted == 0,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_upstream_subject(
        self, tenant_id: str, upstream_subject: str
    ) -> Optional[User]:
        """按上游主体标识认人（跨 realm 稳定的主匹配键）。

        与 get_by_external_id 同构：三元组 tenant_id + upstream_subject + is_deleted == 0，
        与 partial index users_tenant_id_upstream_subject_key 的谓词严格对齐
        （否则会留下「索引不管、查询能查到」的缝隙）。
        """
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(User).where(
                User.tenant_id == tenant_id,
                User.upstream_subject == upstream_subject,
                User.is_deleted == 0,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_upstream_subject_including_deleted(
        self, tenant_id: str, upstream_subject: str
    ) -> Optional[User]:
        """按上游主体标识认人，**含已软删行**（全量缺失软删后的「原地复活」靠它）。

        与 `get_by_upstream_subject` 的唯一差别：三元组**去掉** `is_deleted == 0`
        —— 软删行不在任何 partial unique index 的谓词内，靠 `get_by_upstream_subject`
        永远找不到它，复活就会退化成「新建一个同人新 id 的账号」。
        ⚠️ 本查询刻意**不对齐**索引谓词（这正是它的用途），调用方须自行承担
        「同一 upstream_subject 可能同时存在活跃行与软删行」的歧义。
        ⛔ **排序即契约**：``ORDER BY is_deleted, id LIMIT 1`` —— 活跃行优先，
        同状态取最小 id。软删行不在 partial unique index 的谓词内，两行**可以共存**；
        少了这层排序+限量，``scalar_one_or_none()`` 遇到两行会抛
        ``MultipleResultsFound``，把整次同步打成 failed（不只那一个人）。
        裁决语义：活跃行存在 ⇒ 取活跃行（调用方据 ``is_deleted`` 跳过复活、走常规匹配）。
        """
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(User)
            .where(
                User.tenant_id == tenant_id,
                User.upstream_subject == upstream_subject,
            )
            .order_by(User.is_deleted.asc(), User.id.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_pending_backfill(self, tenant_id: str) -> Sequence[User]:
        """扫描待回填账号：`upstream_subject IS NULL` 且未软删的本租户账号。

        只扫 NULL ⇒ **天然不覆盖已有值**（回填幂等的前提）。
        """
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(User).where(
                User.tenant_id == tenant_id,
                User.upstream_subject.is_(None),
                User.is_deleted == 0,
            )
        )
        return result.scalars().all()

    async def list_by_tenant(
        self, tenant_id: str, offset: int = 0, limit: int = 20
    ) -> Sequence[User]:
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(User)
            .where(User.tenant_id == tenant_id, User.is_deleted == 0)
            .order_by(User.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return result.scalars().all()

    async def count_by_tenant(self, tenant_id: str) -> int:
        tenant_id = require_tenant(tenant_id)
        result = await self.session.execute(
            select(func.count()).select_from(User).where(
                User.tenant_id == tenant_id, User.is_deleted == 0
            )
        )
        return result.scalar() or 0
