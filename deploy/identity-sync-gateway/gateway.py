#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""身份同步侧车 —— Jonex标准拉取契约 ⇄ Casdoor Admin API 的「翻译官」。

为什么需要它
------------
平台侧 `capabilities/platform/services/identity_sync_client.py` 只会打
``GET {base}/users?sync_type=full|incremental[&cursor=X]`` 并期待一封标准信封；
而真实上游（Casdoor 门户）**不实现**这个契约 —— 直接指过去只会拿到 SPA HTML，
同步必然抛 ``DataSourceContractError``(5007)。

本服务对外讲**Jonex标准契约**、对内调 **Casdoor Admin API**，是二者之间唯一的
适配层。平台按 compose 服务名寻址：``http://identity-sync-gateway:8099``。

契约要点（⚠️ 持久化游标取**顶层** ``cursor`` 键，不是 ``next_cursor`` ——
回错键名平台会存下 ``null`` ⇒ 增量**静默退化**成全量且不报错）
----------------------------------------------------------------------
::

    GET /health                                      → 200 {"status":"ok"}
    GET /users?sync_type=full                        → 200 + 信封
    GET /users?sync_type=incremental[&cursor=<b64url>] → 200 + 信封
    其余路径 / 方法                                    → 404

信封::

    {
      "users": [ {external_user_id, username, status∈{active,inactive}, name?, email?} ],
      "has_more": false,
      "next_cursor": null,     # 仅 has_more=true 时必填；本实现单页返回，恒 null
      "cursor": "..."          # 仅增量：本次应持久化的新游标（最后一页生效）
    }

⚠️ **持久化游标是顶层 ``cursor``，不是 ``next_cursor``**（后者只用于翻页）。
若回错键名，平台会存下 ``null`` ⇒ 每轮从头拉、增量静默退化成全量、且不报错。

鉴权
----
``Authorization: Bearer <DATASOURCE_GATEWAY_TOKEN>``，用 ``hmac.compare_digest``
做等时比较，失败一律 **401**（不要 400/500 —— 会被平台误分类成契约错误/连接错误）。
``/health`` 不鉴权（供容器 healthcheck）。

增量语义（Casdoor 无原生游标）
------------------------------
Casdoor ``/api/get-users`` 没有 filtering 参数，只能用 ``updatedTime`` **时间窗近似**：

- 游标 = **base64url 不透明串**（平台只存只回传，格式可自由演进），解出 ``{"t": <epoch 秒>}``
- 请求不带 ``cursor`` ⇒ 回全量（等价 ``always_full``）
- 新游标 = **请求开始时刻（epoch 秒）向前回退 2 秒**（overlap：覆盖「上一次同步完成」与
  「本次请求开始」之间的写入竞态，代价只是少量重复 —— 平台的 upsert 幂等）
- ``isDeleted`` 软删条目不返回
- ``DATASOURCE_GATEWAY_INCREMENTAL_MODE=always_full`` ⇒ 忽略入参 cursor，永远回全量

字段映射
--------
**只挑 5 个契约字段**（``external_user_id`` / ``username`` / ``name`` / ``email`` / ``status``），
由 ``_to_contract()`` **逐字段构造新 dict**。⛔ 绝不整体透传 Casdoor 用户对象 ——
它带着 ``passwordSalt`` / ``passwordType`` 等敏感项。

安全 / 健壮性（相对早期本地桩的六处必修）
---------------------------------------
1. 强制 Bearer 校验（桩完全没有）
2. 真实增量游标（桩恒回 ``"mock-cursor-1"``）
3. ⛔ 不打印 ``Authorization`` 头（桩会整条打进日志 —— 等于把 token 写进日志文件）
4. 请求路径的失败一律 **5xx 响应**，绝不用 ``raise SystemExit``（那会杀掉进程）
5. session cookie **缓存**（桩每个请求都重新登录一次 Casdoor）
6. ``GET /health``

可测性
------
- **模块导入零副作用**：不读 env、不发网络、不绑端口，env 只在 ``main()`` 里读
- 上游拉取抽成模块级函数 ``fetch_upstream_users(cfg)``，测试 monkeypatch 它
- ``make_server(host, port, cfg)`` 纯工厂，测试可绑 0 端口起真 HTTP 服务

