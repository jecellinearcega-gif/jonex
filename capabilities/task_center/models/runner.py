#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""运行方清单 ORM（平台内置、只读，不带租户）。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §3.4。

平台预置「有哪些运行方可用」，**不是运行方自己注册**；只记标识、名字、对外能力 ID，
供编排步骤绑定运行方时对照。一期就一个（知识库）。
"""

from uuid import uuid4

from sqlalchemy import Column, DateTime, Index, String, text

from jonex_core.common.database import Base


class RunnerDefinition(Base):
    """运行方清单：平台内置的运行方（code → capability_id）。"""

    __tablename__ = "runner_definitions"
    __table_args__ = (
        Index("uq_tc_runner_code", "code", unique=True),
        {"schema": "task_center"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    code = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    capability_id = Column(String(128), nullable=False)
    # 这两列在 DDL 里有（§3.4），应用不写、由数据库 DEFAULT 兜底——映射出来只为
    # 让 ORM 列集合与 DDL 一致（`create_all` 建出来的表不会少列）。
    created_at = Column(DateTime, nullable=True, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=True, server_default=text("CURRENT_TIMESTAMP"))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "capability_id": self.capability_id,
        }


__all__ = ["RunnerDefinition"]
