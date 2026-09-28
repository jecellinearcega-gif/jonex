#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Keycloak OIDC RS256/JWKS 验签工具

供 /auth/oidc/callback 与 /auth/oidc/backchannel-logout 边界端点使用，
与本地 HS256 ``decode_token``（``user_auth.py``）完全隔离：本模块只验 RS256，
拒绝任何算法回退（HS256 签名 token 走 RS256 路径必须拒收）。

安全契约：
- issuer/JWKS URL 来自配置白名单，禁止由 token 的 ``iss`` 动态拼接 URL（防 SSRF）。
- 验签失败统一抛 :class:`TokenValidationError`（401 语义），禁止裸
  ``ValueError``/``Exception`` 泄漏为 500。
- 算法固定 RS256：header ``alg`` 非 ``RS256`` 直接拒收；即使 header 伪造
  ``alg=RS256`` 但实际 HMAC 签名，也因仅用 RSA 公钥验签而失败。

配置依赖（白名单字段由部署配置落地，本模块只读）：
- ``KEYCLOAK_ISSUER_WHITELIST``: JSON 字符串，映射 issuer URL -> 条目::

      {
        "https://sso.example.com/realms/jonex-tenant_jonex_demo": {
          "jwks_uri": "https://sso.example.com/realms/jonex-tenant_jonex_demo/protocol/openid-connect/certs",
          "client_id": "jonex-platform",
          "audience": "jonex-platform"   // 可选，默认取 client_id
        }
      }

- ``KEYCLOAK_JWKS_TIMEOUT_SECONDS``: JWKS 拉取超时（秒），默认 5。
- ``KEYCLOAK_JWKS_CACHE_TTL_SECONDS``: JWKS 本地缓存 TTL（秒），默认 3600。

白名单加载入口为模块级 :func:`load_issuer_whitelist`（跨包共享符号，
平台侧按 realm 反查 issuer 直接调用它）；:meth:`OidcTokenVerifier._load_whitelist`
只是它的兼容包装，逻辑只有一份。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Dict, Optional

import jwt
from jwt.algorithms import RSAAlgorithm

from jonex_core.common.config import get_config
from jonex_core.common.exceptions import TokenValidationError
from jonex_core.security.internal_auth import CLOCK_SKEW_LEEWAY_SECONDS

logger = logging.getLogger(__name__)

# 验签固定算法。禁止 try-RS256-fallback-HS256。
_RS256_ALGORITHMS = ["RS256"]

# JWKS 拉取默认超时（秒），可用 KEYCLOAK_JWKS_TIMEOUT_SECONDS 覆盖。
_DEFAULT_JWKS_TIMEOUT_SECONDS = 5

# JWKS 本地缓存默认 TTL（秒），可用 KEYCLOAK_JWKS_CACHE_TTL_SECONDS 覆盖。
_DEFAULT_JWKS_CACHE_TTL_SECONDS = 3600


