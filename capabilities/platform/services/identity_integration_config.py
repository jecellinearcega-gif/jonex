#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""企业身份集成配置域服务（配置聚合根 CRUD + 测试连接 + 审计）。

职责：
- 按 tenant_id 一对一 get / upsert 企业身份集成配置。
- 生效 kc_idp_hint alias 读取（租户级覆盖 `or` 平台默认值，只读不落库），
  供 SSO 登录发起点拼 authorize URL（跳过 Keycloak 中间授权页）。
- 敏感字段 token 经 Fernet 加密落库（token_ciphertext），响应恒脱敏 `***`，
  明文不出现在响应与日志；client_secret 由部署侧注入（平台级单 client 模型），
  不落库、不进本契约。
- SSRF 校验：datasource_url 在保存 / 测试连接前调 validate_datasource_url，
  SSO 侧在**保存时**校验 sso.issuer_url、**测试连接时**校验解析出的探测基址
  （校验真正出站的目标），两者**同档** —— 均放行 http / https 与私网 / 回环 /
  链路本地（客户业务系统与私有化 Keycloak 都可能与平台同机部署）；仅 scheme 非法
  或无主机名 → InvalidTargetUrlError(1007)。
- SSO「服务地址」可编辑：PUT 的 sso.issuer_url 落 tenant_realm_map.issuer_url
  （本仓该表的唯一写入点），按 tenant_id 定位行、无行 → 1002 且不 INSERT。
- SSO「测试连接」探测目标**三级解析**（realm 取 tenant_realm_map.realm 权威列），
  三档产出**一律 issuer 形态**（含 `/realms/{realm}` 段）：
  ① 按 realm 反查 KEYCLOAK_ISSUER_WHITELIST，取 jwks_uri 只剥常量尾
  `/protocol/openid-connect/certs` → 后端视角 issuer（保留 realm 段）→
  ② KEYCLOAK_SERVER_URL（**服务器根**，须补 `/realms/{realm}`）→
  ③ 行内 issuer_url（现状兜底）。三档全落空 → 1002。
  原因：issuer_url 是对外（浏览器视角）的 OIDC issuer 标识，而探测是容器内出站，
  跨网络部署下不存在同时满足两个视角的单值。⚠️ realm 段不可削：实测
  `{服务器根}/.well-known/openid-configuration` → 404（realm 路径同端点 → 200）。
- 只读展示（get_sso_realm）额外回显 effective_probe_target（完整 probe_url，
  解析不出填 None、不抛异常）。
- 关键字段（datasource_url / token / SSO issuer_url）变更 → 对应
  *_connection_verified_at 失效。
- 测试连接（target=sso 或 datasource）：出站 HTTPS 探测，返回
  {ok, latency_ms, message}；成功绑定对应 *_connection_verified_at。
  失败分类：连接失败 → DataSourceConnectionError(5005)、
  认证失败 → DataSourceAuthError(5006)、超时 → ServiceTimeoutError(5004)、
  契约不符 → DataSourceContractError(5007)。
- 两条探测路径**刻意不统一**（各自的成功判据与超时口径都不同，统一零收益）：
  - `_test_sso`：裸探 OIDC 发现端点，任意 2xx 即通（超时 = CONNECTION_TEST_TIMEOUT 10s）；
  - `_test_datasource`：按「Jonex标准拉取 API」**真契约**单页探测
    `{生效地址}/users?sync_type=full`（复用 IdentitySyncClient.probe_contract：
    200 + JSON 对象信封 + users 列表 + 逐条用户实体合规），超时 = 客户端的
    DEFAULT_TIMEOUT 30s。改前后者是裸 GET 根路径 + 任意 2xx 即通、完全不解析响应体，
    对「根路径返回 SPA HTML」的上游（如 Casdoor 门户）给**假绿灯**。
- 数据源测试连接成功时，`datasource_connection_verified_at` **只在本次探测用的 url 与
  token 都来自持久化配置**时才写（该列是波 3 放行门控的开关，不是展示字段）；
  审计不受此约束 —— 记的是用户动作，成功即记，两者解耦。
- 配置更新 / 测试连接写审计日志（ResourceType.IDENTITY_INTEGRATION）。

