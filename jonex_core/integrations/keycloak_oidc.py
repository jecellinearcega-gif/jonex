#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Keycloak OIDC 登录侧 token 端点客户端（public client）。

与 ``keycloak_admin.py``（Admin API，confidential ``client_credentials``）职责分离：
本模块承载登录侧 public client 的 OIDC token 端点交换 —— ``refresh_token`` grant
与 ``authorization_code`` grant。public client 无 ``client_secret``，
凭 ``client_id`` + refresh token / PKCE 交换。

安全契约：
- server URL / client_id 读环境变量（``KEYCLOAK_`` 前缀，见 ``keycloak_admin.py`` 键名约定），
  空 server URL 时 fail-closed 抛 :class:`UpstreamServiceError`；
- Keycloak 返回 ``invalid_grant``（refresh token 失效/撤销/rotation 重放）时，
  映射为 :class:`RefreshTokenRevokedError`（401 语义），供 refresh 端点引导重新登录；
  其余 HTTP/网络错误抛 :class:`UpstreamServiceError`（上游依赖错误）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import httpx

from jonex_core.common import get_logger
from jonex_core.common.exceptions import (
    JonexException,
    OidcAuthorizationCodeError,
    OidcAuthorizationCodeExpiredError,
    OidcCallbackRejectedError,
    OidcSessionConflictError,
    RefreshTokenRevokedError,
    UpstreamServiceError,
)

logger = get_logger("integrations.keycloak_oidc")

# 键名与 keycloak_admin.py 的 KEYCLOAK_ 约定一致，在 config.py 落地。
KEYCLOAK_SERVER_URL = "KEYCLOAK_SERVER_URL"
# 浏览器视角的 Keycloak 基地址（authorize 302 目标，浏览器须可达），
# 与 KEYCLOAK_SERVER_URL（后端进程访问 keycloak，token 交换 / Admin API）解耦：
# Docker 下后端用 host.docker.internal，宿主机浏览器解析不了，authorize 须走此变量。
KEYCLOAK_AUTHORIZE_BASE_URL = "KEYCLOAK_AUTHORIZE_BASE_URL"
KEYCLOAK_CLIENT_ID = "KEYCLOAK_CLIENT_ID"
KEYCLOAK_TIMEOUT = "KEYCLOAK_TIMEOUT"
# RP-initiated logout 完成后的回跳地址（浏览器须可达；须与 Keycloak client 的
# post.logout.redirect.uris 白名单匹配）。空 = 不带该参数。
KEYCLOAK_POST_LOGOUT_REDIRECT_URI = "KEYCLOAK_POST_LOGOUT_REDIRECT_URI"

_DEFAULT_TIMEOUT = 30.0

# Keycloak token 端点拒绝时的 OAuth2 错误码（= rotation 下旧 token 复用/撤销/过期/授权码一次性）。
_INVALID_GRANT = "invalid_grant"

# invalid_grant 异常映射：按 grant_type 区分语义（refresh → 401 引导重登；code → 400 重新发起登录）。
_INVALID_GRANT_EXCEPTIONS = {
    "refresh_token": RefreshTokenRevokedError,
    "authorization_code": OidcAuthorizationCodeError,
}

# ── 授权端点回跳（callback）携带的 OAuth2 error → 异常分类 ────────────────────
# Keycloak 在「浏览器已有会话（already_logged_in）」时**不回** expired_code，而是回
# ``error=temporarily_unavailable`` + ``error_description=authentication_expired``；
# 回跳 URL 重放（code 已被消费 / 过期）才回 ``error=expired_code``。两者成因与处置
# 完全不同，必须归到不同错误码与文案，否则现场只剩「授权码兑换失败」无从排障。
_CALLBACK_SESSION_CONFLICT_DESCRIPTIONS = frozenset({"authentication_expired"})
_CALLBACK_SESSION_CONFLICT_ERRORS = frozenset({"already_logged_in"})
_CALLBACK_EXPIRED_ERRORS = frozenset({"expired_code", "invalid_grant"})

