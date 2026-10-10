"""Minimal dependency plan and recovery state for the default query agent."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator, field_validator, ConfigDict


class NodeStatus(str, Enum):
    PENDING_DEPENDENCIES = "pending_dependencies"
    READY = "ready"
    CANDIDATE = "candidate"
    SUPPORTED = "supported"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"
    STALE = "stale"


class QueryNode(BaseModel):
    id: str
    question: str
    relation: str
    depends_on: List[str] = Field(default_factory=list)
    input_bindings: Dict[str, str] = Field(default_factory=dict)
    expected_type: str = "fact"
    constraints: Dict[str, Any] = Field(default_factory=dict)
    status: NodeStatus = NodeStatus.PENDING_DEPENDENCIES
    candidate: Optional[str] = None
    supported_result: Optional[str] = None
    evidence_refs: List[Dict[str, Any]] = Field(default_factory=list)
    attempt_history: List[Dict[str, Any]] = Field(default_factory=list)
    rewrite_count: int = 0
    fallback_used: bool = False
    candidate_ids: List[str] = Field(default_factory=list)
    failure_reason: Optional[str] = None
    binding_version: int = 1

    model_config = ConfigDict(extra="forbid")
    @field_validator("id", "question", "relation")
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("QUERY_PLAN_BLANK_FIELD")
        return value


class QueryBudget(BaseModel):
    max_nodes: int = 8
    max_tool_actions: int = 32
    max_model_calls: int = 24
    max_total_tokens: int = 360000
    max_seconds: int = 180
    tool_actions: int = 0
    model_calls: int = 0
    total_tokens: int = 0

    model_config = ConfigDict(extra="forbid")
class QueryPlan(BaseModel):
    original_question: str
    nodes: List[QueryNode]
    final_node_id: str
    backtrack_count: int = 0
    budget: QueryBudget = Field(default_factory=QueryBudget)

    model_config = ConfigDict(extra="forbid")
    @model_validator(mode="after")
    def _valid_graph(self):
        nodes = self.nodes or []
        budget = self.budget or QueryBudget()
        if not nodes or len(nodes) > budget.max_nodes:
            raise ValueError("QUERY_PLAN_NODE_LIMIT")
        by_id = {node.id: node for node in nodes}
        if len(by_id) != len(nodes) or self.final_node_id not in by_id:
            raise ValueError("QUERY_PLAN_ID_INVALID")
        for node in nodes:
            if node.id in node.depends_on or any(dep not in by_id for dep in node.depends_on):
                raise ValueError("QUERY_PLAN_DEPENDENCY_INVALID")
        visiting, visited = set(), set()
        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError("QUERY_PLAN_CYCLE")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dependency in by_id[node_id].depends_on:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)
        for node_id in by_id:
            visit(node_id)
        return self

    @classmethod
    def from_directions(cls, question: str, directions: List[str]) -> "QueryPlan":
        directions = directions or [question]
        nodes = [QueryNode(id="N{}".format(index + 1), question=value,
                           relation=value, depends_on=[] if index == 0 else ["N{}".format(index)],
                           status=NodeStatus.READY if index == 0 else NodeStatus.PENDING_DEPENDENCIES)
                 for index, value in enumerate(directions)]
        return cls(original_question=question, nodes=nodes, final_node_id=nodes[-1].id)

    @classmethod
    def from_graph(cls, question: str, specifications: List[Dict[str, Any]],
                   *, final_node_id: str) -> "QueryPlan":
        """Build a bounded explicit DAG, including two independent branches."""
        nodes = [QueryNode.model_validate({**item, "relation": item.get("relation", item.get("question", ""))})
                 for item in specifications]
        by_id = {node.id: node for node in nodes}
        normalized = []
        for node in nodes:
            status = (NodeStatus.READY if not node.depends_on else
                      NodeStatus.PENDING_DEPENDENCIES)
            normalized.append(node.model_copy(update={"status": status}))
        if final_node_id not in by_id:
            raise ValueError("QUERY_PLAN_ID_INVALID")
        return cls(original_question=question, nodes=normalized,
                   final_node_id=final_node_id)

    def ready_nodes(self) -> List[QueryNode]:
        by_id = {node.id: node for node in self.nodes}
        return [node for node in self.nodes if node.status in {NodeStatus.READY, NodeStatus.PENDING_DEPENDENCIES}
                and all(by_id[dependency].status == NodeStatus.SUPPORTED for dependency in node.depends_on)]

    def support(self, node_id: str, result: str, evidence_refs: List[Dict[str, Any]]) -> "QueryPlan":
        if not evidence_refs:
            raise ValueError("QUERY_PLAN_SUPPORT_REQUIRES_EVIDENCE")
        nodes = [node.model_copy(deep=True) for node in self.nodes]
        target = next(node for node in nodes if node.id == node_id)
        by_id = {node.id: node for node in nodes}
        if not all(by_id[dependency].status == NodeStatus.SUPPORTED for dependency in target.depends_on):
            raise ValueError("QUERY_PLAN_DEPENDENCIES_NOT_SUPPORTED")
        target.status, target.supported_result, target.evidence_refs = NodeStatus.SUPPORTED, result, evidence_refs
        for node in nodes:
            if node.status == NodeStatus.PENDING_DEPENDENCIES and all(by_id[dep].status == NodeStatus.SUPPORTED for dep in node.depends_on):
                node.status = NodeStatus.READY
        return self.model_copy(update={"nodes": nodes})

    def backtrack(self, node_id: str, *, reason: str) -> "QueryPlan":
        if self.backtrack_count >= 1:
            raise ValueError("QUERY_PLAN_BACKTRACK_LIMIT")
        nodes = [node.model_copy(deep=True) for node in self.nodes]
        descendants = {node_id}
        changed = True
        while changed:
            changed = False
            for node in nodes:
                if node.id not in descendants and any(dep in descendants for dep in node.depends_on):
                    descendants.add(node.id)
                    changed = True
        for node in nodes:
            if node.id == node_id:
                node.status, node.failure_reason = NodeStatus.AMBIGUOUS, reason
                node.binding_version += 1
            elif node.id in descendants:
                node.status, node.supported_result, node.evidence_refs = NodeStatus.STALE, None, []
                node.binding_version += 1
        return self.model_copy(update={"nodes": nodes, "backtrack_count": self.backtrack_count + 1})

    def can_answer(self) -> bool:
        by_id = {node.id: node for node in self.nodes}
        final = by_id[self.final_node_id]
        closure = set()
        stack = [final.id]
        while stack:
            current = stack.pop()
            if current in closure:
                continue
            closure.add(current)
            stack.extend(by_id[current].depends_on)
        return all(by_id[node_id].status == NodeStatus.SUPPORTED for node_id in closure)


__all__ = ["NodeStatus", "QueryBudget", "QueryNode", "QueryPlan"]