路由薄层只做解析请求 → extract_tenant_id → require_permission → 调本服务；
本服务是业务规则唯一归属。
"""
from __future__ import annotations

import logging
from datetime import datetime
from time import perf_counter
from typing import Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jonex_core.common.config import get_config
from jonex_core.common.crypto import decrypt_secret, encrypt_secret
from jonex_core.common.exceptions import (
    DataSourceAuthError,
    DataSourceConnectionError,
    InvalidParameterError,
    JonexException,
    ResourceNotFoundError,
    ServiceTimeoutError,
)
from jonex_core.common.tenant import require_tenant
from jonex_core.security.oidc import load_issuer_whitelist
from jonex_core.security.outbound_url_validator import (
    validate_datasource_url,
    validate_sso_url,
)

from capabilities.platform.dtos.identity_integration import (
    SECRET_MASK,
    DatasourceConfigResponse,
    DatasourceStatus,
    IntegrationConfigResponse,
    IntegrationConfigUpdateRequest,
    IntegrationStatusResponse,
    SsoConfigResponse,
    SsoRealmResponse,
    SsoStatus,
    TestConnectionRequest,
    TestConnectionResponse,
)
from capabilities.platform.models.audit_enums import AuditAction, LogType, ResourceType
from capabilities.platform.models.identity_integration import (
    IdentityIntegrationConfig,
    LoginPolicy,
)
from capabilities.platform.repository.tenant_realm_map_repository import (
    TenantRealmMapRepository,
)
from capabilities.platform.services.identity_sync_client import IdentitySyncClient

logger = logging.getLogger(__name__)

# 测试连接出站超时（秒）。
# ⚠️ 自波 2 起**只服务 `_test_sso`**（OIDC 发现端点是轻量 GET，10s 足够）。
# 数据源那条路走 `IdentitySyncClient.DEFAULT_TIMEOUT`（30s）——「测试连接的超时」与
# 「同步的超时」字面就是同一个常量，不变量由构造保证（谁改都不会漂），故**不注入**。
CONNECTION_TEST_TIMEOUT = 10.0

# 认证失败可判定的 HTTP 状态码。
_AUTH_FAILURE_STATUS = frozenset({401, 403})

# 「Jonex标准拉取 API」探测目标（与 IdentitySyncClient 的请求路径**同构**：
# `GET {base}/users?sync_type=full`）。测试连接只探这一条、**单页不翻页**。
_DATASOURCE_USERS_PATH = "/users"
_DATASOURCE_PROBE_SYNC_TYPE = "full"

# OIDC 发现端点后缀（探测目标 = 基址 + 此后缀）。
_WELL_KNOWN_PATH = "/.well-known/openid-configuration"

# JWKS 端点的**常量尾**：三档 ① 只剥掉它，必须**保留** `/realms/{realm}` 段，
# 否则探测会打到服务器根 —— 实测根路径 `/.well-known/openid-configuration` 返回 404
# （realm 路径同端点返回 200），404 会被 `_test_sso` 归成 5005，正是本需求要修的症状。
_JWKS_PATH_TAIL = "/protocol/openid-connect/certs"


def _realms_path(realm: str) -> str:
    """issuer 的 realm 路径段：``/realms/{realm}``。"""
    return f"/realms/{realm}"


def _issuer_from_jwks_uri(realm: str, jwks_uri: str) -> Optional[str]:
    """把白名单条目的 `jwks_uri` 还原成**后端视角的 issuer**（档 ① 的单条判定）。

    采用条件（两条都过，**绝不瞎猜**）：
    1. ``jwks_uri`` 以常量尾 ``/protocol/openid-connect/certs`` 结尾；
    2. 剥掉常量尾后得到的 URI path 以 ``/realms/{realm}`` 结尾 —— 这条护栏交叉核对
       「key 声明的 realm」与「value 里的 realm」是否一致，防白名单条目 key/value 配歪时
       把探测打到别的 realm 上。

    任一条不过 ⇒ 本档不成立（返回 None、由调用方回落下一档）：宁可回落，也不把格式异常的
    配置变成一次打到错误地址的出站。
    """
    if not jwks_uri.endswith(_JWKS_PATH_TAIL):
        return None
    issuer = jwks_uri[: -len(_JWKS_PATH_TAIL)].rstrip("/").strip()
    if not issuer:
        return None
    if not urlparse(issuer).path.rstrip("/").endswith(_realms_path(realm)):
        return None
    return issuer


def _issuer_from_issuer_whitelist(realm: Optional[str]) -> Optional[str]:
    """档 ①：用 realm 反查 KEYCLOAK_ISSUER_WHITELIST，从命中条目的 jwks_uri 反推 issuer。

    白名单的唯一实现是 :func:`jonex_core.security.oidc.load_issuer_whitelist`（本模块
    不自行解析 JSON，也不实例化验签器只为拿白名单）。

    先按 key 过滤：key（issuer URL）的 URI path 归一后等于 ``/realms/{realm}``（归一 =
    去掉尾部斜杠，故 ``.../realms/x/`` 与 ``.../realms/x`` 等价）；再逐条交给
    :func:`_issuer_from_jwks_uri` 做 value 侧校验（见其 docstring）。

    同 realm 多 key（异常配置）时按 key 字典序依次尝试以保证确定性，并 warning 记歧义；
    跳过其中 value 校验不过的条目（不让一条坏配置挡住同 realm 的好条目）。全部落空 → None。
    """
    if not realm:
        return None
    whitelist = load_issuer_whitelist()
    candidates = sorted(
        issuer
        for issuer in whitelist
        if urlparse(issuer).path.rstrip("/") == _realms_path(realm)
    )
    if not candidates:
        return None
    if len(candidates) > 1:
        logger.warning(
            "KEYCLOAK_ISSUER_WHITELIST 存在多个 issuer 指向同一 realm %s（歧义配置），"
            "按字典序取 %r",
            realm,
            candidates[0],
        )
    for issuer in candidates:
        backend_issuer = _issuer_from_jwks_uri(
            realm, whitelist[issuer].get("jwks_uri") or ""
        )
        if backend_issuer:
            return backend_issuer
    return None


def _resolve_sso_probe_issuer(realm: Optional[str], realm_row) -> Optional[str]:
    """三级解析 SSO 探测**基址**（统一 issuer 形态，含 ``/realms/{realm}`` 段）；全落空 → None。

    背景：同一列 ``tenant_realm_map.issuer_url`` 背负两个相反视角 —— 对外是浏览器视角的
    OIDC issuer 标识（本地 = ``http://localhost:8081/realms/...``，须与 Keycloak 签发的
    ``iss`` 逐字一致），而探测是容器内出站（本地 Docker 下 ``localhost:8081`` 指向 platform
    容器自身）。跨网络部署下不存在同时满足两者的单值，故探测目标改为三级解析：

    ① 白名单反查（按 ``realm`` 比对 key 的 URI path，再从 ``jwks_uri`` 剥常量尾）
       —— **容器内出站视角的真值来源**，与登录链路验签用的是同一份白名单；
    ② ``KEYCLOAK_SERVER_URL`` 补上 ``/realms/{realm}`` —— 它是**服务器根**形态（如
       ``http://keycloak:8080``），三档里唯一的异类，不补 realm 段就会打到根路径 → 实测 404；
    ③ ``row.issuer_url`` —— 现状兜底（该列本就是 issuer 形态；容器内未必可达，故只作末档，
       仅为不发生行为倒退）。

    三档产出**一律是 issuer 形态**，故调用方统一 ``issuer.rstrip("/") + /.well-known/...``。
    ``realm`` 由调用方传 ``row.realm``（DB 权威列，登录链路 ``realm_resolver`` 实际比对的
    就是它），**不要**传 ``realm_name_for_tenant`` 推导值。
    """
    issuer = _issuer_from_issuer_whitelist(realm)
    if issuer:
        return issuer
    server_url = getattr(get_config(), "KEYCLOAK_SERVER_URL", None)
    # realm 为空时补不出 realm 段，不能拿根地址充当 issuer（会 404）→ 直接看下一档。
    if realm and server_url and str(server_url).strip():
        return str(server_url).rstrip("/") + _realms_path(realm)
    issuer_url = getattr(realm_row, "issuer_url", None)
    if issuer_url and str(issuer_url).strip():
        return str(issuer_url)
    return None


def _build_probe_url(issuer: str) -> str:
    """issuer 形态基址 → 完整 OIDC 发现端点 URL。"""
    return issuer.rstrip("/") + _WELL_KNOWN_PATH


def _build_datasource_probe_url(base_url: str) -> str:
    """数据源生效地址 → 完整探测 URL ``{base}/users?sync_type=full``。

    与 :class:`IdentitySyncClient` 实际出站的地址**逐字同构**（它也是
    ``base_url.rstrip("/") + "/users"`` 加 ``sync_type`` 查询参数）—— 错误响应里回显的
    ``details["target"]`` 必须是**后端真正探了哪个地址**（含查询串），不是裸基址。
    """
    base = (base_url or "").rstrip("/")
    return f"{base}{_DATASOURCE_USERS_PATH}?sync_type={_DATASOURCE_PROBE_SYNC_TYPE}"


class IdentityIntegrationConfigService:
    """企业身份集成配置服务（配置聚合根业务规则唯一归属）。"""

    def __init__(
        self,
        session: AsyncSession,
        sso_transport: Optional[httpx.AsyncBaseTransport] = None,
        datasource_transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.session = session
        # SSO 出站探测的 transport 注入点（单测注入 httpx.MockTransport 模拟
        # well-known 响应，不依赖真实网络；生产缺省 None，走默认网络栈）。
        self._sso_transport = sso_transport
        # 数据源出站探测的 transport 注入点（与 `sso_transport` 同构：单测注入
        # MockTransport 模拟「Jonex标准拉取 API」响应，不依赖真实网络；生产缺省 None）。
        self._datasource_transport = datasource_transport

    # ---- 读取 ----

    async def _get_entity(self, tenant_id: str) -> Optional[IdentityIntegrationConfig]:
        stmt = select(IdentityIntegrationConfig).where(
            IdentityIntegrationConfig.tenant_id == tenant_id
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_or_create(self, tenant_id: str) -> IdentityIntegrationConfig:
        entity = await self._get_entity(tenant_id)
        if entity is None:
            entity = IdentityIntegrationConfig(tenant_id=tenant_id)
            self.session.add(entity)
        return entity

    @staticmethod
    def _to_config_response(entity: IdentityIntegrationConfig) -> IntegrationConfigResponse:
        """构造脱敏回显 DTO：token 恒为 `***`，不回显明文。"""
        return IntegrationConfigResponse(
            sso=SsoConfigResponse(
                enabled=bool(entity.sso_enabled),
                login_policy=entity.login_policy or LoginPolicy.AUTO_CREATE.value,
                last_verified_at=entity.sso_connection_verified_at,
                idp_hint_alias=entity.idp_hint_alias,
            ),
            datasource=DatasourceConfigResponse(
                name=entity.datasource_name,
                url=entity.datasource_url,
                auth_type=entity.auth_type,
                auto_sync=bool(entity.auto_sync_enabled),
                interval=entity.sync_interval,
                last_verified_at=entity.datasource_connection_verified_at,
            ),
        )

    async def get_config(self, tenant_id: str) -> IntegrationConfigResponse:
        """读取租户配置（脱敏）；未配置时返回默认态（不落库）。"""
        tenant_id = require_tenant(tenant_id)
        entity = await self._get_entity(tenant_id)
        if entity is None:
            entity = IdentityIntegrationConfig(tenant_id=tenant_id)
        return self._to_config_response(entity)

    async def get_idp_hint_alias(self, tenant_id: str) -> str:
        """读取生效的 kc_idp_hint alias（租户级覆盖 `or` 平台默认值）。

        供 SSO 登录发起点拼 authorize URL 用：有租户级覆盖值用覆盖值，否则回落
        平台默认值 KEYCLOAK_IDP_HINT_DEFAULT_ALIAS。**只读、不落库** —— 无 config
        行的租户（或列为 NULL）直接返回平台默认，零人工配置即可登录。

        公开方法：调用方不止本 service —— AuthService.oidc_authorize 拼授权 URL 时
        也要读它（jonex-rnyg 接线 kc_idp_hint），故不用下划线前缀。

        该键由配置层（KeycloakSettings.KEYCLOAK_IDP_HINT_DEFAULT_ALIAS）提供，
        此处 getattr 兜底：键缺失时用 `casdoor`（与 config.py 的默认值一致）。
        """
        entity = await self._get_entity(tenant_id)
        if entity is not None and entity.idp_hint_alias:
            return entity.idp_hint_alias
        return getattr(get_config(), "KEYCLOAK_IDP_HINT_DEFAULT_ALIAS", "casdoor")

    async def get_status(self, tenant_id: str) -> IntegrationStatusResponse:
        """统一展示（SSO 状态 + 主数据源状态 + 最近同步记录摘要）。

        最近同步记录归同步域本配置域返回 None；connection_status 由
        datasource_url / datasource_connection_verified_at 推导。
        """
        tenant_id = require_tenant(tenant_id)
        entity = await self._get_entity(tenant_id)
        if entity is None:
            return IntegrationStatusResponse(
                sso=SsoStatus(enabled=False, provider="keycloak"),
                datasource=DatasourceStatus(connection_status="unconfigured"),
                latest_sync_record=None,
            )

        if not entity.datasource_url:
            connection_status = "unconfigured"
        elif entity.datasource_connection_verified_at:
            connection_status = "verified"
        else:
            connection_status = "unverified"

        return IntegrationStatusResponse(
            sso=SsoStatus(
                enabled=bool(entity.sso_enabled),
                provider="keycloak",
                login_policy=entity.login_policy or LoginPolicy.AUTO_CREATE.value,
                last_verified_at=entity.sso_connection_verified_at,
            ),
            datasource=DatasourceStatus(
                name=entity.datasource_name,
                connection_status=connection_status,
                auto_sync=bool(entity.auto_sync_enabled),
                interval=entity.sync_interval,
                last_sync_at=entity.last_sync_at,
                next_sync_at=entity.next_sync_at,
            ),
            latest_sync_record=None,
        )

    async def get_sso_realm(self, tenant_id: str) -> SsoRealmResponse:
        """tenant_realm_map 只读展示（服务地址/Realm/Client ID/回调地址/探测地址）。

        数据源为 tenant_realm_map（realm/issuer_url/client_id/status）+ 配置项
        KEYCLOAK_REDIRECT_URI（回调地址）；``realm`` 取 ``row.realm``（DB 权威列 ——
        登录链路 realm_resolver 实际比对的就是它，推导值仅在无行时才有意义，而那时本
        方法已经 1002 了）。

        ``effective_probe_target`` = 「测试连接」将实际探测的完整 probe_url，由
        :func:`_resolve_sso_probe_base` 三级解析后拼接；解析不出时填 ``None``。
        ⚠️ 本方法是**只读展示**：解析不出**不抛异常**（不该因为解析不出就让整个抽屉打不开），
        只有「无该租户行（Realm 未开通）」才 → ResourceNotFoundError（1002/404）。
        """
        tenant_id = require_tenant(tenant_id)
        row = await TenantRealmMapRepository(self.session).get_by_tenant(tenant_id)
        if row is None:
            raise ResourceNotFoundError(message="该租户尚未开通 SSO")
        issuer = _resolve_sso_probe_issuer(row.realm, row)
        redirect_uri = get_config().KEYCLOAK_REDIRECT_URI
        return SsoRealmResponse(
            realm=row.realm,
            issuer_url=row.issuer_url,
            client_id=row.client_id,
            redirect_uri=redirect_uri,
            status=row.status,
            effective_probe_target=_build_probe_url(issuer) if issuer else None,
        )

    # ---- 写入 ----

    async def upsert(
        self,
        tenant_id: str,
        req: IntegrationConfigUpdateRequest,
        username: Optional[str] = None,
    ) -> IntegrationConfigResponse:
        """更新配置（SSO 子配置 + 主数据源子配置，未传字段保持原值）。

        - Token「未改不传（None）/ 占位值 `***`」不覆盖原值。
        - 关键字段（接口地址 / Token / SSO 服务地址）变更使对应 verified_at 失效。
        - datasource_url 与 sso.issuer_url 保存前均做 SSRF 校验，非法抛
          InvalidTargetUrlError(1007)（与测试连接入口同档、同错误码）。
        - sso.issuer_url 落 tenant_realm_map（本仓该表**唯一写入点**）：以
          `get_by_tenant` 按 tenant_id 定位行（严禁按 realm / 裸 id 定位 ——
          realm_resolver 反查时不比对 row.tenant_id，realm 列是唯一被信任的键）。
          该租户无行（Realm 未开通）→ ResourceNotFoundError(1002)，**不 INSERT**：
          该表 client_id NOT NULL 无值可填，且 Keycloak 侧并无对应 realm，
          插行只会造出「配置看着齐了、实际登不进去」的假象。
        """
        tenant_id = require_tenant(tenant_id)
        entity = await self._get_or_create(tenant_id)

        if req.sso is not None:
            sso = req.sso
            if sso.issuer_url is not None:
                # 先定位本租户行再校验：无行是「未开通」（1002），优先级高于地址格式。
                realm_row = await TenantRealmMapRepository(self.session).get_by_tenant(
                    tenant_id
                )
                if realm_row is None:
                    raise ResourceNotFoundError(message="该租户尚未开通 SSO")
                # SSRF 校验（保存路径，与测试连接入口同档）；非法 scheme / 无主机名 /
                # 空串 → InvalidTargetUrlError(1007)。
                validate_sso_url(
                    sso.issuer_url,
                    allowed_hosts=get_config().identity_integration_allowed_hosts,
                )
                if sso.issuer_url != realm_row.issuer_url:
                    # 地址变了 → 之前的验证结论作废（前端「启用前必须已验证」护栏依赖）。
                    entity.sso_connection_verified_at = None
                realm_row.issuer_url = sso.issuer_url
            if sso.enabled is not None:
                entity.sso_enabled = sso.enabled
            if sso.login_policy is not None:
                entity.login_policy = sso.login_policy
            if sso.idp_hint_alias is not None:
                # 空串 = 清空覆盖、回落平台默认 → 落 NULL（NULL 语义见 update/036）。
                entity.idp_hint_alias = sso.idp_hint_alias or None

        if req.datasource is not None:
            ds = req.datasource
            if ds.name is not None:
                entity.datasource_name = ds.name
            if ds.url is not None:
                # 放行 http 与私网 / 回环（同机部署场景）；仅 scheme 非法 → 1007。
                validate_datasource_url(ds.url)
                if ds.url != entity.datasource_url:
                    entity.datasource_connection_verified_at = None
                entity.datasource_url = ds.url
            if ds.auth_type is not None:
                entity.auth_type = ds.auth_type
            if ds.token is not None and ds.token != SECRET_MASK:
                entity.token_ciphertext = encrypt_secret(ds.token)
                entity.datasource_connection_verified_at = None
            if ds.auto_sync is not None:
                entity.auto_sync_enabled = ds.auto_sync
            if ds.interval is not None:
                entity.sync_interval = ds.interval

        await self.session.flush()
        await self._audit(
            tenant_id,
            AuditAction.UPDATE_IDENTITY_INTEGRATION.value,
            username,
            resource_id=str(entity.id),
        )
        return self._to_config_response(entity)

    # ---- 测试连接 ----

    async def test_connection(
        self,
        tenant_id: str,
        req: TestConnectionRequest,
        username: Optional[str] = None,
    ) -> TestConnectionResponse:
        """测试连接（target=sso 或 datasource）。

        成功绑定对应 *_connection_verified_at（配置版本）；失败分类：
        连接失败 5005 / 认证失败 5006 / 超时 5004。
        """
        tenant_id = require_tenant(tenant_id)
        if req.target == "sso":
            return await self._test_sso(tenant_id, username)
        return await self._test_datasource(tenant_id, req, username)

    async def _test_sso(
        self,
        tenant_id: str,
        username: Optional[str] = None,
    ) -> TestConnectionResponse:
        """SSO 连接测试：出站探测 OIDC 发现端点 + 错误分类 + 绑定验证时间。

        探测目标由 :func:`_resolve_sso_probe_issuer` **三级解析**（realm 取
        ``realm_row.realm`` 这个 DB 权威列）：① 按 realm 反查 KEYCLOAK_ISSUER_WHITELIST
        并取 jwks_uri 剥常量尾后的 issuer → ② KEYCLOAK_SERVER_URL 补 `/realms/{realm}`
        → ③ 行内 ``issuer_url``（现状兜底）；三档产出均为 issuer 形态，最终
        ``probe_url = 基址.rstrip("/") + /.well-known/openid-configuration``。
        ⚠️ realm 段不可削（实测根路径 404、realm 路径 200）。

        无该租户行（Realm 未开通）或**三档全部落空** → ResourceNotFoundError(1002/404)，
        与 get_sso_realm 对齐。⚠️ ``issuer_url`` 为空**本身不再单独构成** 1002 —— 只要
        ① 或 ② 能解析出基址就照探。

        出站前对**解析出的基址**（而非 issuer_url）做 validate_sso_url（SSRF 防御纵深）：
        校验真正出站的目标才是正确语义；与数据源同档，放行 http 与私网 / 回环 / 链路本地，
        仅 scheme 非法或无主机名 → 1007；命中 IDENTITY_INTEGRATION_ALLOWED_HOSTS 白名单的
        内网 Keycloak 跳过 scheme 与私网检查。成功仅表示端点可达，不校验凭据。
        """
        tenant_id = require_tenant(tenant_id)
        realm_row = await TenantRealmMapRepository(self.session).get_by_tenant(tenant_id)
        if realm_row is None:
            raise ResourceNotFoundError(message="该租户尚未开通 SSO")

        issuer = _resolve_sso_probe_issuer(realm_row.realm, realm_row)
        if not issuer:
            # 三档全落空 = 「无法确定探测地址」（未配置语义）；先判空再做 SSRF 校验，
            # 否则会被误判为 InvalidTargetUrlError(1007)「格式错误」。
            raise ResourceNotFoundError(message="无法确定 SSO 探测地址")

        # SSRF 校验（防御纵深，与数据源同档）；校验对象是真正出站的目标基址。
        validate_sso_url(
            issuer, allowed_hosts=get_config().identity_integration_allowed_hosts
        )

        probe_url = _build_probe_url(issuer)
        started = perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=CONNECTION_TEST_TIMEOUT,
                follow_redirects=False,
                transport=self._sso_transport,
            ) as client:
                resp = await client.get(probe_url)
        except httpx.TimeoutException as exc:
            raise ServiceTimeoutError(
                message="SSO 连接超时",
                details={"target": probe_url},
                cause=exc,
            ) from exc
        except httpx.NetworkError as exc:
            # DNS 解析失败 / TLS 失败 / 连接拒绝。
            raise DataSourceConnectionError(
                message="SSO 连接失败",
                details={"target": probe_url},
                cause=exc,
            ) from exc
        latency_ms = int((perf_counter() - started) * 1000)

        if 200 <= resp.status_code < 300:
            ok = True
            message = "端点可达性检查通过"
        elif resp.status_code in _AUTH_FAILURE_STATUS:
            raise DataSourceAuthError(
                message="SSO 认证失败",
                details={"target": probe_url, "status_code": resp.status_code},
            )
        else:
            raise DataSourceConnectionError(
                message="SSO 连接失败",
                details={"target": probe_url, "status_code": resp.status_code},
            )

        # 成功：绑定验证时间。
        entity = await self._get_or_create(tenant_id)
        entity.sso_connection_verified_at = datetime.now()
        await self.session.flush()
        await self._audit(
            tenant_id,
            AuditAction.TEST_IDENTITY_INTEGRATION_CONNECTION.value,
            username,
            resource_id=str(entity.id),
        )
        return TestConnectionResponse(ok=ok, latency_ms=latency_ms, message=message)

    async def _test_datasource(
        self,
        tenant_id: str,
        req: TestConnectionRequest,
        username: Optional[str] = None,
    ) -> TestConnectionResponse:
        """主数据源连接测试：按「Jonex标准拉取 API」**真契约**单页探测 + 绑定验证时间。

        探测目标 = ``{生效地址}/users?sync_type=full``，由
        :meth:`IdentitySyncClient.probe_contract` 承载：**只发一次请求、绝不翻页**
        （即便信封自称 ``has_more=true`` 也不沿 ``next_cursor`` 续拉 —— 否则「测试
        连接」会变成一次真实全量拉取），成功判据 = HTTP 200 + JSON 对象信封 +
        ``users``（兼容 ``data``）列表 + 列表中**每一条**用户实体都过 ``_parse_user``
        校验；第一页有一条不合规即整体失败。

        ⚠️ 改前是裸 GET **根路径** + 任意 2xx 即通、且完全不解析响应体 —— 对「根路径
        返回 SPA HTML」的上游（如 Casdoor 门户）会给出**假绿灯**：测试连接通过，紧接着
        「立即同步」（同一条 ``/users`` 契约）必然 5007。本次改造即治此症，故存量
        「测试连接」会**由绿转红**，**不是回归**。

        生效地址：``req.datasource.url`` 快照优先，缺则回落持久化实体的
        ``datasource_url``（回落顺序不变）；token 同构（快照占位值 ``***`` 不算快照，
        回落库内密文解密）。明文只在内存中拼 ``Bearer``。

        超时：构造 ``IdentitySyncClient`` **不传 timeout**，吃它自己的
        ``DEFAULT_TIMEOUT``（30s）—— 与同步链路字面同一个常量。代价：按钮 loading
        最长 ~60.5s（连接失败重试 1 次 + 0.5s 退避；读超时不重试）。
        ⛔ SSO 那条路的 ``CONNECTION_TEST_TIMEOUT``(10s) 与这里无关，两条路径不通用。

        失败分类（**全部带 `details["target"]` = 完整探测 URL**，供前端回显定位）：

        - 401 / 403 → ``DataSourceAuthError``(5006)；
        - 5xx / 连接失败 / DNS / TLS / 其余非 2xx（含 3xx）
          → ``DataSourceConnectionError``(5005)；
        - **400** 与「非 JSON / 信封非对象 / 缺 users 列表 / 任一用户实体不符」
          → ``DataSourceContractError``(5007)；
        - 读 / 写超时 → ``ServiceTimeoutError``(5004)；
        - 生效地址为空 → ``InvalidParameterError``(1001)；
          地址非法 → ``InvalidTargetUrlError``(1007)。

        ``datasource_connection_verified_at`` **只在本次探测用的 url 与 token 都来自
        持久化配置时**才写：该列是波 3 放行门控的开关而非展示字段，判据必须从「目标像
        不像库里的」提到「**这次验证的确实是库里那套配置**」—— 否则「地址不动只填新
        token」「地址没保存」这类路径会验证出一个同步链路根本用不上的结论（假绿灯翻版）。
        审计不受此约束：审计记的是**用户动作**，成功即记，与是否写该列解耦。
        """
        # 快照是否真的带上了值（`***` 占位 = 用户没改 token = 不算快照）。
        used_snapshot_url = False
        used_snapshot_token = False
        url: Optional[str] = None
        token_plain: Optional[str] = None

        if req.datasource is not None:
            if req.datasource.url is not None:
                url = req.datasource.url
                used_snapshot_url = True
            if req.datasource.token is not None and req.datasource.token != SECRET_MASK:
                token_plain = req.datasource.token
                used_snapshot_token = True

        # 请求快照未提供时回落到已持久化配置。
        entity = await self._get_entity(tenant_id)
        if url is None and entity is not None:
            url = entity.datasource_url
        if url is None:
            raise InvalidParameterError(
                message="请先配置主数据源接口地址",
                details={"target": req.target},
            )
        if token_plain is None and entity is not None and entity.token_ciphertext:
            token_plain = decrypt_secret(entity.token_ciphertext)

        # SSRF 校验（保存路径之外的二次闸门，测试连接入口也强制校验）。
        validate_datasource_url(url)

        probe_url = _build_datasource_probe_url(url)
        client = IdentitySyncClient(
            url,
            token_plain or "",
            transport=self._datasource_transport,
        )
        try:
            latency_ms = await client.probe_contract()
        except JonexException as exc:
            # 客户端不知道自己在被谁探测：就地补上探测目标后**原样上抛**（保 code /
            # message 显式性 / params / cause）。⛔ 绝不重建异常（`message=` 会把
            # `_message_explicit` 置真 ⇒ 全局处理器第 ② 分支跳过 i18n，英文用户拿到
            # 写死的中文）。
            exc.details = {**exc.details, "target": probe_url}
            raise
        finally:
            # probe_contract 的约定：调用方负责归还连接。
            await client.aclose()

        # 成功：仅当这次验证的就是库里的那套配置，才刷新配置版本验证凭证。
        verified_entity = await self._get_or_create(tenant_id)
        if not (used_snapshot_url or used_snapshot_token):
            verified_entity.datasource_connection_verified_at = datetime.now()
        await self.session.flush()
        # 审计：记用户动作，成功即记（与是否写 verified_at 无关）。
        await self._audit(
            tenant_id,
            AuditAction.TEST_IDENTITY_INTEGRATION_CONNECTION.value,
            username,
            resource_id=str(verified_entity.id),
        )
        return TestConnectionResponse(
            ok=True, latency_ms=latency_ms, message="连接成功"
        )

    # ---- 审计 ----

    async def _audit(
        self,
        tenant_id: str,
        action: str,
        username: Optional[str] = None,
        resource_id: Optional[str] = None,
    ) -> None:
        """写审计日志（惰性导入避免启动循环依赖；失败不阻塞业务）。"""
        try:
            from capabilities.platform.services.audit_log_service import AuditLogService

            audit_svc = AuditLogService(self.session)
            await audit_svc.record(
                tenant_id=tenant_id,
                log_type=LogType.OPERATION.value,
                action=action,
                resource=ResourceType.IDENTITY_INTEGRATION.value,
                resource_id=resource_id,
                username=username,
                sync=True,
            )
        except Exception:
            logger.warning("企业身份集成配置审计日志写入失败（不阻塞业务）")


__all__ = ["IdentityIntegrationConfigService"]
