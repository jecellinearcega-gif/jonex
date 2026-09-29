"""
平台管理能力 (business.platform.v1)

作为独立容器运行，提供：
- 认证业务逻辑（login/refresh/ticket，由 Sidecar 代理调用）
- 平台管理 CRUD（用户/角色/权限/菜单/应用/配置/审计/任务）
"""
import logging

from jonex_core.capability import BaseCapability
from jonex_core.capability.models import (
    CapabilityMetadata,
    CapabilityRequest,
    CapabilityResponse,
    CapabilityType,
)
from jonex_core.common.config import get_config
from jonex_core.common.security_guards import assert_secure_jwt_secret

logger = logging.getLogger(__name__)


def _assert_keycloak_client_secret_configured(config) -> None:
    """生产环境守卫：KEYCLOAK_CLIENT_SECRET 必须已从默认哨兵值覆盖。

    生产漏配时平台能力会静默使用公开可知的哨兵字符串 "change-me-in-production"
    作为 Keycloak confidential client secret，攻击者可据此伪造 client 身份。
    fail-closed：生产未正确配置则抛 RuntimeError，拒绝启动。
    """
    if not config.is_production:
        return
    # SSO 未启用的部署不消费该密钥（全仓运行时唯一读取者即本守卫），
    # 不得让与 SSO 无关的部署因哨兵值而启动失败。
    if not config.sso_enabled:
        return
    client_secret = config.KEYCLOAK_CLIENT_SECRET
    if not client_secret or client_secret == "change-me-in-production":
        raise RuntimeError(
            "KEYCLOAK_CLIENT_SECRET 未配置或仍为默认哨兵值。"
            "生产环境必须在 .env 或环境变量中设置 KEYCLOAK_CLIENT_SECRET 为真实随机 secret，"
            "否则 Keycloak confidential client secret 可被攻击者猜到。"
        )


class PlatformCapability(BaseCapability):
    """平台管理能力 — 独立容器"""

    def __init__(self):
        super().__init__()

    def _build_metadata(self) -> CapabilityMetadata:
        return CapabilityMetadata(
            capability_id="platform",
            capability_name="平台管理能力",
            capability_type=CapabilityType.BUSINESS,
            version="v1",
            description="用户认证、RBAC、菜单、应用、系统配置、审计日志、任务调度",
            author="jonex",
        )

    async def validate_input(self, request: CapabilityRequest) -> bool:
        return True

    async def execute(self, request: CapabilityRequest) -> CapabilityResponse:
        return CapabilityResponse.ok(data={"message": "platform capability"})

    async def initialize(self) -> None:
        """启动审计日志 sink 后台任务 + 自动同步调度器"""
        # 安全守卫：生产环境 KEYCLOAK_CLIENT_SECRET 必须已覆盖默认哨兵值，
        # fail-closed 拒绝启动（RuntimeError 不吞掉）。
        _assert_keycloak_client_secret_configured(get_config())
        # 安全守卫：JWT_SECRET 必须为真实随机值（无条件）。平台侧既签发用户 JWT，
        # 又用 _sign_payload 计算 MCP Key 哈希，泄漏即可伪造管理员并批量作废用户 Key。
        assert_secure_jwt_secret(get_config())
        try:
            from capabilities.platform.services.audit_log_sink import get_audit_log_sink
            sink = get_audit_log_sink()
            sink.start()
            logger.info("AuditLogSink 已启动")
        except Exception:
            logger.exception("AuditLogSink 启动失败")

        # 企业身份集成「自动同步」调度器（闹钟 + 电池）：扫到期租户 → CAS 抢占 → 发起同步。
        # ⚠️ **必须 try/except 包住**：`deploy/start_capability.py:98` 是裸的
        # `await capability.initialize()`，抛出即能力起不来 —— 而 platform 是认证关键
        # 服务（登录 / 刷新 / SSO 都走它），**自动同步起不来绝不能拖垮登录**。
        # 失败只丢一行 ERROR，调度器不跑而已（用户可继续用「立即同步」手动触发）。
        try:
            from capabilities.platform.services.identity_sync_scheduler import start_scheduler
            await start_scheduler()
            logger.info("IdentitySyncScheduler 已启动")
        except Exception:
            logger.exception("IdentitySyncScheduler 启动失败（不影响登录等平台功能）")

    async def shutdown(self) -> None:
        """停止审计日志 sink，flush 剩余条目；停自动同步调度器"""
        try:
            from capabilities.platform.services.audit_log_sink import get_audit_log_sink
            sink = get_audit_log_sink()
            await sink.stop()
            logger.info("AuditLogSink 已停止")
        except Exception:
            logger.exception("AuditLogSink 停止失败")

        try:
            from capabilities.platform.services.identity_sync_scheduler import stop_scheduler
            await stop_scheduler()
            logger.info("IdentitySyncScheduler 已停止")
        except Exception:
            logger.exception("IdentitySyncScheduler 停止失败")

    def register_routes(self, app):
        """注册平台管理 REST API 路由"""
        from capabilities.platform.api import create_platform_router

        router = create_platform_router()
        app.include_router(router, prefix="/api/v1")
