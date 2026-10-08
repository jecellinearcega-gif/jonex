"""模型名解析 —— 模型名的唯一来源是环境变量。

为什么要有这个模块
------------------
2026-10-08 事故：上游 tokenhub 改了模型名，但代码里 24 处把它硬编码成兜底字面量。
兜底平时不生效（env 都配了），一旦某个 env 漏配就**静默把上游已经不认的旧名字捡回来**，
而且日志里看不出任何线索 —— 排查时只能靠翻调用方日志。

所以这里确立两条规矩：

1. 代码里**不留**任何模型名字面量，一律经本模块解析；
2. 解析不出来就**响亮失败**（`ModelNotConfiguredError`），并指明该配哪个变量 ——
   宁可拦在配置层，也不让一个可能早已失效的名字流到上游才炸。

解析链
------
``<PURPOSE>_MODEL``（如 ``ONTOLOGY_LLM_MODEL`` / ``LLM_MODEL`` / ``OPENKB_LLM_MODEL``）
  ↳ 缺失或空 → ``DEFAULT_LLM_MODEL``
    ↳ 也缺失 → 抛 `ModelNotConfiguredError`

空白（``""`` / 空格 / 换行）一律视为未配置 —— 容器 env 里 ``X=`` 是常见的误配形态，
它在 ``os.getenv`` 眼里是「有值」，直接放行会把空模型名发给上游。
"""

from __future__ import annotations

import os

from jonex_core.common.exceptions import ModelNotConfiguredError

__all__ = [
    "DEFAULT_MODEL_ENV_VAR",
    "resolve_llm_model",
    "resolve_optional_llm_model",
]

#: 全局兜底变量名。所有用途级变量都缺失时，模型名取自它。
DEFAULT_MODEL_ENV_VAR = "DEFAULT_LLM_MODEL"


def _read_env(name: str) -> str:
    """读取环境变量并把空白归一为「未配置」。"""
    if not name:
        return ""
    return os.environ.get(name, "").strip()


def resolve_llm_model(purpose_env: str) -> str:
    """解析模型名：``purpose_env`` → :data:`DEFAULT_MODEL_ENV_VAR` → 抛错。

    Args:
        purpose_env: 用途级变量名（如 ``"ONTOLOGY_LLM_MODEL"``）。
            传空串表示「没有用途级变量，直接用兜底」。

    Returns:
        已去除首尾空白的模型名。

    Raises:
        ModelNotConfiguredError: 两个变量都未配置（或都是空白）。

    每次调用都重新读 ``os.environ``，因此容器里改完 env 无需重启进程即可生效，
    也避免把值缓存在模块级常量里（那正是「改了 env 不生效」这类问题的温床）。
    """
    # 调用方直接传兜底变量名时，等同「没有用途级变量」——否则错误信息里
    # 同一个变量名会出现两次（"DEFAULT_LLM_MODEL 与 DEFAULT_LLM_MODEL 都为空"）。
    if purpose_env == DEFAULT_MODEL_ENV_VAR:
        purpose_env = ""

    value = _read_env(purpose_env)
    if value:
        return value

    fallback = _read_env(DEFAULT_MODEL_ENV_VAR)
    if fallback:
        return fallback

    # 显式 message 只作 i18n 的 fallback（translate 先查语言包），但它会出现在
    # str(exc)、traceback 与日志里 —— 那些地方不走 i18n，变量名必须写全。
    raise ModelNotConfiguredError(
        message=(
            f"模型名称未配置：环境变量 {purpose_env} 与 {DEFAULT_MODEL_ENV_VAR} 都为空"
            if purpose_env
            else f"模型名称未配置：环境变量 {DEFAULT_MODEL_ENV_VAR} 为空"
        ),
        params={"field": purpose_env or DEFAULT_MODEL_ENV_VAR},
        details={
            "purpose_env": purpose_env,
            "default_env": DEFAULT_MODEL_ENV_VAR,
            "hint": (
                f"请配置 {purpose_env} 或 {DEFAULT_MODEL_ENV_VAR}"
                if purpose_env
                else f"请配置 {DEFAULT_MODEL_ENV_VAR}"
            ),
        },
    )


def resolve_optional_llm_model(purpose_env: str = "") -> str:
    """同 :func:`resolve_llm_model`，但都不存在时返回 ``""`` 而不抛错。

    仅供「该字段本就允许留空」的配置默认值使用（如
    ``LLMGW_DISABLE_THINKING_MODELS`` 的兜底）。业务调用点请用
    :func:`resolve_llm_model` —— 静默的空值正是本模块要消灭的东西。
    """
    try:
        return resolve_llm_model(purpose_env)
    except ModelNotConfiguredError:
        return ""
