#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Keycloak SSO Realm ↔ 租户转换工具（纯函数，无 DB、无 IO）。

Realm 命名规则：realm name = "jonex-" + tenant_id，
tenant_id 保留原样（含大小写）；issuer URL 形如 https://sso.example.com/realms/{realm}。
认证时 tenant_id 不从 token claim 读取，而是从 Realm 名 / issuer URL 反查。
"""

from urllib.parse import urlparse

from jonex_core.common.exceptions import RealmTenantMismatchError, TenantIsolationError
from jonex_core.common.tenant import require_tenant

_REALM_PREFIX = "jonex-"
_REALMS_PATH_MARKER = "/realms/"


def realm_name_for_tenant(tenant_id: str) -> str:
    """返回该租户对应的 Keycloak Realm 名（先校验 tenant_id 合法性）。"""
    return _REALM_PREFIX + require_tenant(tenant_id)


def tenant_id_from_realm(realm: str) -> str:
    """从 Realm 名反查租户；Realm 必须带 jonex- 前缀且剥前缀后为合法业务租户。"""
    if not isinstance(realm, str) or not realm.startswith(_REALM_PREFIX):
        raise RealmTenantMismatchError()
    tenant_id = realm[len(_REALM_PREFIX):]
    try:
        return require_tenant(tenant_id)
    except TenantIsolationError as exc:
        raise RealmTenantMismatchError(cause=exc) from exc


def tenant_id_from_issuer(issuer_url: str, allowed_hosts=None) -> str:
    """从 issuer URL 路径的 /realms/{realm} 段反查租户。

    allowed_hosts 非空时，issuer 的 hostname 必须命中该集合，否则抛
    RealmTenantMismatchError（防任意 host 伪造 /realms/jonex-{租户} 映射到合法租户）。
    缺省 None/空时保持原行为（纯函数向后兼容，不 import config 避免循环依赖）。
    """
    try:
        parsed = urlparse(issuer_url)
    except ValueError as exc:
        raise RealmTenantMismatchError(cause=exc) from exc

    if allowed_hosts and parsed.hostname not in allowed_hosts:
        raise RealmTenantMismatchError()

    path = parsed.path
    marker_index = path.find(_REALMS_PATH_MARKER)
    if marker_index == -1:
        raise RealmTenantMismatchError()
    realm = path[marker_index + len(_REALMS_PATH_MARKER):].split("/", 1)[0]
    if not realm:
        raise RealmTenantMismatchError()
    return tenant_id_from_realm(realm)