# error / error_description 是 IdP 可控字符串，落日志与进 details 前必须收敛：
# 剔除非安全字符（含换行等控制字符，防日志注入）+ 限长（防刷屏）。
_OIDC_ERROR_VALUE_MAX_LENGTH = 128
_OIDC_ERROR_VALUE_UNSAFE = re.compile(r"[^A-Za-z0-9 ._:\-]")


def _env_float(name: str, default: float) -> float:
    """读数值型 env，容错空串 / 空白串 / 非法值 —— 一律回退 ``default``。

    键存在但为空串（``KEYCLOAK_TIMEOUT=``）时，裸 ``float()`` 会抛 ``ValueError``，
    让未填真实 SSO 配置的部署起不来。语义与 ``keycloak_admin._env_float`` 一致。
    """
    raw = os.environ.get(name, "")
    if isinstance(raw, str):
        raw = raw.strip()
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def sanitize_oidc_error_value(
    value: Optional[str], *, max_length: int = _OIDC_ERROR_VALUE_MAX_LENGTH
) -> str:
    """归一 IdP 回传的 ``error`` / ``error_description``：去空白 + 剔不安全字符 + 限长。

    供日志与异常 ``details`` 使用；空值/None 归一为 ``""``（调用方自行决定兜底文案）。
    """
    if not value:
        return ""
    return _OIDC_ERROR_VALUE_UNSAFE.sub("", str(value).strip())[:max_length]


def authorization_error_class(
    error: Optional[str], error_description: Optional[str] = None
) -> type:
    """把授权端点回跳的 OAuth2 ``error`` / ``error_description`` 归到异常类（纯函数）。

    分类（顺序敏感）：
    1. **会话冲突** —— ``error_description=authentication_expired``（Keycloak 对
       ``already_logged_in`` 的编码）或 ``error=already_logged_in`` →
       :class:`OidcSessionConflictError`（3023）；
    2. **授权码过期 / URL 重放** —— ``error=expired_code`` / ``invalid_grant`` →
       :class:`OidcAuthorizationCodeExpiredError`（3024）；
    3. **其它未知** —— 一律 :class:`OidcCallbackRejectedError`（3025），原始值经
       ``details`` + 日志透出。

    注意：``temporarily_unavailable`` 单独出现（无 ``authentication_expired`` 描述）
    不归到会话冲突 —— 它也可能是 Keycloak 真的暂时不可用，按未知处理更保守。
    """
    e = (error or "").strip().lower()
    d = (error_description or "").strip().lower()
    if e in _CALLBACK_SESSION_CONFLICT_ERRORS or d in _CALLBACK_SESSION_CONFLICT_DESCRIPTIONS:
        return OidcSessionConflictError
    if e in _CALLBACK_EXPIRED_ERRORS or d in _CALLBACK_EXPIRED_ERRORS:
        return OidcAuthorizationCodeExpiredError
    return OidcCallbackRejectedError


def build_authorization_error(
    error: Optional[str], error_description: Optional[str] = None
) -> JonexException:
    """按分类构造回调异常实例，原始 ``error`` / ``error_description`` 落 ``details``。

    未知错误的 ``error`` 值经 ``params`` 渲染进用户文案（模板键 = 3025，
    见 ``jonex_core/common/messages/*.yaml``）；会话冲突 / 授权码过期两类用固定文案，
    原始值只进 ``details`` 与日志。所有值均经 :func:`sanitize_oidc_error_value` 脱敏。
    """
    exc_cls = authorization_error_class(error, error_description)
    safe_error = sanitize_oidc_error_value(error)
    safe_description = sanitize_oidc_error_value(error_description)
    details = {"oidc_error": safe_error or "unknown"}
    if safe_description:
        details["oidc_error_description"] = safe_description
    params = None
    if exc_cls is OidcCallbackRejectedError:
        params = {"error": safe_error or "unknown"}
    return exc_cls(details=details, params=params)


@dataclass(frozen=True)
class KeycloakTokenResponse:
    """token 端点返回（只保留 refresh 链路需要的字段）。"""

    access_token: str
    refresh_token: Optional[str] = None
    id_token: Optional[str] = None
    expires_in: Optional[int] = None
    token_type: str = "Bearer"


