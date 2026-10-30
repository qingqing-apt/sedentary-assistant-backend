from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import services
from ..db import get_db
from ..deps import current_user
from ..schemas import WeeklyOut

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/weekly", response_model=WeeklyOut, summary="本周完成情况")
def weekly(db: Session = Depends(get_db), user_id: str = Depends(current_user)):
    return services.weekly(db, user_id)
