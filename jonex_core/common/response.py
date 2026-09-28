#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - 统一响应格式

定义所有 API 接口的标准响应结构，确保前后端契约一致
"""

import json as _json
from dataclasses import dataclass, field, asdict
from typing import Optional, Any, Dict
from datetime import datetime, timezone

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response


@dataclass
class StandardResponse:
    """标准化响应格式"""
    request_id: str
    success: bool
    code: int
    message: str
    data: Optional[Any] = None
    error_details: Optional[Dict] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict:
        """转为字典（None 字段会被过滤，datetime 自动序列化）"""
        result = asdict(self)
        result = {k: v for k, v in result.items() if v is not None}
        # FastAPI jsonable_encoder handles datetime, Pydantic models, etc.
        return jsonable_encoder(result)

    @classmethod
    def ok(
        cls,
        request_id: str = "",
        data: Any = None,
        message: str = "success",
    ) -> "StandardResponse":
        """成功响应"""
        return cls(
            request_id=request_id,
            success=True,
            code=0,
            message=message,
            data=data,
        )

    @classmethod
    def error(
        cls,
        request_id: str,
        code: int,
        message: str,
        details: Optional[Dict] = None,
    ) -> "StandardResponse":
        """错误响应"""
        return cls(
            request_id=request_id,
            success=False,
            code=code,
            message=message,
            error_details=details,
        )


def success_response(
    data: Any = None,
    message: str = "success",
    request_id: str = "",
    status_code: int = 200,
) -> JSONResponse:
    """
    构造成功的 JSONResponse

    Args:
        data: 响应数据
        message: 成功消息
        request_id: 请求 ID
        status_code: HTTP 状态码

    Returns:
        JSONResponse
    """
    response = StandardResponse.ok(request_id=request_id, data=data, message=message)
    return JSONResponse(status_code=status_code, content=response.to_dict())


def error_response(
    code: int,
    message: str,
    request_id: str = "",
    status_code: int = 500,
    details: Optional[Dict] = None,
    *,
    params: Optional[Dict] = None,
    localize: bool = True,
) -> JSONResponse:
    """
    构造错误的 JSONResponse

    Args:
        code: 业务错误码
        message: 错误信息（fallback 原文）
        request_id: 请求 ID
        status_code: HTTP 状态码
        details: 错误详情
        params: 模板占位符实参（与 code 对应语言包模板配合）
        localize: 是否按 code+params 取模板翻译（False=原样使用 message）

    Returns:
        JSONResponse
    """
    from jonex_core.common.i18n import translate  # 局部导入避免循环依赖

    display = translate(code, params=params, fallback=message) if localize else message
    response = StandardResponse.error(
        request_id=request_id,
        code=code,
        message=display,
        details=details,
    )
    return JSONResponse(status_code=status_code, content=response.to_dict())


def passthrough_upstream_response(resp) -> Response:
    """把上游代理响应原样透传（保留状态码 + Location + Set-Cookie，不解析 JSON）。

    代理层（Gateway/Sidecar）对 capability 返回的 3xx 重定向必须透传而非
    ``resp.json()``：OIDC 授权码回调（platform ``oidc_callback``）返回
    ``RedirectResponse(302)`` 并 ``set_cookie`` 下发 refresh httpOnly cookie，
    响应体为空。代理层若对其 ``json()`` 会抛 ``JSONDecodeError``，且
    ``raise_for_status()`` 对 3xx 也抛 ``HTTPStatusError``（httpx >= 0.28），
    最终转成 500，SSO 登录不可用。

    Args:
        resp: httpx.Response 上游响应。

    Returns:
        携带原状态码、Location 与全部 Set-Cookie 的 starlette Response。
    """
    headers = {}
    location = resp.headers.get("location")
    if location:
        headers["location"] = location
    # 2xx refresh 透传需保留 content-type（JSON body），否则前端按 text 解析失败。
    content_type = resp.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    response = Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=headers,
    )
    for cookie in resp.headers.get_list("set-cookie"):
        response.headers.append("set-cookie", cookie)
    return response
