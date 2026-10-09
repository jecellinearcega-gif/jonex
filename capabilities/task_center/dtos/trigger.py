#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""触发层 DTO（§八、§3.3）。"""

from typing import Any, Dict, List, Optional

try:  # pydantic>=1.10 的 v1 兼容命名空间优先
    from pydantic.v1 import BaseModel, Field
except ImportError:  # pragma: no cover
    from pydantic import BaseModel, Field

# 这几个长度**与 `task_triggers` 的列宽一一对应**（`models/trigger.py`），别各写各的。
#
# ⚠️ **不是装饰**：`cron_expr` 是自由文本，合法但很长的表达式是写得出来的（比如把分钟段
# 的 60 个值逐个列出来 ≈ 170 字符），那种串能过语法校验、却会在 INSERT 时被 PostgreSQL
# 以 `22001`（value too long）拒掉 —— 抛的是 `DataError`，**不是** service 认领的
# `IntegrityError`，于是它一路漏到能力入口的兜底 `except`，回一句「执行失败，请稍后重试
# 或联系管理员」+ 一个 5xxx 码（`capability.py` 的 `_internal_error_code` 按异常类型给）。
# 调用方以为服务端坏了，真因却是自己给的串太长，正确答案是 **1001 参数非法**。
# 写进 DTO 之后由 `_from_request` 把 pydantic 的 `ValidationError` 翻成 1001（字段名与
# 原因一并带回），交给调用方自己改。同仓 `knowledge_base/dtos/data_source.py` 对同宽列
# 就是这么做的，这里从前漏了。
_MAX_DEFINITION_ID = 64
_MAX_TRIGGER_KEY = 64
_MAX_NAME = 128
_MAX_CRON_EXPR = 128


class TaskTriggerCreate(BaseModel):
    """`trig.create` 参数（`space_id` / `tenant_id` 由调用方另外传，不在这）。

    **触发器只等于定时** —— 从前有 `manual`（手动点一下）/ `cron`（定时到点自动）两种
    类型，`manual` 已砍、`trigger_type` 这一列整个删掉，所以这里**没有类型可选**。

    想「手动跑一次」**不该建触发器**，走 `task.fire(definition_id, space_id, …)` 那条路
    —— 它不经触发器、`tasks.trigger_id` 记 NULL。

    因此 `cron_expr` 与 `misfire_policy` 是**必填**（`next_fire_at` 由服务端按 `cron_expr`
    算出来，**不是**入参）。字段在 DTO 上仍写成 `Optional` 是为了让「缺哪个」由服务层的
    `cron_fields_incomplete` 去报 —— 那句文案比 pydantic 的裸校验错有用得多。
    """

    definition_id: str = Field(max_length=_MAX_DEFINITION_ID, description="要触发哪个定义")
    trigger_key: str = Field(
        max_length=_MAX_TRIGGER_KEY,
        description="触发器的唯一名（同租户同空间内不重复）",
    )
    name: str = Field(max_length=_MAX_NAME, description="给人看的名字")
    cron_expr: Optional[str] = Field(
        default=None,
        max_length=_MAX_CRON_EXPR,
        description="【必填】5 段表达式「分 时 日 月 周」，语法见 §3.3.2",
    )
    misfire_policy: Optional[str] = Field(
        default=None, description="【必填】skip=跳过已错过的 / catch_up=补跑一次（只补一次）"
    )
    inputs: Optional[Dict[str, Any]] = Field(
        default=None, description="触发时带的入参，会注入成任务级参数"
    )
    fixed_version: Optional[int] = Field(
        default=None,
        ge=1,
        description="锁定哪一版：不填=用最新已发布版；填了但该版本不存在 / 未发布 → 报错。"
        "⚠️ `ge=1` 是刻意的：`0` 不是「没填」，是个非法的版本号，一律回 1001",
    )

    class Config:
        extra = "ignore"