用法
----
::

    # 容器（推荐，见 deploy/docker-compose.yml 的 identity-sync-gateway 服务）
    docker compose --profile identity-sync-gateway up -d

    # 本地直跑（凭据取进程环境变量，缺文件不报错）
    DATASOURCE_GATEWAY_TOKEN=... MOCK_DATASOURCE_BASE_URL=... \\
    MOCK_DATASOURCE_OWNER=... MOCK_DATASOURCE_ADMIN_USER=... MOCK_DATASOURCE_ADMIN_PASSWORD=... \\
    python3 deploy/identity-sync-gateway/gateway.py --port 8099
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("identity-sync-gateway")

# ── 常量 ─────────────────────────────────────────────────────────────────────

_FETCH_TYPES = ("full", "incremental")
_INCREMENTAL_MODES = ("time_window", "always_full")

_DEFAULT_PORT = 8099
_DEFAULT_PAGE_SIZE = 100
_DEFAULT_INCREMENTAL_MODE = "time_window"

#: 新游标相对「请求开始时刻」的回退秒数（overlap，见模块 docstring）
_CURSOR_OVERLAP_SEC = 2

#: 防御性上限：页满即继续，撞上异常上游不至于死循环
_MAX_PAGES = 50

#: 单次上游 HTTP 调用超时（秒）
_UPSTREAM_TIMEOUT = 15.0
_LOGIN_TIMEOUT = 10.0

#: session cookie 缓存 TTL（秒）；过期即重新登录
_SESSION_TTL = 600.0

#: 必填配置键（缺失 → 启动即退出，非 0 码）
_REQUIRED_KEYS = (
    "DATASOURCE_GATEWAY_TOKEN",
    "MOCK_DATASOURCE_BASE_URL",
    "MOCK_DATASOURCE_OWNER",
    "MOCK_DATASOURCE_ADMIN_USER",
    "MOCK_DATASOURCE_ADMIN_PASSWORD",
)


# ── 异常 ─────────────────────────────────────────────────────────────────────


class ConfigError(Exception):
    """配置缺失或非法（只在启动期抛出）。"""


class UpstreamError(Exception):
    """上游（Casdoor）不可达 / 非 ok / 分页越界 —— 由请求处理器转成 5xx。"""


class _SessionExpired(Exception):
    """内部信号：上游拒绝了当前 session cookie，值得重新登录一次。"""


# ── 配置 ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GatewayConfig:
    """侧车运行配置（纯数据，构造它不产生任何副作用）。"""

    token: str
    upstream_base_url: str
    owner: str
    admin_user: str
    admin_password: str
    page_size: int = _DEFAULT_PAGE_SIZE
    usernames: Tuple[str, ...] = field(default_factory=tuple)
    incremental_mode: str = _DEFAULT_INCREMENTAL_MODE

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "GatewayConfig":
        """从映射构造配置；缺必填键抛 :class:`ConfigError`。

        ⛔ 本函数**不读** ``os.environ`` —— 由调用方（``main()``）决定来源，
        这样模块导入与测试构造都零副作用。
        """
        missing = [k for k in _REQUIRED_KEYS if not (env.get(k) or "").strip()]
        if missing:
            raise ConfigError("缺少必填配置：" + ", ".join(missing))

        mode = (env.get("DATASOURCE_GATEWAY_INCREMENTAL_MODE") or "").strip()
        if not mode:
            mode = _DEFAULT_INCREMENTAL_MODE
        if mode not in _INCREMENTAL_MODES:
            raise ConfigError(
                f"DATASOURCE_GATEWAY_INCREMENTAL_MODE 非法：{mode!r}"
                f"（可选 {' / '.join(_INCREMENTAL_MODES)}）"
            )

        raw_page_size = (env.get("MOCK_DATASOURCE_PAGE_SIZE") or "").strip()
        try:
            page_size = int(raw_page_size) if raw_page_size else _DEFAULT_PAGE_SIZE
        except ValueError as exc:
            raise ConfigError(f"MOCK_DATASOURCE_PAGE_SIZE 非法：{raw_page_size!r}") from exc
        if page_size <= 0:
            raise ConfigError(f"MOCK_DATASOURCE_PAGE_SIZE 必须为正：{page_size}")

        usernames = tuple(
            n.strip()
            for n in (env.get("MOCK_DATASOURCE_USERNAMES") or "").split(",")
            if n.strip()
        )

        return cls(
            token=env["DATASOURCE_GATEWAY_TOKEN"].strip(),
            upstream_base_url=env["MOCK_DATASOURCE_BASE_URL"].strip().rstrip("/"),
            owner=env["MOCK_DATASOURCE_OWNER"].strip(),
            admin_user=env["MOCK_DATASOURCE_ADMIN_USER"].strip(),
            admin_password=env["MOCK_DATASOURCE_ADMIN_PASSWORD"],
            page_size=page_size,
            usernames=usernames,
            incremental_mode=mode,
        )


