from sqlalchemy import Column, BigInteger, String, SmallInteger, TIMESTAMP, Index, text

from jonex_core.common.database import Base
from jonex_core.common.entity import SoftDeleteMixin, TenantMixin, TimestampMixin


class User(TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        Index(
            "users_tenant_id_external_id_key",
            "tenant_id",
            "external_id",
            unique=True,
            postgresql_where=text("is_deleted = 0"),
        ),
        Index(
            "users_tenant_id_upstream_subject_key",
            "tenant_id",
            "upstream_subject",
            unique=True,
            postgresql_where=text("is_deleted = 0 AND upstream_subject IS NOT NULL"),
        ),
        {"schema": "platform"},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    username = Column(String(128), nullable=False)
    external_id = Column(String(64), nullable=True)
    # 上游客户系统的稳定主体标识（本地场景 = Casdoor 的 id）。
    # 跨 realm 稳定，是「认人」的主匹配键；而 external_id（Keycloak 本地 user_id）跨 realm 漂移。
    # ⛔ 不得与 external_id 混用 —— account_sync_service 拿 external_id 拼 KC Admin API URL。
    upstream_subject = Column(String(64), nullable=True)
    password_hash = Column(String(256), nullable=True)
    display_name = Column(String(255))
    email = Column(String(255))
    role = Column(String(32), nullable=False, default="user")
    status = Column(SmallInteger, default=1)
    last_login_at = Column(TIMESTAMP)