class TaskTriggerUpdate(BaseModel):
    """`trig.update` 参数：只给要改的字段，没给的不动。

    **`space_id` / `status` 改不了**（§4.3 M5）——它们列在这里不是为了改，而是为了
    **让服务层能察觉「调用方试图改」**：传了、且跟库里那个值不一样 → **拒绝**
    （**明确报错、不静默忽略**）；传了但跟原来一样 → 当没传。触发器建好就绑死那个空间，
    要换就删了重建；状态一律走 `trig.pause` / `trig.resume`。

    **`trigger_key` 可以改**（改名）—— 它不是「不可改字段」，所以**不在**上面那套
    「靠字段在场把『想改』捞进服务层」的套路里：给了值就真改。改了要查**同空间重名**，
    撞上别的触发器 → `ResourceConflictError`（**1003**，与 `trig.create` 复用同一个
    消息键 `err.task_center.trigger.dup_key`）。

    ⚠️ **`status` 这一行不是可有可无的**：本 DTO 是 `extra = "ignore"`，报文里一个
    **没有对应字段**的键会被 pydantic 直接丢掉、服务层连看一眼的机会都没有 ——
    于是「传了 `status='paused'`」会**静默无效**、还回 `code=0`，调用方以为改成了。
    它跟 `space_id` 是同一个套路：**靠「字段在场」把「想改」这件事捞进服务层**。
    """

    trigger_key: Optional[str] = Field(
        default=None, max_length=_MAX_TRIGGER_KEY, description="触发器的唯一名"
    )
    name: Optional[str] = Field(
        default=None, max_length=_MAX_NAME, description="给人看的名字"
    )
    space_id: Optional[str] = Field(
        default=None,
        max_length=_MAX_DEFINITION_ID,
        description="**不可改**：传了就得跟原值一样",
    )
    status: Optional[str] = Field(
        default=None,
        description="**不可改**：传了就得跟原值一样。改状态走 `trig.pause` / `trig.resume`",
    )
    cron_expr: Optional[str] = Field(
        default=None,
        max_length=_MAX_CRON_EXPR,
        description="5 段表达式；改了会重算 next_fire_at",
    )
    misfire_policy: Optional[str] = Field(
        default=None, description="skip / catch_up；改了会重算 next_fire_at"
    )
    inputs: Optional[Dict[str, Any]] = Field(default=None, description="触发入参")
    fixed_version: Optional[int] = Field(
        default=None,
        ge=1,
        description="锁定哪一版。⚠️ 本字段按「键在不在」判定：**传 `null` = 清空**（切回用最新已发布版）；不传 = 不动。"
        "⚠️ `ge=1` 是刻意的：`0` 不是「没填」、也不是「清空」——清空请传 `null`",
    )

    class Config:
        extra = "ignore"


class TaskTriggerItem(BaseModel):
    """触发详情 / 列表项。"""

    id: str = Field(description="主键")
    tenant_id: Optional[str] = Field(default=None, description="租户")
    space_id: str = Field(description="这个触发器在哪个空间下引用定义")
    definition_id: str = Field(description="要触发哪个定义")
    trigger_key: str = Field(description="触发器的唯一名")
    name: str = Field(description="给人看的名字")
    status: str = Field(
        default="active",
        description="active(启用) / paused(已暂停) / deleted(已删)；paused 的不会被调度循环扫到，但仍能手动 trig.fire",
    )
    cron_expr: Optional[str] = Field(default=None, description="5 段表达式")
    next_fire_at: Optional[str] = Field(
        default=None, description="下次该触发的时间（秒一律为 00）"
    )
    misfire_policy: Optional[str] = Field(
        default=None, description="skip / catch_up"
    )
    fixed_version: Optional[int] = Field(
        default=None, description="锁定哪一版；空 = 用最新已发布版"
    )
    inputs: Dict[str, Any] = Field(default_factory=dict, description="触发入参")
    last_fired_at: Optional[str] = Field(
        default=None,
        description="上次「到点处理」的时间 —— 不代表真的起过任务（skip 那次没起，也照样刷）；要「上次真正触发的时间」看 trig.runs 最后一条的 created_at",
    )
    state_version: int = Field(default=0, description="每行版本号（触发抢占的 CAS 用它）")
    created_at: Optional[str] = Field(default=None, description="创建时间")
    updated_at: Optional[str] = Field(default=None, description="更新时间")

    class Config:
        extra = "ignore"


class TaskTriggerList(BaseModel):
    """触发分页列表（§4.0：列表类统一 `{items, page, page_size, total}`）。"""

    items: List[TaskTriggerItem] = Field(default_factory=list)
    page: int = Field(default=1, description="第几页（从 1 起）")
    page_size: int = Field(default=20, description="每页几条")
    total: int = Field(default=0, description="这次的匹配总数（判断还有没有下一页）")

    class Config:
        extra = "ignore"


__all__ = [
    "TaskTriggerCreate",
    "TaskTriggerItem",
    "TaskTriggerList",
    "TaskTriggerUpdate",
]
