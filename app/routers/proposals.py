from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import proposals
from ..db import get_db
from ..deps import current_user
from ..schemas import ConfirmIn, ProposalConfirmOut, ProposalOut

router = APIRouter(prefix="/api/proposals", tags=["proposals"])


@router.get(
    "/pending",
    response_model=ProposalOut | None,
    summary="当前待确认的 Agent 建议（没有则返回 null）",
)
def pending(db: Session = Depends(get_db), user_id: str = Depends(current_user)):
    row = proposals.latest_pending(db, user_id)
    return proposals.to_out(row) if row else None


@router.post(
    "/{proposal_id}/confirm",
    response_model=ProposalConfirmOut,
    summary="确认保存建议（可只保存勾选的几条）",
)
def confirm(
    proposal_id: str,
    payload: ConfirmIn,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user),
):
    created, row = proposals.confirm(db, user_id, proposal_id, payload.items)
    return {"proposal": proposals.to_out(row), "activities": created}


@router.post(
    "/{proposal_id}/discard",
    response_model=ProposalOut,
    summary="放弃整张建议单（不产生任何安排）",
)
def discard(
    proposal_id: str, db: Session = Depends(get_db), user_id: str = Depends(current_user)
):
    return proposals.to_out(proposals.discard(db, user_id, proposal_id))