def _load_env_file(path: str) -> Dict[str, str]:
    """极简 ``.env`` 解析（stdlib；不为一个侧车引入 python-dotenv）。找不到就当空。"""
    out: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip().strip('"').strip("'")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        pass
    return out


# ── 鉴权 ─────────────────────────────────────────────────────────────────────


def _constant_time_eq(candidate: str, expected: str) -> bool:
    """等时字符串比较，长度不等直接 False（不泄露长度，也不抛 TypeError）。

    ⚠️ ``hmac.compare_digest`` 对**非 ASCII 的 str** 会抛 ``TypeError`` —— 这里统一
    编码成 bytes 再比，bytes 分支任何长度都不会抛。
    """
    cb = candidate.encode("utf-8")
    eb = expected.encode("utf-8")
    if len(cb) != len(eb):
        # 仍然做一次等时比较，避免「先比长度」引入的时序差异
        hmac.compare_digest(hashlib.sha256(cb).digest(), hashlib.sha256(eb).digest())
        return False
    return hmac.compare_digest(cb, eb)


def _bearer_ok(header_value: Optional[str], expected_token: str) -> bool:
    """校验 ``Authorization: Bearer <token>``（scheme 大小写不敏感）。"""
    if not header_value:
        return False
    scheme, sep, credentials = header_value.partition(" ")
    if not sep or scheme.lower() != "bearer":
        return False
    return _constant_time_eq(credentials.strip(), expected_token)


# ── 上游：登录 + session cookie 缓存 ─────────────────────────────────────────

_SESSION_LOCK = threading.Lock()
#: key -> (cookie, 过期时刻)
_SESSION_CACHE: Dict[str, Tuple[str, float]] = {}


def reset_session_cache() -> None:
    """清空 session 缓存（测试用；运行期用不到）。"""
    with _SESSION_LOCK:
        _SESSION_CACHE.clear()


def _session_key(cfg: GatewayConfig) -> str:
    return f"{cfg.upstream_base_url}|{cfg.owner}|{cfg.admin_user}"


