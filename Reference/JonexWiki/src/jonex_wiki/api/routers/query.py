from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from jonex_wiki.api.deps import require_tenant

router = APIRouter(prefix="/api/v1/kbs/{kb_id}", tags=["query"])


class QueryBody(BaseModel):
    question: str


@router.post("/query", status_code=202)
async def query(kb_id: str, body: QueryBody, request: Request,
                tenant_id: str = Depends(require_tenant)):
    return await request.app.state.app.query(tenant_id, kb_id, body.question)
