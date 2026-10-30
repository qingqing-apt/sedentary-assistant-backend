from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import advisor
from ..db import get_db
from ..deps import current_user
from ..schemas import SuggestIn, SuggestOut

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.post("/suggest", response_model=SuggestOut, summary="生成安排建议（不落库）")
async def suggest(
    payload: SuggestIn, db: Session = Depends(get_db), user_id: str = Depends(current_user)
):
    return await advisor.suggest(db, user_id, payload.message)