def _login(cfg: GatewayConfig) -> str:
    """``POST /api/login`` → ``casdoor_session_id`` cookie 串。

    ⛔ 不能用 client_credentials：该 app 没配此 grant（2026-09-29 实测）。
    """
    body = json.dumps(
        {
            "type": "login",
            "application": "app-built-in",
            "organization": "built-in",
            "username": cfg.admin_user,
            "password": cfg.admin_password,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        cfg.upstream_base_url + "/api/login",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_LOGIN_TIMEOUT) as resp:
            cookies = resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as exc:
        raise UpstreamError(f"上游登录失败：HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise UpstreamError(f"上游登录失败：连不上 {cfg.upstream_base_url}（{exc}）") from exc

    for cookie in cookies:
        if cookie.startswith("casdoor_session_id="):
            return cookie.split(";", 1)[0]
    raise UpstreamError("上游登录响应里没有 casdoor_session_id（口令或端点不对？）")


def _get_session(cfg: GatewayConfig) -> str:
    """取缓存的 session cookie；未命中或已过期才真正登录。

    源桩每个请求都登录一次 —— 对 Casdoor 是无谓的连接与口令校验压力，
    也让增量同步这种连续请求更容易撞上上游限流。
    """
    key = _session_key(cfg)
    now = time.monotonic()
    with _SESSION_LOCK:
        cached = _SESSION_CACHE.get(key)
        if cached and cached[1] > now:
            return cached[0]

    cookie = _login(cfg)  # 网络 I/O 在锁外做（并发双登录幂等，可接受）
    with _SESSION_LOCK:
        _SESSION_CACHE[key] = (cookie, time.monotonic() + _SESSION_TTL)
    return cookie


# ── 上游：拉取 ───────────────────────────────────────────────────────────────


def _upstream_get_json(cfg: GatewayConfig, path_and_query: str, session: str) -> Dict[str, Any]:
    req = urllib.request.Request(
        cfg.upstream_base_url + path_and_query,
        headers={"Cookie": session, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_UPSTREAM_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # session 可能过期：上层捕获后重登一次再试
            raise _SessionExpired(f"上游返回 HTTP {exc.code}") from exc
        raise UpstreamError(f"上游 get-users 返回 HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise UpstreamError(f"连不上上游 {cfg.upstream_base_url}：{exc}") from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise UpstreamError("上游返回的不是 JSON（门户地址？SPA HTML？）") from exc
    if not isinstance(payload, dict):
        raise UpstreamError("上游返回的 JSON 顶层不是对象")
    return payload


def _fetch_pages(cfg: GatewayConfig, session: str) -> List[Dict[str, Any]]:
    """分页拉全量**原始** Casdoor 用户对象（未做字段映射）。"""
    users: List[Dict[str, Any]] = []
    page = 1
    while True:
        query = urllib.parse.urlencode(
            {"owner": cfg.owner, "p": page, "pageSize": cfg.page_size}
        )
        payload = _upstream_get_json(cfg, "/api/get-users?" + query, session)
        if payload.get("status") != "ok":
            raise UpstreamError(
                f"上游 get-users 非 ok：{payload.get('msg')!r}"
                f"（owner={cfg.owner!r} 是否有权读？）"
            )
        rows = payload.get("data") or []
        # isDeleted 是软删标记（deletedTime 非空）：已删用户不该出现在上游快照里
        users.extend(u for u in rows if isinstance(u, dict) and not u.get("isDeleted"))
        if len(rows) < cfg.page_size:
            return users
        page += 1
        if page > _MAX_PAGES:
            raise UpstreamError(f"分页超过上限 {_MAX_PAGES}，疑似上游异常")


def fetch_upstream_users(cfg: GatewayConfig) -> List[Dict[str, Any]]:
    """拉取上游用户（**原始 Casdoor 对象**，未映射、未过滤）。

    测试 monkeypatch 本函数即可完全离线驱动整条链路。
    session 过期（上游回 401/403）时自动重登一次再试。

    Raises:
        UpstreamError: 上游不可达 / 非 ok / 非法 JSON / 分页越界。
    """
    session = _get_session(cfg)
    try:
        return _fetch_pages(cfg, session)
    except _SessionExpired:
        logger.info("上游 session 已失效，重新登录后重试一次")
        with _SESSION_LOCK:
            _SESSION_CACHE.pop(_session_key(cfg), None)
        return _fetch_pages(cfg, _get_session(cfg))


# ── 字段映射 / 过滤 ──────────────────────────────────────────────────────────


def _to_contract(raw: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    """Casdoor User → Jonex契约实体（**逐字段构造**，杜绝敏感字段透传）。

    ⛔ Casdoor 用户对象带 ``passwordSalt`` / ``passwordType`` 等敏感项，
    整体透传等于把它们写进平台库和同步日志里。这里只取 5 个契约字段。

    返回 ``None`` 表示该行缺 ``id`` / ``name``（不是可用身份）—— 平台侧
    ``_parse_user`` 对缺字段是**抛异常**（整次同步判 5007 失败），
    丢掉单个坏行比拖垮整轮同步更合理（会记 WARNING）。
    """
    ext_id = raw.get("id")
    username = raw.get("name")
    if not isinstance(ext_id, str) or not ext_id:
        logger.warning("跳过缺 id 的上游用户：%r", raw.get("name"))
        return None
    if not isinstance(username, str) or not username:
        logger.warning("跳过缺 name 的上游用户：id=%r", ext_id)
        return None
    email = raw.get("email")
    display = raw.get("displayName")
    return {
        "external_user_id": ext_id,
        # Casdoor 无 preferred_username，登录名在 name
        "username": username,
        # 展示名在 displayName（如「周浩」），空则回落登录名
        "name": display if isinstance(display, str) and display else username,
        "email": email if isinstance(email, str) else "",
        # Casdoor 没有 status 字段，用 isForbidden 推导
        "status": "inactive" if raw.get("isForbidden") else "active",
    }


def _filter_username_whitelist(
    rows: Sequence[Mapping[str, Any]], cfg: GatewayConfig
) -> List[Mapping[str, Any]]:
    """按 ``MOCK_DATASOURCE_USERNAMES`` 白名单收窄（空 = 全量）。"""
    if not cfg.usernames:
        return list(rows)
    allow = set(cfg.usernames)
    return [r for r in rows if r.get("name") in allow]


#: 上游时间戳字段（Casdoor：毫秒或 ISO-8601 字符串，两种都实测见过）
_UPDATED_TIME_FIELD = "updatedTime"


def _parse_updated_time(raw: Any) -> Optional[float]:
    """把上游 ``updatedTime`` 归一成 epoch 秒；无法解析返回 ``None``。

    兼容：int/float（>1e11 视为毫秒）、数字字符串、ISO-8601 字符串（含 ``Z``）。
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        try:
            value = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    else:
        return None
    # 毫秒时间戳（Casdoor 常见）→ 秒
    return value / 1000.0 if value > 1e11 else value


def _filter_since(rows: Sequence[Mapping[str, Any]], since_epoch: float) -> List[Mapping[str, Any]]:
    """保留 ``updatedTime >= since_epoch`` 的行。

    ``updatedTime`` 缺失或无法解析的行**保留**（保守：宁可多同步一条幂等 upsert，
    也不要因为上游少给一个字段就静默漏掉一个人）。
    """
    kept: List[Mapping[str, Any]] = []
    for row in rows:
        ts = _parse_updated_time(row.get(_UPDATED_TIME_FIELD))
        if ts is None or ts >= since_epoch:
            kept.append(row)
    return kept


# ── 游标（base64url 不透明串）────────────────────────────────────────────────


def encode_cursor(epoch_seconds: float) -> str:
    """``{"t": <epoch 秒>}`` → base64url（去 padding），对平台**不透明**。"""
    payload = json.dumps({"t": int(epoch_seconds)}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: Optional[str]) -> Optional[int]:
    """base64url → epoch 秒；空 / 非法一律返回 ``None``（= 回退全量）。"""
    if not cursor:
        return None
    text = cursor.strip()
    if not text:
        return None
    padding = "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(text + padding)
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        logger.warning("游标无法解析，本轮回退为全量：%r", text)
        return None
    if not isinstance(payload, dict):
        logger.warning("游标内容不是对象，本轮回退为全量")
        return None
    value = payload.get("t")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning("游标缺 t 字段，本轮回退为全量")
        return None
    return int(value)


# ── HTTP 处理器 ──────────────────────────────────────────────────────────────


class IdentitySyncGatewayHandler(BaseHTTPRequestHandler):
    """Jonex契约端点。配置从 ``self.server.cfg`` 取（不用类属性全局态）。"""

    server_version = "IdentitySyncGateway/1.0"
    protocol_version = "HTTP/1.1"

    # -- 路由 ---------------------------------------------------------------
    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler 约定)
        request_started = time.time()  # 新游标的基准时刻：必须早于任何上游调用
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        # 路径优先于鉴权：未知路径恒 404（含未带 token），不泄露「路径存在与否」
        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if path != "/users":
            self._send_json(404, {"error": "not found", "path": parsed.path})
            return

        cfg: GatewayConfig = self.server.cfg
        # ⛔ 不记录 Authorization 头（早期桩把它整条打进了日志 = token 落盘）
        if not _bearer_ok(self.headers.get("Authorization"), cfg.token):
            logger.warning("鉴权失败：GET %s", parsed.path)
            self._send_json(401, {"error": "unauthorized"})
            return

        query = urllib.parse.parse_qs(parsed.query)
        sync_type = (query.get("sync_type") or [""])[0]
        if sync_type not in _FETCH_TYPES:
            self._send_json(400, {"error": "bad sync_type", "got": sync_type})
            return

        try:
            rows = fetch_upstream_users(cfg)
        except UpstreamError as exc:
            logger.warning("上游拉取失败：%s", exc)
            self._send_json(502, {"error": "upstream_unavailable", "detail": str(exc)})
            return
        except SystemExit as exc:
            # 防御：上游适配代码误用 SystemExit 也不许杀掉进程（旧桩就是这个写法）
            logger.error("上游适配代码抛了 SystemExit（已拦截）：%s", exc)
            self._send_json(502, {"error": "upstream_error", "detail": str(exc)})
            return
        except Exception:  # noqa: BLE001 - 兜底：任何异常都要变成 5xx，不能让连接裸断
            logger.exception("上游拉取出现未预期异常")
            self._send_json(500, {"error": "internal_error"})
            return

        # 软删条目再兜一层：删除的人不能被同步成 active（那会把人「复活」）
        rows = [r for r in rows if not r.get("isDeleted")]
        rows = _filter_username_whitelist(rows, cfg)
        cursor_in = (query.get("cursor") or [None])[0]

        if sync_type == "full":
            body: Dict[str, Any] = {
                "users": self._map_users(rows),
                "has_more": False,
                "next_cursor": None,
            }
        else:
            since = None
            if cfg.incremental_mode == "time_window":
                since = decode_cursor(cursor_in)  # None ⇒ 无游标 ⇒ 全量
            if since is not None:
                rows = _filter_since(rows, since)
            body = {
                "users": self._map_users(rows),
                "has_more": False,
                "next_cursor": None,
                # ⚠️ 持久化游标 = 顶层 cursor（不是 next_cursor）
                "cursor": encode_cursor(request_started - _CURSOR_OVERLAP_SEC),
            }

        logger.info(
            "GET /users sync_type=%s mode=%s users=%d",
            sync_type,
            cfg.incremental_mode,
            len(body["users"]),
        )
        self._send_json(200, body)

    def do_HEAD(self):  # noqa: N802
        self._not_found(send_body=False)

    def do_POST(self):  # noqa: N802
        self._not_found()

    def do_PUT(self):  # noqa: N802
        self._not_found()

    def do_PATCH(self):  # noqa: N802
        self._not_found()

    def do_DELETE(self):  # noqa: N802
        self._not_found()

    # -- 工具 ---------------------------------------------------------------
    @staticmethod
    def _map_users(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
        """逐条映射并丢掉字段不全的坏行（见 ``_to_contract``）。"""
        out: List[Dict[str, str]] = []
        for row in rows:
            mapped = _to_contract(row)
            if mapped is not None:
                out.append(mapped)
        return out

    def _not_found(self, send_body: bool = True) -> None:
        parsed = urllib.parse.urlparse(self.path)
        self._send_json(404, {"error": "not found", "path": parsed.path}, send_body=send_body)

    def _send_json(self, code: int, obj: Dict[str, Any], send_body: bool = True) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if send_body:
            self.wfile.write(raw)

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音 stderr 逐请求噪音
        logger.debug("%s - %s", self.address_string(), fmt % args)


# ── 服务工厂 / 入口 ──────────────────────────────────────────────────────────


class IdentitySyncGatewayServer(ThreadingHTTPServer):
    """带配置的 ThreadingHTTPServer（daemon 线程，ctrl-c 不卡）。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], cfg: GatewayConfig) -> None:
        self.cfg = cfg
        super().__init__(address, IdentitySyncGatewayHandler)


def make_server(host: str, port: int, cfg: GatewayConfig) -> IdentitySyncGatewayServer:
    """构造（并绑定）服务器。测试传 ``port=0`` 取随机空闲端口。"""
    return IdentitySyncGatewayServer((host, port), cfg)


def _build_parser() -> "argparse.ArgumentParser":  # noqa: F821 - 延迟 import 见函数体
    import argparse

    parser = argparse.ArgumentParser(description="身份同步侧车（Jonex契约 → Casdoor Admin API）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0，容器才够得着）")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"监听端口（默认取 DATASOURCE_GATEWAY_PORT，再默认 {_DEFAULT_PORT}）",
    )
    parser.add_argument(
        "--env-file",
        default="deploy/.env",
        help="配置兜底来源（默认 deploy/.env，按 cwd 解析；进程环境变量优先；找不到就当空）",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    """进程入口：**唯一**读 env / 绑端口 / 发网络的地方。"""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    args = _build_parser().parse_args(argv)

    # 进程环境变量优先，其次 --env-file
    env = {**_load_env_file(args.env_file), **os.environ}
    try:
        cfg = GatewayConfig.from_env(env)
    except ConfigError as exc:
        raise SystemExit(f"[identity-sync-gateway] 配置错误：{exc}")  # 非 0 码退出

    port = args.port
    if port is None:
        raw_port = (env.get("DATASOURCE_GATEWAY_PORT") or "").strip()
        try:
            port = int(raw_port) if raw_port else _DEFAULT_PORT
        except ValueError as exc:
            raise SystemExit(
                f"[identity-sync-gateway] DATASOURCE_GATEWAY_PORT 非法：{raw_port!r}"
            ) from exc

    server = make_server(args.host, port, cfg)
    logger.info(
        "侧车已启动 http://%s:%s  上游=%s owner=%s 增量模式=%s 白名单=%s",
        args.host,
        server.server_address[1],
        cfg.upstream_base_url,
        cfg.owner,
        cfg.incremental_mode,
        ",".join(cfg.usernames) or "全量",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断，正在停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
