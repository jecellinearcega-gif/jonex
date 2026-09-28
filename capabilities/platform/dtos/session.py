"""Keycloak SSO 会话聚合根 DTO + Redis 键结构约定。

只定义实体结构与键结构约定，不实现完整生命周期（登录创建 / 刷新滑动续期 /
登出·SLO·账号停用吊销归后续任务）。与 LoginTicket（DB、已登录换票，
`user_id NOT NULL`）职责分离；OIDC state/nonce 落本文件 OidcState（Redis 一次性票据）。
"""
from datetime import datetime
from typing import Optional

try:
    from pydantic.v1 import BaseModel
except ImportError:
    from pydantic import BaseModel


# ============ Redis 键结构约定 ============

SESSION_KEY_PREFIX = "jonex:session"
REFRESH_FAMILY_KEY_PREFIX = "jonex:refresh"
LOGOUT_JTI_KEY_PREFIX = "jonex:logout:jti"
OIDC_STATE_KEY_PREFIX = "jonex:oidc:state"


def session_key(sid: str) -> str:
    """会话聚合根键：对齐 Keycloak SSO Session `sid`（全局唯一）。"""
    return f"{SESSION_KEY_PREFIX}:{sid}"


def refresh_family_key(tenant_id: str, user_id: int) -> str:
    """refresh token 家族键：同用户（tenant_id + user_id）的 refresh 归一组，便于轮换与撤销。

    值为 Redis HASH：field=sid（Keycloak SSO Session 标识，多设备隔离）→ value=当前 jti。
    同一用户不同设备（不同 sid）各存各的 jti，互不覆盖，避免多设备登录互踢。
    """
    return f"{REFRESH_FAMILY_KEY_PREFIX}:{tenant_id}:{user_id}"


def logout_jti_key(jti: str) -> str:
    """logout_token jti 防重放键：已处理的 logout_token 记录于此，重放即拒绝。"""
    return f"{LOGOUT_JTI_KEY_PREFIX}:{jti}"


def oidc_state_key(state: str) -> str:
    """OIDC 授权码流程 state 一次性票据键（Redis 主存，授权码兑换时原子消费）。"""
    return f"{OIDC_STATE_KEY_PREFIX}:{state}"


# ============ 会话聚合根实体 ============

class SessionAggregate(BaseModel):
    """Redis 主存的会话聚合根。

    - sid: 对齐 Keycloak SSO Session `sid`（session 标识，全局唯一）。
    - user_id: 本地自增 int user_id（external_id 映射后的本地主键）。
    - tenant_id: 会话所属租户（Realm 名 / issuer URL 反查得到）。
    - refresh_token_ref: refresh token 家族在 Redis 中的引用（family 键），用于轮换 / 撤销。
    - created_at / expires_at: 会话创建 / 过期时间，TTL 对齐 Keycloak 在线会话
      （``ssoSessionIdleTimeout`` 7 天 / ``ssoSessionMaxLifespan`` 30 天）。
    - revoked_at: 非空表示会话已吊销（登出 / SLO 回调 / 账号停用）。
    """

    sid: str
    user_id: int
    tenant_id: str
    refresh_token_ref: Optional[str] = None
    created_at: datetime
    expires_at: datetime
    revoked_at: Optional[datetime] = None


# ============ OIDC 授权码 state 一次性票据 ============

class OidcState(BaseModel):
    """OIDC 授权码流程的 state 一次性票据（Redis 主存，callback 原子消费）。

    由授权发起端点写入，callback 读取并 `getdel` 一次性消费，
    承载跨跳转必需的 PKCE / nonce / 重定向上下文：

    - state: 随机 state 值（防 CSRF），键 = ``oidc_state_key(state)``。
    - tenant_id: 发起登录的租户（callback 据此反查 Realm，不信任 token claim）。
    - nonce: 校验 id_token ``nonce``（防重放）。
    - code_verifier / code_challenge / code_challenge_method: PKCE S256（local 预校验 +
      Keycloak token 端点强制校验）。
    - redirect_uri: OIDC 回调 URL，回显给 Keycloak token 端点（须与授权请求一致）。
    - post_login_redirect_uri: 登录成功后 302 跳转的 SPA 地址（回调侧再做白名单校验）。
    """

    state: str
    tenant_id: str
    nonce: str
    code_verifier: str
    code_challenge: Optional[str] = None
    code_challenge_method: Optional[str] = "S256"
    redirect_uri: str
    post_login_redirect_uri: str
    created_at: datetime
