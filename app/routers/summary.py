from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import services
from ..db import get_db
from ..deps import current_user
from ..schemas import DailySummaryOut

router = APIRouter(prefix="/api/summary", tags=["summary"])


@router.get("/daily", response_model=DailySummaryOut, summary="某天的每日总结")
def daily(
    date: str | None = Query(default=None, description="YYYY-MM-DD，缺省为昨天"),
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user),
):
    """读日报。当天没生成过时 `state=not_generated`、`summary=null`。

    `stats` 无论生成与否都是真实统计——前端可以拿它渲染数字，
    但**不能用它拼一段话冒充总结**，那正是这个接口要防的事。
    """
    return services.daily_summary(db, user_id, date)


@router.post("/daily", response_model=DailySummaryOut, summary="生成某天的每日总结")
def generate(
    date: str | None = Query(default=None, description="YYYY-MM-DD，缺省为昨天"),
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user),
):
    """生成并落库。需要登录态（受保护操作），重复调用覆盖旧内容。"""
    return services.generate_daily_summary(db, user_id, date)
