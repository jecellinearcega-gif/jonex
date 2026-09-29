#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""建号后补默认角色绑定。

平台自动建号（SSO 首登 JIT / 企业身份同步）此前只写遗留列
``users.role="user"``，从不写 ``platform.user_roles`` 绑定表 ⇒ 新用户零角色、
权限集为空、侧边栏全空，且全程 HTTP 200（静默坏状态，users.role 对该缺陷完全透明）。
本模块把「建号后补默认角色绑定」这一动作收敛到一处。

冻结契约（三条，顺序不可换）：
1. 仅在该用户**当前零角色**时才绑 —— ``UserService.set_roles`` 是 delete-then-insert，
   对已有角色的用户盲目重绑会把它现有的角色抹掉；
2. 租户没有「普通用户」角色时**降级**：warning + 不绑，**不抛异常**
   —— SSO 首登抛异常 = 该用户永远登不进来；同步抛异常 = 一条角色缺失污染整批；
3. 内部调用必须传 ``operator_permissions=None``：无 HTTP 操作者上下文，会跳过
   ``assert_can_assign_roles`` 那道「租户管理员只能由平台管理员指定」的闸门。

本模块刻意不 import ``UserService``（仅 ``TYPE_CHECKING`` 标注），用鸭子类型调用
``user_service`` 的三个入口，避免新增 import 边。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注，运行时不 import，避免与 user_service 成环
    from capabilities.platform.services.user_service import UserService

logger = logging.getLogger(__name__)

#: 租户预设的普通用户角色名 —— 全平台该字面量的**唯一出处**
#: （``services/misc_services.py`` 的租户角色模板引用本常量）。
DEFAULT_USER_ROLE_NAME = "普通用户"


async def ensure_default_role_binding(
    user_service: "UserService",
    tenant_id: str,
    user_id: int,
    source: str,
) -> bool:
    """给「当前零角色」的用户补绑租户默认角色，返回是否真的写入了绑定。

    只经由 ``user_service`` 的三个入口查询/写入（``user_role_repo.get_role_ids`` /
    ``role_repo.get_by_name`` / ``set_roles``），不自建仓储、不直接读 session。

    ``source`` 为调用来源（``oidc_jit`` / ``identity_sync``），只用于日志定位。
    """
    # 1) 已有绑定则不动：set_roles 是 delete-then-insert，重绑会抹掉用户现有角色。
    existing_role_ids = await user_service.user_role_repo.get_role_ids(tenant_id, user_id)
    if existing_role_ids:
        return False

    # 2) 缺默认角色 → 降级：不绑、不抛，仅记 warning（字段须能唯一定位现场）。
    role = await user_service.role_repo.get_by_name(tenant_id, DEFAULT_USER_ROLE_NAME)
    if role is None:
        logger.warning(
            "默认角色缺失，用户建号后未绑定任何角色（建号照常，需补该租户角色种子）: "
            "tenant_id=%s user_id=%s source=%s role_name=%s",
            tenant_id,
            user_id,
            source,
            DEFAULT_USER_ROLE_NAME,
        )
        return False

    # 3) 绑默认角色。operator_permissions=None = 内部调用，跳过租户管理员闸门。
    await user_service.set_roles(
        tenant_id, user_id, [role.id], operator_permissions=None
    )
    return True


__all__ = ["DEFAULT_USER_ROLE_NAME", "ensure_default_role_binding"]
