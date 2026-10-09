#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""能力层统一响应壳 `ApiResponse[T]`。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §4.0。

四层壳 `{success, code, message, data}`，所有能力对内的「运行方约定接口」与
「能力 client 函数」共用同一套壳，不各写各的：

- ``success``：这次调用通没通。**只要请求被正常处理就是 True**（哪怕业务裁决是
  「拒绝 / 忽略」也算处理成功）；系统层出错（参数非法、找不到、内部异常）才 False。
- ``code``：业务状态码。``0`` = 成功；失败时给具体错误码。与对外 HTTP 那套
  ``StandardResponse.code`` 是同一套码，调用方（尤其运行方）可以按 ``code``
  走语言包翻译（``jonex_core.common.i18n.translate``），不必只依赖 ``message`` 文案。
- ``message``：给人看的说明；成功可为空串，失败放失败原因（兜底文案）。
- ``data``：真正的业务数据；没有数据就为 None。

与 ``jonex_core/common/response.py`` 的 ``StandardResponse`` 的分工：
``StandardResponse`` 是**对外 HTTP** 出口的信封（带 request_id/code/timestamp，
由路由层 ``success_response()`` / ``error_response()`` 产出）；``ApiResponse``
是**对内函数调用**的信封（能力 client、运行方接口），不经过 HTTP 序列化。
两者不要互相顶替。
"""

from typing import Any, Dict, Generic, Optional, TypeVar

try:  # pydantic>=1.10 的 v1 兼容命名空间优先
    from pydantic.v1 import BaseModel, Field
except ImportError:  # pragma: no cover - 仅 pydantic v1 原生安装时走到
    from pydantic import BaseModel, Field

T = TypeVar("T")

#: 业务状态码：成功
CODE_OK = 0
#: 业务状态码：未分类的失败（没有更具体的码时用）
CODE_FAIL = -1


class ApiResponse(BaseModel, Generic[T]):
    """能力层统一响应壳：``{success, code, message, data}``。"""

    success: bool = Field(default=True, description="这次调用有没有被正常受理/处理")
    code: int = Field(
        default=CODE_OK,
        description="业务状态码：0=成功；失败时给具体错误码（供按 code 做多语言）",
    )
    message: str = Field(default="", description="给人看的说明：失败原因是失败原因，成功可为空串")
    data: Optional[T] = Field(default=None, description="业务数据；没有就为 None")

    @classmethod
    def ok(
        cls,
        data: Optional[T] = None,
        message: str = "",
        code: int = CODE_OK,
    ) -> "ApiResponse[T]":
        """受理成功：带上业务数据（可为 None）。"""
        return cls(success=True, code=code, message=message, data=data)

    @classmethod
    def fail(
        cls,
        message: str,
        code: int = CODE_FAIL,
        data: Optional[T] = None,
    ) -> "ApiResponse[T]":
        """没受理成：``code`` 放业务错误码、``message`` 放原因；业务裁决层面的「拒绝/忽略」不走这里。"""
        return cls(success=False, code=code, message=message, data=data)

    def to_dict(self) -> Dict[str, Any]:
        """转为裸 dict（跨进程回传 / 与既有 dict 契约互操作时用）。"""
        return {
            "success": self.success,
            "code": self.code,
            "message": self.message,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, payload: Optional[Dict[str, Any]]) -> "ApiResponse[Any]":
        """从裸 dict 还原；``None`` / 非 dict 一律当作「没受理成」。"""
        if not isinstance(payload, dict):
            return cls(success=False, code=CODE_FAIL, message="上游未返回有效响应体", data=None)
        return cls(
            success=bool(payload.get("success", False)),
            code=int(payload.get("code", CODE_FAIL)),
            message=str(payload.get("message") or ""),
            data=payload.get("data"),
        )


__all__ = ["ApiResponse", "CODE_OK", "CODE_FAIL"]
