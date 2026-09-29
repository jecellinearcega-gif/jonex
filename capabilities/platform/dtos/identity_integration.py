"""企业身份集成 DTO（数据契约层）。

响应字段名对齐接口契约（单一契约源，前端 TS 与后端 DTO 共同参照）：
- auto_sync       = auto_sync_enabled
- interval        = sync_interval
- last_verified_at = sso_connection_verified_at / datasource_connection_verified_at
- reason          = failure_reason
- 同步记录列表 user_change 为聚合标量（added + updated + disabled 之和）
- 同步记录详情 user_change 为对象 {added, updated, disabled}（同名不同型，按端点区分）

token 字段回显恒为 `***`（脱敏），明文不出现在响应与日志。
client_secret 由部署侧注入，不进本契约（见 SsoConfigResponse）。
"""
import re
from datetime import datetime
from typing import Optional

try:
    from pydantic.v1 import BaseModel, Field, validator
except ImportError:
    from pydantic import BaseModel, Field, validator

from capabilities.platform.models.identity_integration import (
    LoginPolicy,
    SyncEntityType,
    SyncTaskStatus,
    SyncTrigger,
    SyncType,
)

# 脱敏占位：token 回显恒为 ***
SECRET_MASK = "***"

_LOGIN_POLICIES = {e.value for e in LoginPolicy}
_SYNC_TYPES = {e.value for e in SyncType}
_SYNC_STATUSES = {e.value for e in SyncTaskStatus}
_SYNC_TRIGGERS = {e.value for e in SyncTrigger}
_ENTITY_TYPES = {e.value for e in SyncEntityType}
_CONNECTION_STATUSES = {"unconfigured", "verified", "unverified"}


# ============ 统一展示 ============


class SsoStatus(BaseModel):
    """响应 sso 分区。"""

    enabled: bool = False
    provider: str = "keycloak"  # 一期固定 keycloak
    login_policy: str = LoginPolicy.AUTO_CREATE.value
    last_verified_at: Optional[datetime] = None

    @validator("login_policy")
    def _v_login_policy(cls, v):
        if v not in _LOGIN_POLICIES:
            raise ValueError(f"非法登录策略: {v}")
        return v


class DatasourceStatus(BaseModel):
    """响应 datasource 分区（展示态，非配置存储态）。

    ``connection_status`` 为派生字段（按 datasource_url + 验证时间推导），
    非存储枚举、亦非可空：未配置数据源时恒为 ``unconfigured``。
    """

    name: Optional[str] = None
    connection_status: str = "unconfigured"
    auto_sync: bool = False
    interval: Optional[int] = None
    last_sync_at: Optional[datetime] = None
    next_sync_at: Optional[datetime] = None

    @validator("connection_status")
    def _v_connection_status(cls, v):
        if v not in _CONNECTION_STATUSES:
            raise ValueError(f"非法数据源连接状态: {v}")
        return v


class SyncRecordSummary(BaseModel):
    """最近同步记录摘要 / 同步记录列表项（user_change 为聚合标量）。

    `id` 为同步任务主键（= 详情端点的 record_id），
    供前端列表行「查看详情」直接定位，无需再经返回的 task_id。
    """

    id: int
    sync_time: Optional[datetime] = None
    status: str = SyncTaskStatus.PENDING.value
    user_change: int = 0
    failed_count: int = 0
    trigger: str = SyncTrigger.MANUAL.value

    @validator("status")
    def _v_status(cls, v):
        if v not in _SYNC_STATUSES:
            raise ValueError(f"非法同步状态: {v}")
        return v

    @validator("trigger")
    def _v_trigger(cls, v):
        if v not in _SYNC_TRIGGERS:
            raise ValueError(f"非法触发方式: {v}")
        return v


class IntegrationStatusResponse(BaseModel):
    """GET /identity-integration 响应。"""

    sso: SsoStatus
    datasource: DatasourceStatus
    latest_sync_record: Optional[SyncRecordSummary] = None


# ============ 配置更新 / 回显 ============


