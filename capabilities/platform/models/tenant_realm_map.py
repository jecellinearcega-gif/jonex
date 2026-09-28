from sqlalchemy import Column, BigInteger, SmallInteger, String

from jonex_core.common.database import Base
from jonex_core.common.entity import TenantMixin, TimestampMixin


class TenantRealmMap(TenantMixin, TimestampMixin, Base):
    """租户 ↔ Keycloak Realm 映射表。

    realm = "jonex-" + tenant_id，全局唯一（一一对应）。供开通客户时 pre-provision
    Realm、登录反查 tenant、SLO 回调定位 tenant。授权码/token 边界端点用 issuer_url、
    client_id 做 iss/aud 校验。status 为启用/停用态（0 停用 / 1 启用）。
    """

    __tablename__ = "tenant_realm_map"
    __table_args__ = {"schema": "platform"}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    realm = Column(String(128), nullable=False, unique=True)
    issuer_url = Column(String(512), nullable=False)
    client_id = Column(String(255), nullable=False)
    status = Column(SmallInteger, default=1)
