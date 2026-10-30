from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import advisor, services
from ..db import get_db
from ..deps import current_user
from ..schemas import ActivityOut, ConfirmIn, GenerateOut, PostponeOut

router = APIRouter(prefix="/api/activities", tags=["activities"])


@router.get("/today", response_model=list[ActivityOut], summary="今日活动安排")
def today(db: Session = Depends(get_db), user_id: str = Depends(current_user)):
    moment = services.now_local()
    rows = services.day_rows(db, user_id, moment.date())
    return [services.activity_out(item, moment) for item in rows]


@router.post("/generate", response_model=GenerateOut, summary="按设置生成今日安排")
def generate(
    force: bool = Query(default=False, description="为 true 时清掉未完成安排重新生成"),
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user),
):
    return services.generate_day(db, user_id, force=force)


@router.patch("/{activity_id}/complete", response_model=ActivityOut, summary="标记完成")
def complete(
    activity_id: str, db: Session = Depends(get_db), user_id: str = Depends(current_user)
):
    return services.complete(db, user_id, activity_id)


@router.patch("/{activity_id}/skip", response_model=ActivityOut, summary="今天不方便")
def skip(activity_id: str, db: Session = Depends(get_db), user_id: str = Depends(current_user)):
    return services.skip(db, user_id, activity_id)


@router.patch("/{activity_id}/postpone", response_model=PostponeOut, summary="推迟到下一可用时段")
def postpone(
    activity_id: str, db: Session = Depends(get_db), user_id: str = Depends(current_user)
):
    return services.postpone(db, user_id, activity_id)


@router.post("/confirm", response_model=list[ActivityOut], summary="确认保存 Agent 建议")
def confirm(
    payload: ConfirmIn, db: Session = Depends(get_db), user_id: str = Depends(current_user)
):
    return advisor.confirm(db, user_id, payload.items)
