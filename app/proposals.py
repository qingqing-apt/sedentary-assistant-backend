"""Agent 提议单（Proposal）。

这一层存在的唯一理由，是把 C06 那条规则从「prompt 说了算」变成「结构上做不到」：

    Agent 提议 → 只写 proposals 表（不进活动列表、不进统计）
    用户确认   → 才调 advisor.confirm() 写 activities 表

也就是说，确认之前 `activities` 一行都不会多。刷新页面、切设备，待确认的建议都还在；
用户改主意就 discard，连痕迹都不会留在活动表里。

第二道保险在 `confirm()` 里：即使前端传来的是一个被篡改过的 body，落库前仍然要跑
`advisor.confirm()` 那套完整校验（今天 / 免打扰 / 已过去 / 间隔），和 REST
`/api/activities/confirm` 用的是同一个函数，不存在两套规则。
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import advisor, services
from .errors import AppError
from .models import Proposal

PENDING = "pending"
CONFIRMED = "confirmed"
DISCARDED = "discarded"
SUPERSEDED = "superseded"


def _stamp() -> str:
    return services.to_iso(services.now_local())


def _dump(items: list[dict]) -> str:
    """只保留前端需要的三个字段，别把内部字段漏出去。"""
    clean = [
        {
            "activity_type": str(item.get("activity_type", "")),
            "title": str(item.get("title", "")),
            "scheduled_at": str(item.get("scheduled_at", "")),
        }
        for item in items
    ]
    return json.dumps(clean, ensure_ascii=False)


def _load(raw: str) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def to_out(row: Proposal) -> dict:
    return {
        "id": row.id,
        "day": row.day,
        "items": _load(row.items),
        "note": row.note,
        "capacity_note": row.capacity_note,
        "status": row.status,
        "created_at": row.created_at,
    }


def _supersede(db: Session, user_id: str, day: str) -> None:
    """同一天只留一张待确认单。Agent 连排两次时，用户看到的是最后一次的结果。

    这里不删旧记录：留着重放历史，排查「Agent 到底提议过什么」时用得上。
    """
    stmt = select(Proposal).where(
        Proposal.user_id == user_id,
        Proposal.day == day,
        Proposal.status == PENDING,
    )
    stamp = _stamp()
    for row in db.scalars(stmt).all():
        row.status = SUPERSEDED
        row.updated_at = stamp


def create(
    db: Session,
    user_id: str,
    items: list[dict],
    note: str = "",
    capacity_note: str = "",
) -> Proposal:
    """写入待确认提议单。注意：**不创建任何 Activity**。"""
    day = services.now_local().date().isoformat()
    _supersede(db, user_id, day)
    row = Proposal(
        user_id=user_id,
        day=day,
        items=_dump(items),
        note=(note or "")[:255],
        capacity_note=(capacity_note or "")[:255],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def latest_pending(db: Session, user_id: str) -> Proposal | None:
    """只返回今天的待确认单。昨天的建议单不该在今天的界面上晃。"""
    day = services.now_local().date().isoformat()
    stmt = (
        select(Proposal)
        .where(
            Proposal.user_id == user_id,
            Proposal.day == day,
            Proposal.status == PENDING,
        )
        .order_by(Proposal.created_at.desc())
        .limit(1)
    )
    return db.scalars(stmt).first()


def _get_owned(db: Session, user_id: str, proposal_id: str) -> Proposal:
    row = db.get(Proposal, proposal_id)
    # 别人的提议单按「不存在」处理，不泄露它存在过
    if row is None or row.user_id != user_id:
        raise AppError(404, "PROPOSAL_NOT_FOUND", "找不到这张建议单，可能已经失效")
    return row


def confirm(
    db: Session, user_id: str, proposal_id: str, items: list
) -> tuple[list, Proposal]:
    """用户点确认后的落库入口。

    `items` 允许是原提议的子集（用户取消勾选了几条），但每一条都要重新过校验。
    校验失败时 advisor.confirm 会抛 AppError，提议单保持 pending——用户可以改完再试，
    而不是点了确认就整张单子作废。
    """
    row = _get_owned(db, user_id, proposal_id)
    if row.status != PENDING:
        raise AppError(
            409, "PROPOSAL_NOT_PENDING", "这张建议单已经处理过了，刷新看看最新状态"
        )

    created = advisor.confirm(db, user_id, items)

    row.status = CONFIRMED
    row.updated_at = _stamp()
    db.commit()
    db.refresh(row)
    return created, row


def discard(db: Session, user_id: str, proposal_id: str) -> Proposal:
    row = _get_owned(db, user_id, proposal_id)
    if row.status != PENDING:
        raise AppError(
            409, "PROPOSAL_NOT_PENDING", "这张建议单已经处理过了，刷新看看最新状态"
        )
    row.status = DISCARDED
    row.updated_at = _stamp()
    db.commit()
    db.refresh(row)
    return row