def _default_fetch_jwks(jwks_uri: str, timeout: float) -> Dict[str, dict]:
    """默认 JWKS 拉取实现：GET ``jwks_uri``，解析 ``keys`` 为 ``kid -> JWK``。

    独立成函数便于测试注入 mock fetcher（充当 mock JWKS 端点）。
    """
    import httpx

    resp = httpx.get(jwks_uri, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    result: Dict[str, dict] = {}
    for key in data.get("keys") or []:
        kid = key.get("kid")
        if kid:
            result[kid] = key
    return result


def _jwk_to_public_key(jwk_dict: dict):
    """JWK 转 RSA 公钥，只接受 ``kty == RSA``（防 oct/EC 混淆注入）。"""
    if jwk_dict.get("kty") != "RSA":
        raise TokenValidationError("JWK 公钥类型非法（仅接受 RSA）")
    try:
        return RSAAlgorithm.from_jwk(json.dumps(jwk_dict))
    except Exception as exc:  # noqa: BLE001 - 统一转业务异常
        logger.warning("JWK 公钥解析失败: %s", exc)
        raise TokenValidationError("JWK 公钥解析失败") from exc


# ---------- 配置白名单（模块级入口，供跨包复用） ----------


def load_issuer_whitelist() -> Dict[str, dict]:
    """加载 KEYCLOAK_ISSUER_WHITELIST 配置，返回 ``{issuer: {"jwks_uri", "client_id", "audience"}}``。

    白名单加载的唯一实现；:meth:`OidcTokenVerifier._load_whitelist` 是它的兼容包装。
    跨包（如平台侧 SSO「测试连接」按 realm 反查 issuer）应直接调用本函数，
    不要为取白名单去实例化验签器、也不要依赖私有名。

    解析与校验规则（fail-closed，逐字保留既有语义）：
    - 配置缺失/为空 -> ``{}``。
    - ``json.loads`` 抛 ``ValueError`` -> 记 error 日志后 ``{}``。
    - 解析结果非 ``dict`` -> 记 error 日志后 ``{}``。
    - 逐条强校验：条目须为 ``dict`` 且 ``jwks_uri`` / ``client_id`` 均为非空 ``str``，
      否则该条被拒（``continue``），其余条目保留。
    - ``audience`` 可选，缺省取 ``client_id``；key 统一 ``str(issuer)``。
    """
    raw = getattr(get_config(), "KEYCLOAK_ISSUER_WHITELIST", None) or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.error("KEYCLOAK_ISSUER_WHITELIST 解析失败: %s", exc)
        return {}
    if not isinstance(data, dict):
        logger.error("KEYCLOAK_ISSUER_WHITELIST 必须是 JSON 对象")
        return {}
    result: Dict[str, dict] = {}
    for issuer, entry in data.items():
        # 强校验（fail-closed）：条目必须是 dict 且含非空 jwks_uri 与 client_id，
        # 否则拒绝该条目，防止漏配导致任意同 realm 客户端 token 被接受。
        if not isinstance(entry, dict):
            logger.error("KEYCLOAK_ISSUER_WHITELIST 条目 %s 非法（非对象），已拒绝", issuer)
            continue
        jwks_uri = entry.get("jwks_uri")
        client_id = entry.get("client_id")
        if not jwks_uri or not isinstance(jwks_uri, str):
            logger.error("KEYCLOAK_ISSUER_WHITELIST 条目 %s 缺少 jwks_uri，已拒绝", issuer)
            continue
        if not client_id or not isinstance(client_id, str):
            logger.error("KEYCLOAK_ISSUER_WHITELIST 条目 %s 缺少 client_id，已拒绝", issuer)
            continue
        # audience 可选，默认取 client_id
        result[str(issuer)] = {
            "jwks_uri": jwks_uri,
            "client_id": client_id,
            "audience": entry.get("audience") or client_id,
        }
    return result


class OidcTokenVerifier:
    """Keycloak RS256/JWKS 验签器。

    每个 issuer 维护独立 JWKS 缓存：``kid -> JWK``，带 TTL。TTL 内旧 kid 保留
    以支持密钥轮换窗口（旧 kid token 在 ``exp`` 内仍接受）；超 TTL 整体刷新，
    已不在 JWKS 端点的 kid 自然淘汰（被吊销/泄露的公钥不再永久驻留）。
    """

    def __init__(
        self,
        issuer_whitelist: Optional[Dict[str, dict]] = None,
        jwks_fetcher: Optional[Callable[[str, float], Dict[str, dict]]] = None,
        leeway: Optional[int] = None,
        timeout: Optional[float] = None,
        ttl: Optional[float] = None,
    ):
        self._whitelist = (
            issuer_whitelist if issuer_whitelist is not None else self._load_whitelist()
        )
        self._fetcher = jwks_fetcher or _default_fetch_jwks
        self._leeway = leeway if leeway is not None else CLOCK_SKEW_LEEWAY_SECONDS
        self._timeout = timeout if timeout is not None else self._config_jwks_timeout()
        self._jwks_cache_ttl = ttl if ttl is not None else self._config_jwks_cache_ttl()
        # issuer -> {kid -> jwk}
        self._jwks_cache: Dict[str, Dict[str, dict]] = {}
        # issuer -> 最近一次成功拉取 JWKS 的 monotonic 时间戳
        self._jwks_fetched_at: Dict[str, float] = {}
        self._lock = threading.Lock()

    # ---------- 配置白名单 ----------

    @staticmethod
    def _config_jwks_timeout() -> float:
        try:
            return float(getattr(get_config(), "KEYCLOAK_JWKS_TIMEOUT_SECONDS", None) or _DEFAULT_JWKS_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            return _DEFAULT_JWKS_TIMEOUT_SECONDS

    @staticmethod
    def _config_jwks_cache_ttl() -> float:
        try:
            return float(getattr(get_config(), "KEYCLOAK_JWKS_CACHE_TTL_SECONDS", None) or _DEFAULT_JWKS_CACHE_TTL_SECONDS)
        except (TypeError, ValueError):
            return _DEFAULT_JWKS_CACHE_TTL_SECONDS

    @staticmethod
    def _load_whitelist() -> Dict[str, dict]:
        """兼容包装：实现见模块级 :func:`load_issuer_whitelist`。"""
        return load_issuer_whitelist()

    # ---------- JWKS 缓存 + kid 轮换 ----------

    def _get_signing_key(self, issuer: str, jwks_uri: str, kid: str) -> Optional[dict]:
        """返回 kid 对应 JWK；缓存带 TTL，超 TTL 整体刷新以淘汰已撤销/泄露的旧 kid。"""
        with self._lock:
            cache = self._jwks_cache.get(issuer) or {}
            fetched_at = self._jwks_fetched_at.get(issuer)
            stale = fetched_at is None or (time.monotonic() - fetched_at) >= self._jwks_cache_ttl

            # 缓存命中且未超 TTL：直接返回（密钥轮换窗口内旧 kid 仍可验签）。
            if kid in cache and not stale:
                return cache[kid]

            # 未命中或超 TTL：重新拉取。
            try:
                fresh = self._fetcher(jwks_uri, self._timeout)
            except Exception as exc:  # noqa: BLE001 - 拉取失败不阻断旧 kid token
                logger.warning("JWKS 拉取失败 (%s): %s", issuer, exc)
                fresh = None

            if not fresh:
                # 拉取失败：降级用旧缓存（宁可暂时接受旧密钥也不 DoS）。
                return cache.get(kid)

            if stale:
                # 超 TTL：整体替换，已不在 JWKS 端点的 kid 自然淘汰。
                self._jwks_cache[issuer] = dict(fresh)
            else:
                # TTL 内：合并保留旧 kid，支持密钥轮换窗口。
                self._jwks_cache[issuer] = {**cache, **fresh}
            self._jwks_fetched_at[issuer] = time.monotonic()

            return self._jwks_cache[issuer].get(kid)

    # ---------- 验签 ----------

    def verify(
        self,
        token: str,
        *,
        expected_issuer: str,
        expected_audience: Optional[str] = None,
        expected_azp: Optional[str] = None,
        require_azp: bool = True,
    ) -> dict:
        """验签并校验 iss/aud/azp/exp，返回解码后的 payload。

        Args:
            token: Keycloak 签发的 JWT（RS256）。
            expected_issuer: 期望 issuer，必须命中配置白名单（jwks_uri 取自白名单）。
            expected_audience: 期望 audience；缺省取白名单条目 ``audience``/``client_id``。
            expected_azp: 期望 authorized party；缺省取白名单条目 ``client_id``。
            require_azp: 是否强制校验 azp（默认 True）。backchannel logout_token
                （OIDC BCL 1.0）不含 azp，调用方传 False 跳过，仅靠 aud 校验身份。

        Returns:
            payload dict（含 sub/iss/aud/azp/exp 等 claim）。

        Raises:
            TokenValidationError: 算法混淆 / kid 未命中 / 签名或 claim 校验失败。
        """
        if not token or not isinstance(token, str):
            raise TokenValidationError("token 缺失")

        # 1. 读未验签 header：alg 非 RS256 直接拒（算法混淆第一道防线）。
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise TokenValidationError("token 头解析失败") from exc
        alg = header.get("alg")
        if alg != "RS256":
            raise TokenValidationError(f"不支持的签名算法: {alg!r}（仅接受 RS256）")
        kid = header.get("kid")
        if not kid:
            raise TokenValidationError("token 缺少 kid")

        # 2. issuer 必须命中白名单；jwks_uri 取自白名单（禁止由 iss 动态拼接）。
        entry = self._whitelist.get(expected_issuer)
        if not entry:
            raise TokenValidationError("issuer 不在配置白名单")
        jwks_uri = entry.get("jwks_uri")
        if not jwks_uri:
            raise TokenValidationError("白名单条目缺少 jwks_uri")

        # 3. 期望 audience/azp：显式参数 > 白名单条目。
        audience = (
            expected_audience
            if expected_audience is not None
            else (entry.get("audience") or entry.get("client_id"))
        )
        azp = expected_azp if expected_azp is not None else entry.get("client_id")

        # 4. 取公钥（缓存命中 / 刷新后按 kid 匹配）。
        key_jwk = self._get_signing_key(expected_issuer, jwks_uri, kid)
        if key_jwk is None:
            raise TokenValidationError("kid 未匹配到公钥（密钥已轮换且旧密钥不可用）")
        public_key = _jwk_to_public_key(key_jwk)

        # 5. 验签（固定 RS256 + leeway）。
        try:
            payload = jwt.decode(
                token,
                public_key,
                algorithms=_RS256_ALGORITHMS,
                leeway=self._leeway,
                options={"verify_aud": False, "verify_iss": False, "require": ["exp"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenValidationError("token 已过期") from exc
        except jwt.InvalidSignatureError as exc:
            raise TokenValidationError("签名校验失败") from exc
        except jwt.PyJWTError as exc:
            raise TokenValidationError("token 验签失败") from exc

        # 6. 显式校验 iss/aud/azp。
        self._validate_claims(payload, expected_issuer, audience, azp, require_azp=require_azp)
        return payload

    @staticmethod
    def _validate_claims(
        payload: dict,
        expected_issuer: str,
        expected_audience: Optional[str],
        expected_azp: Optional[str],
        *,
        require_azp: bool = True,
    ) -> None:
        if payload.get("iss") != expected_issuer:
            raise TokenValidationError("iss 不匹配")

        # aud 校验不得因 audience 缺失跳过：缺失即拒绝（fail-closed）。
        if not expected_audience:
            raise TokenValidationError("白名单条目缺少 audience")
        aud = payload.get("aud")
        aud_list = aud if isinstance(aud, list) else ([aud] if aud else [])
        if expected_audience not in aud_list:
            raise TokenValidationError("aud 不匹配")

        # azp 校验对齐 aud：require_azp 且期望 azp 时，token 必须携带 azp 且相等，缺 azp 即拒绝。
        # backchannel logout_token（OIDC BCL）不含 azp，调用方传 require_azp=False 跳过，仅靠 aud 校验。
        if require_azp and expected_azp:
            if "azp" not in payload:
                raise TokenValidationError("token 缺少 azp")
            if payload.get("azp") != expected_azp:
                raise TokenValidationError("azp 不匹配")


_verifier: Optional[OidcTokenVerifier] = None


def get_oidc_verifier() -> OidcTokenVerifier:
    """获取默认验签器单例。"""
    global _verifier
    if _verifier is None:
        _verifier = OidcTokenVerifier()
    return _verifier


def verify_rs256_token(
    token: str,
    *,
    expected_issuer: str,
    expected_audience: Optional[str] = None,
    expected_azp: Optional[str] = None,
    require_azp: bool = True,
) -> dict:
    """便捷入口：用默认单例验签 Keycloak RS256 token。"""
    return get_oidc_verifier().verify(
        token,
        expected_issuer=expected_issuer,
        expected_audience=expected_audience,
        expected_azp=expected_azp,
        require_azp=require_azp,
    )
