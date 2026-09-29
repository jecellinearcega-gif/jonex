#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""企业身份集成配置 API（platform 容器内部，经 Gateway REST 透传 / Sidecar 治理）。

薄层职责：解析请求 → extract_tenant_id → require_permission → 调 service。
业务规则唯一归属 IdentityIntegrationConfigService。

端点：
- GET  /identity-integration            读取统一展示（platform:tenant:read）
- PUT  /identity-integration            更新配置（platform:tenant:write）
- POST /identity-integration/test-connection  测试连接（platform:tenant:write）

body 不接收 tenant_id；租户来自认证上下文 / X-Tenant-ID。
"""
from fastapi import APIRouter, Depends, Request

from jonex_core.common.database import get_db
from jonex_core.common.response import success_response
from jonex_core.common.tenant import extract_tenant_id
from jonex_core.security.permission import require_permission

from capabilities.platform.dtos.identity_integration import (
    IntegrationConfigUpdateRequest,
    TestConnectionRequest,
)
from capabilities.platform.services.identity_integration_config import (
    IdentityIntegrationConfigService,
)

router = APIRouter()


@router.get("/identity-integration")
async def get_identity_integration(
    request: Request,
    db=Depends(get_db),
    _p: dict = Depends(require_permission("platform:tenant:read")),
):
    """查询企业身份集成统一展示（SSO + 主数据源 + 最近同步记录摘要）。"""
    tenant_id = extract_tenant_id(request)
    svc = IdentityIntegrationConfigService(db)
    result = await svc.get_status(tenant_id)
    return success_response(data=result.dict())


@router.get("/identity-integration/sso-realm")
async def get_sso_realm(
    request: Request,
    db=Depends(get_db),
    _p: dict = Depends(require_permission("platform:tenant:read")),
):
    """tenant_realm_map 只读展示（服务地址/Realm/Client ID/回调地址）。"""
    tenant_id = extract_tenant_id(request)
    result = await IdentityIntegrationConfigService(db).get_sso_realm(tenant_id)
    return success_response(data=result.dict())


@router.put("/identity-integration")
async def update_identity_integration(
    request: Request,
    req: IntegrationConfigUpdateRequest,
    db=Depends(get_db),
    _p: dict = Depends(require_permission("platform:tenant:write")),
):
    """更新配置；Token/Secret 未改不传 / 占位 `***` 不覆盖，关键字段变更使验证失效。"""
    tenant_id = extract_tenant_id(request)
    svc = IdentityIntegrationConfigService(db)
    result = await svc.upsert(tenant_id, req, username=_p.get("username"))
    return success_response(data=result.to_dict(), message="企业身份集成配置已更新")


@router.post("/identity-integration/test-connection")
async def test_identity_integration_connection(
    request: Request,
    req: TestConnectionRequest,
    db=Depends(get_db),
    _p: dict = Depends(require_permission("platform:tenant:write")),
):
    """测试连接（target=sso 或 datasource），返回 {ok, latency_ms, message}。"""
    tenant_id = extract_tenant_id(request)
    svc = IdentityIntegrationConfigService(db)
    result = await svc.test_connection(tenant_id, req, username=_p.get("username"))
    return success_response(data=result.dict())