class SsoConfigUpdate(BaseModel):
    """请求 sso 子配置（未传字段保持原值）。"""

    enabled: Optional[bool] = None
    login_policy: Optional[str] = None
    # 租户级 kc_idp_hint alias 覆盖值（Keycloak 跳过中间授权页）：
    # None = 未传（保持原值）；"" = 清空覆盖（回落平台默认值）；非空 = 覆盖值。
    idp_hint_alias: Optional[str] = None
    # 「单点登录 → 服务地址」= tenant_realm_map.issuer_url（唯一可编辑的高级配置项）：
    # None = 未传（保持原值）。合法性（scheme / 主机名 / 空串）**不在 DTO 层判** ——
    # 由 service 调 validate_sso_url 统一抛 InvalidTargetUrlError(1007)，与数据源
    # 接口地址同档；空串若在此拦下会变成 422，与契约要求的 1007 不一致。
    issuer_url: Optional[str] = None

    @validator("login_policy")
    def _v_login_policy(cls, v):
        if v is None:
            return v
        if v not in _LOGIN_POLICIES:
            raise ValueError(f"非法登录策略: {v}")
        return v

    @validator("idp_hint_alias")
    def _v_idp_hint_alias(cls, v):
        """白名单字符集：对齐 Keycloak alias 实际约束 + 列宽 VARCHAR(64)。"""
        if v is None or v == "":
            return v
        if not re.fullmatch(r"[a-zA-Z0-9._-]{1,64}", v):
            raise ValueError(
                "idp_hint_alias 只允许字母/数字/点/下划线/连字符，长度 1-64"
            )
        return v

    @validator("issuer_url")
    def _v_issuer_url(cls, v):
        """只做列宽守护，对齐 tenant_realm_map.issuer_url VARCHAR(512)。

        格式校验（scheme / 主机名 / 空串）留给 service 的 validate_sso_url，
        保证非法地址统一是 1007 而不是 DTO 层的 422。
        """
        if v is not None and len(v) > 512:
            raise ValueError("issuer_url 长度不得超过 512")
        return v


class DatasourceConfigUpdate(BaseModel):
    """请求 datasource 子配置（未传字段保持原值）。"""

    name: Optional[str] = None  # = datasource_name
    url: Optional[str] = None  # = datasource_url（http/https 均可，允许私网/回环）
    auth_type: Optional[str] = None
    token: Optional[str] = None  # 未改不传；占位 *** 不覆盖原值
    auto_sync: Optional[bool] = None  # = auto_sync_enabled
    interval: Optional[int] = None  # = sync_interval

    @validator("interval")
    def _v_interval(cls, v):
        if v is not None and v <= 0:
            raise ValueError("interval 必须为正整数")
        return v


class IntegrationConfigUpdateRequest(BaseModel):
    """PUT /identity-integration 请求。"""

    sso: Optional[SsoConfigUpdate] = None
    datasource: Optional[DatasourceConfigUpdate] = None


class SsoConfigResponse(BaseModel):
    """响应 sso 子配置。

    client_secret 由部署侧环境变量 KEYCLOAK_CLIENT_SECRET 注入（平台级单 client
    模型），不落库、不进本契约：前端不管理、后端不回显。
    """

    enabled: bool = False
    login_policy: str = LoginPolicy.AUTO_CREATE.value
    last_verified_at: Optional[datetime] = None
    # 租户级覆盖值；None = 无覆盖（生效值 = 平台默认，见 service.get_idp_hint_alias）
    idp_hint_alias: Optional[str] = None


class DatasourceConfigResponse(BaseModel):
    """响应 datasource 子配置（脱敏）。"""

    name: Optional[str] = None  # = datasource_name
    url: Optional[str] = None  # = datasource_url
    auth_type: Optional[str] = None
    token: str = SECRET_MASK  # 恒为 ***，不回显明文
    auto_sync: bool = False
    interval: Optional[int] = None
    last_verified_at: Optional[datetime] = None


class IntegrationConfigResponse(BaseModel):
    """响应（脱敏）。"""

    sso: SsoConfigResponse
    datasource: DatasourceConfigResponse

    def to_dict(self) -> dict:
        """序列化；token 已恒为 ***，不回显明文。"""
        return self.dict()


# ============ SSO Realm 只读展示 ============


class SsoRealmResponse(BaseModel):
    """tenant_realm_map 只读展示（服务地址/Realm/Client ID/回调地址）。

    数据源为 tenant_realm_map（realm/issuer_url/client_id/status）+ 配置项
    KEYCLOAK_REDIRECT_URI（回调地址）；``realm`` 取 DB 权威列 ``row.realm``
    （不再由 tenant_id 推导 —— 登录链路的 realm_resolver 比对的就是这一列）。
    """

    realm: str
    issuer_url: Optional[str] = None
    client_id: Optional[str] = None
    redirect_uri: Optional[str] = None
    status: int = 1
    # 「测试连接」实际会探测的地址（完整 probe_url，含 `/.well-known/openid-configuration`
    # 后缀）——**不是基址**，供前端展示「点测试连接会打到哪儿」。
    # 与 `issuer_url` 不是一回事：后者是面向浏览器的 OIDC issuer 标识（须与 Keycloak
    # 签发的 `iss` 逐字一致），而探测是容器内出站，跨网络部署下二者不可兼得（方案 C）。
    # 三级解析（白名单反查 → KEYCLOAK_SERVER_URL 补 realm 段 → issuer_url）全部落空时为
    # None。三个来源一律产出 issuer 形态（**含 `/realms/{realm}` 段**）：实测
    # `{服务器根}/.well-known/openid-configuration` → 404，realm 路径同端点 → 200，
    # 削掉 realm 段会让测试连接误报 5005。
    effective_probe_target: Optional[str] = None


