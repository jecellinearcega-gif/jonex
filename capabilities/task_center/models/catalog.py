#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""内置清单 ORM：任务清单 + 步骤清单（平台预置、只读，不带租户）。

设计出处：`docs/superpowers/specs/2026-09-09-task-center-design.md` §3.4。

这两张表回答「平台支持哪些任务可选」「每个任务下有哪些步骤可选」；步骤的固有形状
（跑哪个运行方 `runner`、输入输出 schema、默认超时、并发上限）定义在这里，编排里只引用不重定义。
表里的 created_at / updated_at 由数据库 DEFAULT CURRENT_TIMESTAMP 兜底，应用不写。
"""

from uuid import uuid4

from sqlalchemy import Column, DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base


class TaskCatalog(Base):
    """内置任务清单：平台支持哪些任务可选。"""

    __tablename__ = "task_catalog"
    __table_args__ = (
        # 索引名 / 列 / 唯一性逐条对齐 DDL（`deploy/postgres/migrations/008_task_center.sql`）。
        # 线上建表走的是那份 DDL，但这几处**唯一性是有业务含义的**（§7.3：并发重跑靠
        # `uq_tc_ta_attempt` 兜、受理凭证靠 `uq_tc_subtask_exec` 定位），不能只在 DDL 里存在——
        # 万一有人用 `Base.metadata.create_all` 建表，唯一约束必须跟着走。下同。
        Index("uq_tc_catalog_code", "code", unique=True),
        {"schema": "task_center"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    code = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    description = Column(String(512), nullable=True)
    # 这两列在 DDL 里有（§3.4），应用不写、由数据库 DEFAULT 兜底——映射出来只为
    # 让 ORM 列集合与 DDL 一致（`create_all` 建出来的表不会少列）。
    created_at = Column(DateTime, nullable=True, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=True, server_default=text("CURRENT_TIMESTAMP"))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "description": self.description,
        }


class TaskCatalogStep(Base):
    """内置步骤清单：每个任务下有哪些步骤可选（步骤的固有形状都在这）。"""

    __tablename__ = "task_catalog_steps"
    __table_args__ = (
        Index("uq_tc_cs_code", "task_catalog_code", "step_code", unique=True),
        {"schema": "task_center"},
    )

    id = Column(String(64), primary_key=True, default=lambda: str(uuid4()))
    task_catalog_code = Column(String(64), nullable=False)
    step_code = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    runner_code = Column(String(64), nullable=False)
    # 这一步最多同时跑几个（空 = 不限）；派活时管理方数「这步全局有多少个在跑」到上限就跳过（§3.4、§6.3）
    max_concurrency = Column(Integer, nullable=True)
    timeout_seconds = Column(Integer, nullable=False, default=300)
    input_schema = Column(JSONB, nullable=True)
    output_schema = Column(JSONB, nullable=True)
    created_at = Column(DateTime, nullable=True, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=True, server_default=text("CURRENT_TIMESTAMP"))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_catalog_code": self.task_catalog_code,
            "step_code": self.step_code,
            "name": self.name,
            "runner_code": self.runner_code,
            "max_concurrency": self.max_concurrency,
            "timeout_seconds": self.timeout_seconds,
            "input_schema": self.input_schema or {},
            "output_schema": self.output_schema or {},
        }


__all__ = ["TaskCatalog", "TaskCatalogStep"]
