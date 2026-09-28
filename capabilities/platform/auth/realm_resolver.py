#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Keycloak SSO issuer 反查租户的 async 解析器（带 tenant_realm_map 映射表校验）。

分层约束：本模块位于 platform 层（而非 jonex_core/common），因为反查需要查
``platform.tenant_realm_map`` 表，而 jonex_core 是公共内核、不得依赖 capabilities
的 model。纯函数校验（格式/前缀/require_tenant/host 白名单）仍复用
``jonex_core.common.realm_util``，此处只叠加 DB 映射表命中检查。

安全契约：反查必须「require_tenant + 映射表命中」双通过，任一失败抛
``RealmTenantMismatchError``（3020/403）；映射表 ``status == 1`` 才放行（停用 realm
不放行）。防「格式合法但未开通 Realm / 未登记 issuer_url」的伪造租户。
"""

from sqlalchemy import select

from capabilities.platform.models.tenant_realm_map import TenantRealmMap
from jonex_core.common.exceptions import RealmTenantMismatchError
from jonex_core.common.realm_util import realm_name_for_tenant, tenant_id_from_issuer


async def resolve_tenant_id_from_issuer(session, issuer: str, allowed_hosts=None) -> str:
    """从 issuer URL 反查租户，并校验 tenant_realm_map 映射表命中。

    两步：
    1. 复用纯函数 ``tenant_id_from_issuer`` 完成格式/前缀/require_tenant/host
       白名单校验，得到合法 tenant_id；
    2. 查 ``tenant_realm_map``：``realm == "jonex-" + tenant_id`` 且 ``status == 1``
       命中才返回 tenant_id；未命中（未开通 Realm 或 realm 已停用）→ 抛
       ``RealmTenantMismatchError``。
    """
    tenant_id = tenant_id_from_issuer(issuer, allowed_hosts=allowed_hosts)
    realm = realm_name_for_tenant(tenant_id)

    result = await session.execute(
        select(TenantRealmMap).where(
            TenantRealmMap.realm == realm,
            TenantRealmMap.status == 1,
        )
    )
    if result.scalar_one_or_none() is None:
        raise RealmTenantMismatchError()
    return tenant_id