# ============ 测试连接 ============


class TestConnectionRequest(BaseModel):
    """POST /test-connection 请求（target + 目标配置快照）。

    仅 datasource 支持快照（datasource_url 为用户可填可存的配置，先测再存）；
    SSO 的 issuer_url 来自 tenant_realm_map（Realm 预开通），无配置快照语义，
    故不暴露 sso 快照字段。
    """

    target: str = "datasource"
    datasource: Optional[DatasourceConfigUpdate] = None

    @validator("target")
    def _v_target(cls, v):
        if v not in {"sso", "datasource"}:
            raise ValueError("target 必须为 sso 或 datasource")
        return v


class TestConnectionResponse(BaseModel):
    """响应。"""

    ok: bool
    latency_ms: Optional[int] = None
    message: Optional[str] = None


# ============ 立即同步 ============


class SyncRequest(BaseModel):
    """POST /sync 请求。"""

    mode: str = SyncType.INCREMENTAL.value
    #: 波 3 门控：主数据源未通过连接测试时，手动同步需二次确认后置 `true` 才放行
    #: （`None` = 未传 = 不绕过）。自动同步路径**不传**它，因此恒被闸门拦下。
    force: Optional[bool] = None

    @validator("mode")
    def _v_mode(cls, v):
        if v not in _SYNC_TYPES:
            raise ValueError(f"非法同步模式: {v}")
        return v


class SyncResponse(BaseModel):
    """响应（同步执行完成，返回时任务已到终态）。

    ``/sync`` 在同一个请求内「创建任务 → 执行 → 终态」，一期没有后台 worker：
    返回时 ``status`` 已是 success / partial_failed / failed，不是待轮询的中间态。
    """

    task_id: int
    status: str


# ============ 同步记录列表 ============


class SyncRecordListResponse(BaseModel):
    """GET /sync-records 响应（分页）。"""

    items: list[SyncRecordSummary] = Field(default_factory=list)
    total: int = 0


# ============ 同步记录详情 ============


class UserChangeCounts(BaseModel):
    """详情 user_change 对象（同名不同型，区别于列表聚合标量）。"""

    added: int = 0
    updated: int = 0
    disabled: int = 0
    enabled: int = 0
    deleted: int = 0


class SyncFailureDetail(BaseModel):
    """失败明细。"""

    entity_type: str = SyncEntityType.USER.value
    external_entity_id: Optional[str] = None
    reason: Optional[str] = None  # = failure_reason（脱敏后）

    @validator("entity_type")
    def _v_entity_type(cls, v):
        if v not in _ENTITY_TYPES:
            raise ValueError(f"非法实体类型: {v}")
        return v


class SyncRecordDetailResponse(BaseModel):
    """GET /sync-records/{record_id} 响应。"""

    sync_time: Optional[datetime] = None
    sync_type: str = SyncType.INCREMENTAL.value
    status: str = SyncTaskStatus.PENDING.value
    failed_count: int = 0
    user_change: UserChangeCounts = Field(default_factory=UserChangeCounts)
    failures: list[SyncFailureDetail] = Field(default_factory=list)

    @validator("sync_type")
    def _v_sync_type(cls, v):
        if v not in _SYNC_TYPES:
            raise ValueError(f"非法同步模式: {v}")
        return v

    @validator("status")
    def _v_status(cls, v):
        if v not in _SYNC_STATUSES:
            raise ValueError(f"非法同步状态: {v}")
        return v


__all__ = [
    "SECRET_MASK",
    "SsoStatus",
    "DatasourceStatus",
    "SyncRecordSummary",
    "IntegrationStatusResponse",
    "SsoConfigUpdate",
    "DatasourceConfigUpdate",
    "IntegrationConfigUpdateRequest",
    "SsoConfigResponse",
    "DatasourceConfigResponse",
    "IntegrationConfigResponse",
    "SsoRealmResponse",
    "TestConnectionRequest",
    "TestConnectionResponse",
    "SyncRequest",
    "SyncResponse",
    "SyncRecordListResponse",
    "UserChangeCounts",
    "SyncFailureDetail",
    "SyncRecordDetailResponse",
]