class KeycloakOidcClient:
    """Keycloak OIDC token 端点客户端（异步，public client）。

    构造参数均可注入（``transport`` 供测试用 MockTransport），生产缺省读环境变量。
    """

    def __init__(
        self,
        *,
        server_url: Optional[str] = None,
        client_id: Optional[str] = None,
        timeout: Optional[float] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._server_url = (
            server_url or os.environ.get(KEYCLOAK_SERVER_URL, "")
        ).rstrip("/")
        self._client_id = client_id or os.environ.get(KEYCLOAK_CLIENT_ID, "")
        self._timeout = (
            float(timeout)
            if timeout is not None
            else _env_float(KEYCLOAK_TIMEOUT, _DEFAULT_TIMEOUT)
        )
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None

    # ── HTTP ────────────────────────────────────────────────────────────────

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            )
        return self._client

    def _require_server_url(self) -> None:
        if not self._server_url:
            raise UpstreamServiceError(
                message=f"Keycloak OIDC 客户端初始化失败：缺少环境变量 {KEYCLOAK_SERVER_URL}。"
            )

    async def _post_token(self, realm: str, data: dict) -> KeycloakTokenResponse:
        self._require_server_url()
        url = f"{self._server_url}/realms/{realm}/protocol/openid-connect/token"
        grant_type = data.get("grant_type")
        try:
            resp = await self._get_client().post(url, data=data)
            # 400 + invalid_grant 优先按 grant_type 抛业务异常（refresh → 401 / code → 400），
            # 必须放在 raise_for_status 之前，避免被当成普通 4xx 收敛成 UpstreamServiceError。
            # 这两个是 JonexException 子类，不会被下方 except httpx.HTTPError / ValueError 捕获。
            if resp.status_code == 400:
                error = ""
                error_description = ""
                try:
                    body = resp.json() or {}
                    error = body.get("error", "") or ""
                    error_description = body.get("error_description", "") or ""
                except ValueError:
                    error = ""
                if error == _INVALID_GRANT:
                    # 落原始 error_description（id_token/code/refresh 等敏感值不落）：
                    # 兑换失败与「回调带 error 被当成兑换失败」是两类问题，日志须能区分。
                    logger.warning(
                        "Keycloak token 端点拒绝：realm=%s grant_type=%s error=%s error_description=%s",
                        realm,
                        grant_type,
                        sanitize_oidc_error_value(error),
                        sanitize_oidc_error_value(error_description),
                    )
                    exc_cls = _INVALID_GRANT_EXCEPTIONS.get(
                        grant_type, RefreshTokenRevokedError
                    )
                    raise exc_cls()
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise UpstreamServiceError(
                message="Keycloak token 端点请求失败",
                details={"realm": realm, "grant_type": grant_type},
                cause=exc,
            ) from exc
        except ValueError as exc:
            raise UpstreamServiceError(
                message="Keycloak token 端点响应解析失败",
                details={"realm": realm, "grant_type": grant_type},
                cause=exc,
            ) from exc

        access_token = payload.get("access_token")
        if not access_token:
            raise UpstreamServiceError(
                message="Keycloak token 响应缺少 access_token",
                details={"realm": realm},
            )
        return KeycloakTokenResponse(
            access_token=access_token,
            refresh_token=payload.get("refresh_token"),
            id_token=payload.get("id_token"),
            expires_in=payload.get("expires_in"),
            token_type=payload.get("token_type") or "Bearer",
        )

    # ── 对外 API ────────────────────────────────────────────────────────────

    async def exchange_refresh_token(
        self, realm: str, refresh_token: str
    ) -> KeycloakTokenResponse:
        """用 Keycloak refresh token 换新（rotation）。

        ``grant_type=refresh_token`` + ``client_id``（public client 无 secret）。
        失败（invalid_grant）抛 :class:`RefreshTokenRevokedError`。
        """
        return await self._post_token(
            realm,
            {
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "refresh_token": refresh_token,
            },
        )

    async def exchange_authorization_code(
        self,
        realm: str,
        code: str,
        *,
        redirect_uri: str,
        code_verifier: str,
    ) -> KeycloakTokenResponse:
        """用授权码换 token（PKCE S256）。

        ``grant_type=authorization_code`` + ``client_id`` + ``code`` +
        ``redirect_uri``（须与授权请求一致）+ ``code_verifier``（public client 无 secret）。
        失败（invalid_grant：授权码无效 / 过期 / 已用 / code_verifier 不匹配）抛
        :class:`OidcAuthorizationCodeError`。
        """
        return await self._post_token(
            realm,
            {
                "grant_type": "authorization_code",
                "client_id": self._client_id,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )


def build_authorize_url(
    realm: str,
    *,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
    idp_hint: Optional[str] = None,
) -> str:
    """拼装 Keycloak OIDC authorize 发起 URL（纯函数，无 IO / 无状态）。

    契约：
    - 基地址优先读 ``KEYCLOAK_AUTHORIZE_BASE_URL``（浏览器视角），缺省回退
      ``KEYCLOAK_SERVER_URL``（后端进程视角，token 交换 / Admin API 复用）；
      client_id 读 ``KEYCLOAK_CLIENT_ID``（均复用模块顶部常量键名，与
      ``keycloak_admin.py`` 约定一致），不注入入参；
    - 基地址末尾 ``/`` 会被 ``rstrip``，避免产出 ``//`` 双斜杠；
    - 基地址为空或 client_id 为空时抛 :class:`UpstreamServiceError`，fail-closed
      （对齐 ``KeycloakOidcClient._require_server_url`` 语义，禁止半成品 URL 泄露）；
    - query 用 ``urlencode`` 编码，参数顺序固定：client_id、response_type=code、
      scope=openid profile email、redirect_uri、state、nonce、code_challenge、
      code_challenge_method=S256（PKCE S256，public client 无 secret），
      **最后**可选追加 ``kc_idp_hint``（仅 ``idp_hint`` 非空时注入，见下条；``urlencode``
      保序，恒为 query 的最后一个键）；
    - ``idp_hint`` 可选，**非空才注入**（``None`` 与 ``""`` 一并不注入，键不存在而非空串）。
      注入的是 Keycloak 官方特性「Client-suggested Identity Provider」的参数
      ``kc_idp_hint``，取值 = **该 realm 内 IdP 的 alias**（本环境 alias 为 ``casdoor``）。
      作用是让 Keycloak 直接 303 跳到指定外部身份源，跳过自己的中间登录表单（用户不必再点
      一次「用 Casdoor 登录」）。官方明确：**alias 在 realm 内不存在时回落显示登录表单**
      （软失败、不报错），故本函数不做任何存在性校验 —— 那需要查 Admin API，会破坏纯函数契约
      （本函数不读 env 之外的任何状态、不做任何 IO 或网络探测）。注：注入还要求 realm 的
      browser flow 含 ``identity-provider-redirector``，缺失时同样静默失效，同属部署侧前提；
    - **不申请 ``offline_access``**：申请它会让 Keycloak 只建离线会话，而 Keycloak 26
      的 Back-Channel Logout 只遍历在线会话（admin logout / RP-initiated logout 两条
      路径都够不到离线会话），反向登出将永不触发。长会话改由 realm 的
      ``ssoSessionIdleTimeout`` / ``ssoSessionMaxLifespan`` 承载。

    返回：``{base_url}/realms/{realm}/protocol/openid-connect/auth?<query>``。
    """
    base_url = (
        os.environ.get(KEYCLOAK_AUTHORIZE_BASE_URL, "")
        or os.environ.get(KEYCLOAK_SERVER_URL, "")
    ).rstrip("/")
    client_id = os.environ.get(KEYCLOAK_CLIENT_ID, "")
    if not base_url:
        raise UpstreamServiceError(
            message=(
                "Keycloak OIDC authorize 发起失败：缺少环境变量 "
                f"{KEYCLOAK_AUTHORIZE_BASE_URL}（或 {KEYCLOAK_SERVER_URL}）。"
            )
        )
    if not client_id:
        raise UpstreamServiceError(
            message=f"Keycloak OIDC authorize 发起失败：缺少环境变量 {KEYCLOAK_CLIENT_ID}。"
        )

    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": "openid profile email",
        "redirect_uri": redirect_uri,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if idp_hint:
        params["kc_idp_hint"] = idp_hint
    query = urlencode(params)
    return f"{base_url}/realms/{realm}/protocol/openid-connect/auth?{query}"


def build_end_session_url(
    realm: str,
    *,
    id_token_hint: Optional[str] = None,
    post_logout_redirect_uri: Optional[str] = None,
) -> str:
    """拼装 Keycloak RP-initiated logout（``end_session_endpoint``）URL（纯函数，无 IO）。

    用途：应用内「退出登录」必须终结 Keycloak 侧 SSO 会话，否则浏览器残留的
    ``KEYCLOAK_IDENTITY`` / ``KEYCLOAK_SESSION`` cookie 会让下一次 authorize 静默
    登回原用户（无法切换账号）。本地 refresh 家族吊销由 service 层负责，本函数只产 URL。

    契约：
    - 基地址与 client_id 的取值口径与 :func:`build_authorize_url` 完全一致（优先
      ``KEYCLOAK_AUTHORIZE_BASE_URL``、回退 ``KEYCLOAK_SERVER_URL``，``rstrip("/")``；
      client_id 读 ``KEYCLOAK_CLIENT_ID``），因为该 URL 由**浏览器**跳转，不能用后端
      视角的 host（Docker 下 host.docker.internal 浏览器解析不了）；
    - 基地址为空或 client_id 为空抛 :class:`UpstreamServiceError`（fail-closed，禁止
      半成品 URL）。**调用方须容错**：登出链路里本函数失败不得回滚已完成的本地吊销，
      应降级为「仅本地登出」；
    - ``id_token_hint`` 可选。带它 Keycloak 直接终结该 ``sid`` 对应的 SSO 会话（静默）；
      不带则 Keycloak 26 会先弹「Do you want to log out?」确认页。id_token 的 ``exp``
      通常早已过期，但 OIDC RP-Initiated Logout 规范要求 OP「即使 exp 已过也应接受」
      该 hint（Keycloak 亦然），故不必为它做续期；
    - ``post_logout_redirect_uri`` 可选。**带上就必须命中 Keycloak client 的
      ``post.logout.redirect.uris`` 白名单**，否则 Keycloak 报 invalid_redirect_uri 而
      整个登出失败；拿不准时传 None（Keycloak 结束后停在自己的「已登出」页）；
    - query 用 ``urlencode`` 编码，参数顺序固定：client_id、id_token_hint、
      post_logout_redirect_uri（后两者为 None 时省略）。

    返回：``{base_url}/realms/{realm}/protocol/openid-connect/logout?<query>``。
    """
    base_url = (
        os.environ.get(KEYCLOAK_AUTHORIZE_BASE_URL, "")
        or os.environ.get(KEYCLOAK_SERVER_URL, "")
    ).rstrip("/")
    client_id = os.environ.get(KEYCLOAK_CLIENT_ID, "")
    if not base_url:
        raise UpstreamServiceError(
            message=(
                "Keycloak RP-initiated logout URL 构造失败：缺少环境变量 "
                f"{KEYCLOAK_AUTHORIZE_BASE_URL}（或 {KEYCLOAK_SERVER_URL}）。"
            )
        )
    if not client_id:
        raise UpstreamServiceError(
            message=(
                "Keycloak RP-initiated logout URL 构造失败：缺少环境变量 "
                f"{KEYCLOAK_CLIENT_ID}。"
            )
        )

    params = {"client_id": client_id}
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    if post_logout_redirect_uri:
        params["post_logout_redirect_uri"] = post_logout_redirect_uri
    query = urlencode(params)
    return f"{base_url}/realms/{realm}/protocol/openid-connect/logout?{query}"


__all__ = [
    "KeycloakTokenResponse",
    "KeycloakOidcClient",
    "build_authorize_url",
    "build_end_session_url",
    "authorization_error_class",
    "build_authorization_error",
    "sanitize_oidc_error_value",
    "KEYCLOAK_SERVER_URL",
    "KEYCLOAK_AUTHORIZE_BASE_URL",
    "KEYCLOAK_CLIENT_ID",
    "KEYCLOAK_POST_LOGOUT_REDIRECT_URI",
    "KEYCLOAK_TIMEOUT",
]
