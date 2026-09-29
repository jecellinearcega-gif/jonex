"""企业身份同步 API（platform 容器内部，经 Gateway REST 透传）。

薄层：extract_tenant_id(request) → require_permission → 调 service，不承载业务规则。
认证：用户 JWT（Sidecar 验签）；权限码：写 platform:tenant:write、
读 platform:tenant:read；无公开端点。
"""

from fastapi import APIRouter, Depends, Query, Request

from jonex_core.common.database import get_db
from jonex_core.common.response import success_response
from jonex_core.common.tenant import extract_tenant_id
from jonex_core.security.permission import require_permission

from capabilities.platform.dtos.identity_integration import SyncRequest
from capabilities.platform.services.identity_integration_sync import (
    IdentityIntegrationSyncService,
)

router = APIRouter()


@router.post("/identity-integration/sync")
async def trigger_sync(
    request: Request,
    req: SyncRequest,
    db=Depends(get_db),
    _p: dict = Depends(require_permission("platform:tenant:write")),
):
    """立即同步（mode=incremental/full）；running 已存在 → 409；未测通 → 5008(409)。

    `req.force=true` = 用户在前端二次确认后「仍要同步」，绕过波 3 的门控。
    """
    tenant_id = extract_tenant_id(request)
    svc = IdentityIntegrationSyncService(db)
    result = await svc.trigger_sync(
        tenant_id,
        req.mode,
        operator_user_id=_p.get("user_id"),
        operator_username=_p.get("username"),
        force=bool(req.force),
    )
    return success_response(data=result.dict())


@router.get("/identity-integration/sync-records")
async def list_sync_records(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db=Depends(get_db),
    _p: dict = Depends(require_permission("platform:tenant:read")),
):
    """同步记录列表（分页）。"""
    tenant_id = extract_tenant_id(request)
    svc = IdentityIntegrationSyncService(db)
    result = await svc.list_sync_records(tenant_id, page, page_size)
    return success_response(data=result.dict())


@router.get("/identity-integration/sync-records/{record_id}")
async def get_sync_record(
    request: Request,
    record_id: int,
    db=Depends(get_db),
    _p: dict = Depends(require_permission("platform:tenant:read")),
):
    """同步记录详情（含失败明细）。"""
    tenant_id = extract_tenant_id(request)
    svc = IdentityIntegrationSyncService(db)
    result = await svc.get_sync_record(tenant_id, record_id)
    return success_response(data=result.dict())
