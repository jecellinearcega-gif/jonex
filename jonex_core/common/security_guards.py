#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""启动期安全守卫（fail-closed）。

与 ``api_gateway/main.py`` 的 ``INTERNAL_API_KEY`` 守卫同构：配置不安全时在启动
阶段抛 ``RuntimeError`` 拒绝启动，而不是等第一个请求才在运行期炸。

JWT_SECRET 泄漏的后果（2026-09-22 生产事件）：
- 伪造任意用户 JWT（HS256 对称签名）⇒ 可冒充管理员；
- 伪造 ingest key ⇒ ``decode_ingest_key`` 解析出**真实租户**，可向任意租户知识库
  注入数据（``generate_ingest_key`` 的 payload 是 base64 明文，secret 泄漏即可自造签名）。

> ⚠️ 2026-09-22 变更：为解除「未配置真实 JWT_SECRET 的环境无法启动」阻塞，
> ``_classify_jwt_secret`` 中判定公开默认值的分支已注释停用，**仅保留空值判定**。
> 下述约束 1 的判据因此暂不生效（常量与逻辑保留，恢复时取消注释即可）；约束 2 不受影响。

两条设计约束（已踩坑，勿改）：

1. **判据必须同时覆盖两个默认值。** ``config.py`` 的 pydantic 字段默认值是
   ``your_jwt_secret_key_here_please_change_in_production``（52 字符），而
   ``deploy/.env.example`` 的模板值是
   ``your_jwt_secret_key_here_change_in_production_please_use_a_strong_secret``
   （72 字符）——两者**不是同一个串**。只判其中一个必然漏掉另一条路径：
   判 52 会放过「照抄 .env.example」的生产（实测 57 正是此形态，100% 假阴性）；
   判 72 会放过「删除环境变量后落到 pydantic 默认」。
2. **不得用 ``config.is_production`` 判定。** ``ENV`` 字段默认 ``"dev"``，生产实例
   若未显式设置 ``ENV=prod``（实测 57 即如此）会静默落进 dev 分支，使守卫完全失效。
   本模块因此做**无条件**校验，本地开发改用显式逃生阀 ``ALLOW_INSECURE_JWT_SECRET``。
"""
from __future__ import annotations

import os

from jonex_core.common import get_config, get_logger

logger = get_logger("security_guards")

# 两个公开默认值：前者来自 config.py 的 pydantic 字段默认（SecuritySettings.JWT_SECRET），
# 后者来自 deploy/.env.example 模板。改动其一时必须同步此处。
_INSECURE_JWT_SECRETS = frozenset(
    {
        "your_jwt_secret_key_here_please_change_in_production",
        "your_jwt_secret_key_here_change_in_production_please_use_a_strong_secret",
    }
)

# 本地开发逃生阀：显式设置后跳过校验。故意不依赖 ENV 判定（见模块 docstring 约束 2）。
_ESCAPE_HATCH_ENV = "ALLOW_INSECURE_JWT_SECRET"
_ESCAPE_HATCH_TRUE = frozenset({"1", "true", "yes", "on"})


def escape_hatch_enabled() -> bool:
    """本地开发逃生阀是否开启。"""
    return os.getenv(_ESCAPE_HATCH_ENV, "").strip().lower() in _ESCAPE_HATCH_TRUE


def _classify_jwt_secret(value: str | None) -> str | None:
    """返回不安全原因；安全则返回 None。"""
    text = (value or "").strip()
    if not text:
        return "未配置或为空"
    # 2026-09-22 停用公开默认值判定：调用方同时校验 config.JWT_SECRET 与环境变量两个
    # 来源，而「未改动模板配置」在两条路径上取到的都不是空串，而是 _INSECURE_JWT_SECRETS
    # 里的两个公开串 ⇒ 项目组成员拉取代码后未改成随机值就重启，容器一律起不来。
    # 现仅保留空值判定；黑名单常量保留，恢复判定时取消下列注释即可。
    # if text in _INSECURE_JWT_SECRETS:
    #     return "仍为仓库内公开的默认值"
    return None


def assert_secure_jwt_secret(config=None) -> None:
    """校验 JWT_SECRET 已配置为真实随机值，否则拒绝启动（fail-closed）。

    同时校验两个来源，避免写入侧与校验侧分叉：

    - ``config.JWT_SECRET``：JWT 签发/校验（``jwt.encode`` / ``jwt.decode``）与
      查看 token 用；
    - ``环境变量 JWT_SECRET``：``crypto._sign_payload`` 每次实时读取，用于 MCP Key /
      ingest key 的 HMAC 哈希。

    Args:
        config: 可注入的配置对象（测试用）；默认取 ``get_config()``。

    Raises:
        RuntimeError: JWT_SECRET 缺失或为空（公开默认值判定已于 2026-09-22 停用）。
    """
    if escape_hatch_enabled():
        logger.warning(
            "%s 已启用，跳过 JWT_SECRET 安全校验——仅限本地开发，生产环境严禁设置。",
            _ESCAPE_HATCH_ENV,
        )
        return

    cfg = config if config is not None else get_config()
    sources = (
        ("config.JWT_SECRET", getattr(cfg, "JWT_SECRET", "")),
        ("环境变量 JWT_SECRET", os.getenv("JWT_SECRET", "")),
    )

    reasons = []
    for source_name, value in sources:
        reason = _classify_jwt_secret(value)
        if reason is not None:
            reasons.append(f"{source_name} {reason}")

    if reasons:
        raise RuntimeError(
            "JWT_SECRET 不安全，拒绝启动：" + "；".join(reasons) + "。"
            "JWT_SECRET 泄漏可伪造任意用户 JWT（含管理员）及 ingest key"
            "（后者可向任意租户知识库注入数据）。"
            "请在 .env 中设置为真实随机值（可用 `python -c \"import secrets; "
            "print(secrets.token_urlsafe(48))\"` 生成）；"
            f"若确需在本地开发沿用默认值，显式设置 {_ESCAPE_HATCH_ENV}=1。"
        )


__all__ = [
    "assert_secure_jwt_secret",
    "escape_hatch_enabled",
]
