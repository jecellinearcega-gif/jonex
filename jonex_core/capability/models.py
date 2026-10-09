from dataclasses import dataclass, field
from typing import Any, Dict, Optional, List
from enum import Enum
from uuid import uuid4


class CapabilityType(str, Enum):
    """能力类型"""
    ATOMIC = "atomic"        # 原子能力
    DOMAIN = "domain"        # 领域能力
    BUSINESS = "business"    # 业务能力


@dataclass
class CapabilityMetadata:
    """能力元数据"""
    capability_id: str
    capability_name: str
    capability_type: CapabilityType
    version: str = "v1"
    description: str = ""
    author: str = "jonex"
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    pricing_plan: Optional[str] = None
    tags: List[str] = field(default_factory=list)

    @property
    def full_id(self) -> str:
        """完整能力ID: type.id.version"""
        return f"{self.capability_type.value}.{self.capability_id}.{self.version}"


@dataclass
class CapabilityRequest:
    """能力调用请求"""
    tenant_id: str
    capability_id: str
    payload: Dict[str, Any]
    request_id: str = field(default_factory=lambda: str(uuid4()))
    context: Optional[Dict[str, Any]] = None
    user_id: Optional[str] = None
    username: Optional[str] = None
    ip: Optional[str] = None

    def get(self, key: str, default: Any = None) -> Any:
        """获取payload中的字段"""
        return self.payload.get(key, default)


@dataclass
class CapabilityResponse:
    """能力调用响应。

    `data` 是 `Any`：设计文档 §4.3 明确它可以**是对象也可以是列表**（不设分页的接口
    直接回裸数组）。这里是 `@dataclass`、不做运行期校验，写窄了不会当场炸，但会让
    照着标注写代码的人以为「只能回对象」——Sidecar 那张 Pydantic 模型就是这么被带窄、
    进而把列表响应拦成 500 的。标注与契约保持一致。
    """

    request_id: str
    success: bool
    code: int
    message: str
    data: Optional[Any] = None
    metadata: Optional[Dict[str, Any]] = None
    details: Optional[Dict[str, Any]] = None
    params: Optional[Dict[str, Any]] = None

    @classmethod
    def ok(cls, request_id: str, data: Any = None, message: str = "success") -> "CapabilityResponse":
        """成功响应"""
        return cls(request_id=request_id, success=True, code=0, message=message, data=data)

    @classmethod
    def error(cls, request_id: str, code: int, message: str,
              details: Optional[Dict[str, Any]] = None,
              params: Optional[Dict[str, Any]] = None) -> "CapabilityResponse":
        """错误响应"""
        return cls(request_id=request_id, success=False, code=code, message=message,
                   details=details, params=params)


@dataclass
class CapabilityHealth:
    """能力健康状态"""
    capability_id: str
    is_healthy: bool
    message: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)
