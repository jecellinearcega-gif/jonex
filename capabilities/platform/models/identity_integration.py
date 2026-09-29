"""企业身份集成（Enterprise Identity Integration）数据模型。

三张表对应三个聚合/实体：
- IdentityIntegrationConfig  配置聚合根：一个租户一份，聚合 SSO 子配置 + 主数据源子配置。
- IdentitySyncTask          同步任务聚合根：一次「立即同步/自动同步」一条，聚合同步头 + 失败明细。
- IdentitySyncFailure       同步失败明细（弱实体）：归属 sync_task，不单独带 tenant_id。

约束要点：
- 配置表 `(tenant_id)` 唯一（一租户一份）。
- 任务表 `(tenant_id) WHERE status = 'running'` 部分唯一索引（防并发重复 running）。

敏感字段：token_ciphertext 经 Fernet 加密落库（jonex_core/common/crypto.py::encrypt_secret），
回显脱敏为 `***`，明文不入日志。
"""
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    TIMESTAMP,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


class LoginPolicy(str, Enum):
    """首次登录账号策略。"""

    AUTO_CREATE = "auto_create"
    SYNCED_ONLY = "synced_only"


class SyncTaskStatus(str, Enum):
    """同步任务状态（终态：success / partial_failed / failed / canceled）。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"
    CANCELED = "canceled"


class SyncType(str, Enum):
    """同步模式。"""

    INCREMENTAL = "incremental"
    FULL = "full"


class SyncTrigger(str, Enum):
    """同步触发方式。"""

    MANUAL = "manual"
    AUTO = "auto"


class SyncEntityType(str, Enum):
    """失败明细实体类型（一期仅 user，组织后置）。"""

    USER = "user"


class IdentityIntegrationConfig(TenantMixin, TimestampMixin, Base):
    """企业身份集成配置聚合根（一租户一份）。

    SSO 子配置（Realm/issuer_url/client_id 仍归 tenant_realm_map，此处不复制）：
    sso_enabled / login_policy / login_field_mapping / sso_connection_verified_at /
    idp_hint_alias。
    主数据源子配置：datasource_name / datasource_url / auth_type / token_ciphertext /
    auto_sync_enabled / sync_interval / datasource_connection_verified_at。
    """

    __tablename__ = "identity_integration_config"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_identity_integration_config_tenant"),
        {"schema": "platform"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # ---- SSO 子配置 ----
    sso_enabled = Column(Boolean, nullable=False, default=False)
    login_policy = Column(
        String(32), nullable=False, default=LoginPolicy.AUTO_CREATE.value
    )
    # 预留字段：登录字段映射已拍板不做可配置（固定映射硬编码于 auth_service.py），
    # 本列保留仅避免存量库 DDL 变更，当前无消费方。
    login_field_mapping = Column(JSONB)
    sso_connection_verified_at = Column(TIMESTAMP)
    # 租户级 kc_idp_hint alias 覆盖值（Keycloak 跳过中间授权页用）；NULL = 用平台默认值
    # KEYCLOAK_IDP_HINT_DEFAULT_ALIAS，无覆盖值不落库（可空，见 update/036 列注释）。
    idp_hint_alias = Column(String(64))

    # ---- 主数据源子配置 ----
    datasource_name = Column(String(255))
    datasource_url = Column(String(512))
    auth_type = Column(String(32))
    # Fernet 加密落库（密钥 DATA_SOURCE_SECRET_KEY），回显脱敏 ***
    token_ciphertext = Column(String(1024))
    auto_sync_enabled = Column(Boolean, nullable=False, default=False)
    sync_interval = Column(Integer)
    datasource_connection_verified_at = Column(TIMESTAMP)

    # ---- 自动同步调度水位（方案甲，update/039 增量 DDL）----
    # 两列都由自动同步调度器
    # （capabilities/platform/services/identity_sync_scheduler.py）以**一次 CAS 抢占**
    # 原子写入：`next_sync_at` 是「本租户下一次该自动同步的时刻」，`last_sync_at` 是
    # 「最近一次自动同步的发起时刻」。手动「立即同步」不写这两列（手动不受周期约束）。
    #
    # `next_sync_at IS NULL` = **立即到期**（拍板 ③，不是「还没排期」）：`auto_sync_enabled`
    # 刚打开、或本列刚随 039 落地时，下一轮调度就会为它排期，零人工等待。
    # 时区口径：列是 `TIMESTAMP WITHOUT TIME ZONE`（与同表
    # datasource_connection_verified_at、与 identity_sync_tasks.started_at 同档），
    # 写入方一律用**本机朴素时间** `datetime.now()` —— 不要写 tz-aware 值，
    # asyncpg 对 `timestamp` 参数做 `obj - datetime(2000,1,1)` 朴素减（见
    # asyncpg/pgproto/codecs/datetime.pyx::timestamp_encode），带 tzinfo 会直接 TypeError。
    last_sync_at = Column(TIMESTAMP)
    next_sync_at = Column(TIMESTAMP)


class IdentitySyncTask(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """同步任务聚合根（同步头）。"""

    __tablename__ = "identity_sync_tasks"
    __table_args__ = (
        # 单租户最多一条 running：状态机 + 该部分唯一索引双兜底并发窗口
        Index(
            "uq_identity_sync_tasks_running",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        {"schema": "platform"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    status = Column(
        String(32), nullable=False, default=SyncTaskStatus.PENDING.value
    )
    sync_type = Column(
        String(32), nullable=False, default=SyncType.INCREMENTAL.value
    )
    trigger = Column(String(32), nullable=False, default=SyncTrigger.MANUAL.value)

    user_added = Column(Integer, nullable=False, default=0)
    user_updated = Column(Integer, nullable=False, default=0)
    user_disabled = Column(Integer, nullable=False, default=0)
    # 启用路径独立计数：new_status==1 且原状态非启用时 +1，
    # 与 user_disabled 对称，不再只记在 user_updated 上。
    user_enabled = Column(Integer, nullable=False, default=0)
    # 全量缺失经「同轮复核」确认上游已删 ⇒ 软删（is_deleted=1）的账号数；
    # 上游恢复后原地复活**不计本列**（复用 user_enabled 口径）。
    user_deleted = Column(Integer, nullable=False, default=0)
    failed_count = Column(Integer, nullable=False, default=0)

    started_at = Column(TIMESTAMP)
    finished_at = Column(TIMESTAMP)
    # 增量游标：供下次增量同步续拉
    cursor = Column(String(1024))


class IdentitySyncFailure(Base):
    """同步失败明细（弱实体）。

    归属 sync_task 聚合，经父级下钻保证租户隔离，不单独带 tenant_id。
    """

    __tablename__ = "identity_sync_failures"
    __table_args__ = {"schema": "platform"}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    sync_task_id = Column(BigInteger, nullable=False, index=True)
    entity_type = Column(
        String(32), nullable=False, default=SyncEntityType.USER.value
    )
    external_entity_id = Column(String(128))
    # 脱敏后的失败原因（不落技术细节明文）
    failure_reason = Column(String(512))
    created_at = Column(DateTime, default=datetime.now, nullable=False)


__all__ = [
    "LoginPolicy",
    "SyncTaskStatus",
    "SyncType",
    "SyncTrigger",
    "SyncEntityType",
    "IdentityIntegrationConfig",
    "IdentitySyncTask",
    "IdentitySyncFailure",
]
