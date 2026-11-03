"""业务服务层：状态流转、排程调用、统计。

所有校验都在这里做，路由层只负责收参数和返响应。
Agent 走的是同一套函数，没有第二条写入路径。
"""

from __future__ import annotations

import json
from datetime import date as Date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import catalog, scheduler
from .config import get_config
from .errors import AppError
from .models import Activity, ActivitySettings, DailySummary
from .schemas import (
    ActivityOut,
    DailyStats,
    DailySummaryBody,
    DailySummaryOut,
    DayStat,
    SettingsIn,
    WeeklyOut,
)
from .scheduler import Spec, hhmm

STATUS_TEXT = {
    "planned": "待处理",
    "completed": "已完成",
    "postponed": "已推迟",
    "skipped": "已跳过",
}

TERMINAL = ("completed", "postponed", "skipped")


def tz() -> ZoneInfo:
    return ZoneInfo(get_config().app_timezone)


_clock = None


def set_clock(fn) -> None:
    """测试与演示时固定「现在」。传 None 恢复系统时钟。"""
    global _clock
    _clock = fn


def now_local() -> datetime:
    if _clock is not None:
        return _clock()
    return datetime.now(tz())


def to_iso(moment: datetime) -> str:
    return moment.astimezone(tz()).isoformat(timespec="minutes")


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz())
    return parsed.astimezone(tz())


def _parse_time(value: str, field: str) -> time:
    try:
        return hhmm(value)
    except (ValueError, IndexError):
        raise AppError(
            400, "INVALID_TIME_FORMAT", f"{field} 需要 HH:MM 格式，例如 09:00", field
        ) from None


def validate_settings(payload: SettingsIn) -> None:
    work_start = _parse_time(payload.work_start, "work_start")
    work_end = _parse_time(payload.work_end, "work_end")
    if work_start >= work_end:
        raise AppError(400, "INVALID_TIME_RANGE", "工作开始时间必须早于结束时间", "work_start")

    for prefix in ("morning", "afternoon"):
        start = _parse_time(getattr(payload, f"{prefix}_start"), f"{prefix}_start")
        end = _parse_time(getattr(payload, f"{prefix}_end"), f"{prefix}_end")
        if start >= end:
            raise AppError(
                400, "INVALID_TIME_RANGE", "可活动时段的开始时间必须早于结束时间", f"{prefix}_start"
            )
        if start < work_start or end > work_end:
            raise AppError(
                400,
                "WINDOW_OUTSIDE_WORK_HOURS",
                "可活动时段必须落在工作时段之内",
                f"{prefix}_start",
            )

    for index, item in enumerate(payload.quiet_hours):
        start = _parse_time(item.start, f"quiet_hours[{index}].start")
        end = _parse_time(item.end, f"quiet_hours[{index}].end")
        if start >= end:
            raise AppError(
                400, "INVALID_TIME_RANGE", "免打扰时段的开始时间必须早于结束时间",
                f"quiet_hours[{index}].start",
            )

    if not 1 <= payload.daily_count <= 10:
        raise AppError(400, "INVALID_DAILY_COUNT", "每天安排次数需要在 1 到 10 之间", "daily_count")


def spec_from_row(row: ActivitySettings) -> Spec:
    raw_quiet = json.loads(row.quiet_hours or "[]")
    return Spec(
        work_start=hhmm(row.work_start),
        work_end=hhmm(row.work_end),
        morning=(hhmm(row.morning_start), hhmm(row.morning_end)),
        afternoon=(hhmm(row.afternoon_start), hhmm(row.afternoon_end)),
        quiet=tuple((hhmm(item["start"]), hhmm(item["end"])) for item in raw_quiet),
        daily_count=row.daily_count,
    )


def get_or_create_settings(db: Session, user_id: str) -> ActivitySettings:
    row = db.scalar(select(ActivitySettings).where(ActivitySettings.user_id == user_id))
    if row is None:
        row = ActivitySettings(user_id=user_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def save_settings(db: Session, user_id: str, payload: SettingsIn) -> ActivitySettings:
    validate_settings(payload)
    row = get_or_create_settings(db, user_id)
    row.work_start = payload.work_start
    row.work_end = payload.work_end
    row.morning_start = payload.morning_start
    row.morning_end = payload.morning_end
    row.afternoon_start = payload.afternoon_start
    row.afternoon_end = payload.afternoon_end
    row.quiet_hours = json.dumps(
        [item.model_dump() for item in payload.quiet_hours], ensure_ascii=False
    )
    row.daily_count = payload.daily_count
    row.reminders_enabled = payload.reminders_enabled
    row.allow_during_work = payload.allow_during_work
    row.postpone_strategy = payload.postpone_strategy
    row.updated_at = to_iso(now_local())
    db.commit()
    db.refresh(row)
    return row


def settings_out(row: ActivitySettings) -> SettingsIn:
    return SettingsIn(
        work_start=row.work_start,
        work_end=row.work_end,
        morning_start=row.morning_start,
        morning_end=row.morning_end,
        afternoon_start=row.afternoon_start,
        afternoon_end=row.afternoon_end,
        quiet_hours=[{"start": q["start"], "end": q["end"]} for q in json.loads(row.quiet_hours or "[]")],
        daily_count=row.daily_count,
        reminders_enabled=row.reminders_enabled,
        allow_during_work=row.allow_during_work,
        postpone_strategy=row.postpone_strategy,
    )


def activity_out(row: Activity, moment: datetime | None = None) -> ActivityOut:
    moment = moment or now_local()
    scheduled = parse_iso(row.scheduled_at)
    return ActivityOut(
        id=row.id,
        activity_type=row.activity_type,
        title=row.title,
        description=row.description,
        scheduled_at=row.scheduled_at,
        status=row.status,
        source=row.source,
        postponed_from=row.postponed_from,
        is_overdue=row.status == "planned" and scheduled < moment,
    )


def day_rows(db: Session, user_id: str, day: Date) -> list[Activity]:
    rows = db.scalars(
        select(Activity)
        .where(Activity.user_id == user_id, Activity.day == day.isoformat())
        .order_by(Activity.scheduled_at)
    ).all()
    return list(rows)


def generate_day(db: Session, user_id: str, force: bool = False) -> dict:
    row = get_or_create_settings(db, user_id)
    moment = now_local()
    day = moment.date()
    existing = day_rows(db, user_id, day)

    if existing and not force:
        return {
            "created": 0,
            "adjusted": False,
            "message": "今天已经生成过安排了，可以直接处理列表里的活动",
            "activities": [activity_out(item, moment) for item in existing],
        }

    if force:
        for item in existing:
            if item.status == "planned":
                db.delete(item)
        db.commit()
        existing = day_rows(db, user_id, day)

    spec = spec_from_row(row)
    busy = tuple(parse_iso(item.scheduled_at) for item in existing if item.status == "planned")
    slots = scheduler.free_slots(spec, day, tz(), busy=busy, not_before=moment)
    points, placed = scheduler.plan_day(slots, spec.daily_count)

    created: list[Activity] = []
    for index, point in enumerate(points):
        template = catalog.rotating(len(existing) + index)
        item = Activity(
            user_id=user_id,
            day=day.isoformat(),
            activity_type=template.key,
            title=template.title,
            description=template.description,
            scheduled_at=to_iso(point),
            status="planned",
            source="user",
        )
        db.add(item)
        created.append(item)
    db.commit()

    message = None
    if placed == 0:
        message = "今天剩余的可活动时段不够安排活动了，可以放宽时段或改到明天"
    elif placed < spec.daily_count:
        message = f"当前时段容量只够安排 {placed} 次，已按 {placed} 次生成"

    return {
        "created": len(created),
        "adjusted": placed < spec.daily_count,
        "message": message,
        "activities": [activity_out(item, moment) for item in day_rows(db, user_id, day)],
    }


def _get_owned(db: Session, user_id: str, activity_id: str) -> Activity:
    item = db.get(Activity, activity_id)
    if item is None or item.user_id != user_id:
        raise AppError(404, "ACTIVITY_NOT_FOUND", "没有找到这条活动安排")
    return item


def _require_planned(item: Activity) -> None:
    if item.status != "planned":
        raise AppError(
            409,
            "INVALID_TRANSITION",
            f"这条活动已经是「{STATUS_TEXT.get(item.status, item.status)}」状态，不能再次操作",
        )


def complete(db: Session, user_id: str, activity_id: str) -> ActivityOut:
    item = _get_owned(db, user_id, activity_id)
    _require_planned(item)
    item.status = "completed"
    item.updated_at = to_iso(now_local())
    db.commit()
    return activity_out(item)


def skip(db: Session, user_id: str, activity_id: str) -> ActivityOut:
    item = _get_owned(db, user_id, activity_id)
    _require_planned(item)
    item.status = "skipped"
    item.updated_at = to_iso(now_local())
    db.commit()
    return activity_out(item)


def postpone(db: Session, user_id: str, activity_id: str) -> dict:
    """推迟 = 原记录终结 + 派生一条新记录。

    为什么不直接改 scheduled_at：周统计要数「推迟了几次」，
    原地改时间会让这个指标永远归零，也丢掉从哪儿挪过来的追溯链。
    """
    item = _get_owned(db, user_id, activity_id)
    _require_planned(item)

    moment = now_local()
    day = Date.fromisoformat(item.day)
    row = get_or_create_settings(db, user_id)
    spec = spec_from_row(row)

    siblings = [
        other
        for other in day_rows(db, user_id, day)
        if other.status == "planned" and other.id != item.id
    ]
    busy = tuple(parse_iso(other.scheduled_at) for other in siblings)
    slots = scheduler.free_slots(spec, day, tz(), busy=busy)

    original = parse_iso(item.scheduled_at)
    after = max(moment, original) if day == moment.date() else original
    target = scheduler.next_slot(slots, after)

    if target is None:
        raise AppError(
            409,
            "NO_AVAILABLE_SLOT",
            "今天已经没有更晚的可活动时段了。可以改到明天，或者把免打扰时段缩短一点再试。",
        )

    item.status = "postponed"
    item.updated_at = to_iso(moment)

    follow_up = Activity(
        user_id=user_id,
        day=day.isoformat(),
        activity_type=item.activity_type,
        title=item.title,
        description=item.description,
        scheduled_at=to_iso(target),
        status="planned",
        source=item.source,
        postponed_from=item.id,
    )
    db.add(follow_up)
    db.commit()
    return {"previous_id": item.id, "activity": activity_out(follow_up, moment)}


def weekly(db: Session, user_id: str) -> WeeklyOut:
    moment = now_local()
    today = moment.date()
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)

    rows = list(
        db.scalars(
            select(Activity).where(
                Activity.user_id == user_id,
                Activity.day >= start.isoformat(),
                Activity.day <= end.isoformat(),
            )
        ).all()
    )

    counters = {"planned": 0, "completed": 0, "postponed": 0, "skipped": 0}
    for item in rows:
        counters[item.status] = counters.get(item.status, 0) + 1

    daily: list[DayStat] = []
    for offset in range(7):
        current = start + timedelta(days=offset)
        same_day = [item for item in rows if item.day == current.isoformat()]
        daily.append(
            DayStat(
                date=current.isoformat(),
                total=len(same_day),
                completed=sum(1 for item in same_day if item.status == "completed"),
            )
        )

    return WeeklyOut(
        week_start=start.isoformat(),
        week_end=end.isoformat(),
        total=len(rows),
        completed=counters["completed"],
        postponed=counters["postponed"],
        skipped=counters["skipped"],
        pending=counters["planned"],
        daily=daily,
    )


# ---------------------------------------------------------------------------
# 每日总结
# ---------------------------------------------------------------------------


def check_day(raw: str | None) -> str:
    """把 ?date= 归一成 YYYY-MM-DD，缺省为「昨天」。

    默认取昨天是因为日报本身就是回头看：今天还没过完，统计完成率没有意义。
    """
    if not raw:
        return (now_local().date() - timedelta(days=1)).isoformat()

    try:
        parsed = Date.fromisoformat(raw)
    except ValueError as exc:
        raise AppError(400, "INVALID_DATE", "date 需要是 YYYY-MM-DD 格式", "date") from exc

    if parsed > now_local().date():
        raise AppError(400, "FUTURE_DATE", "不能查看未来日期的总结", "date")

    return parsed.isoformat()


def daily_stats(db: Session, user_id: str, day: str) -> DailyStats:
    """当天统计，永远现算。

    不在生成总结时把数字抄一份存起来，是因为用户完全可能在生成之后
    再补记一条活动；抄下来的数字会立刻变成假的。这里每次查都是真实值。
    """
    rows = list(
        db.scalars(
            select(Activity).where(Activity.user_id == user_id, Activity.day == day)
        ).all()
    )

    counters = {"planned": 0, "completed": 0, "postponed": 0, "skipped": 0}
    for item in rows:
        counters[item.status] = counters.get(item.status, 0) + 1

    total = len(rows)
    completed = counters["completed"]

    return DailyStats(
        date=day,
        total=total,
        completed=completed,
        postponed=counters["postponed"],
        skipped=counters["skipped"],
        pending=counters["planned"],
        completion_rate=0 if total == 0 else round(completed * 100 / total),
    )


def _summary_body(row: DailySummary) -> DailySummaryBody:
    try:
        points = json.loads(row.points)
    except json.JSONDecodeError:
        points = []
    return DailySummaryBody(
        headline=row.headline,
        points=[str(item) for item in points],
        source=row.source,
        generated_at=row.generated_at,
    )


def daily_summary(db: Session, user_id: str, raw_date: str | None = None) -> DailySummaryOut:
    """读某天的总结。没生成过就返回 state=not_generated，绝不现编一段话。"""
    day = check_day(raw_date)
    stats = daily_stats(db, user_id, day)

    row = db.scalar(
        select(DailySummary).where(
            DailySummary.user_id == user_id, DailySummary.day == day
        )
    )

    return DailySummaryOut(
        date=day,
        state="generated" if row is not None else "not_generated",
        stats=stats,
        summary=_summary_body(row) if row is not None else None,
    )


def _compose(stats: DailyStats) -> tuple[str, list[str]]:
    """规则生成总结文案，不调模型。

    两个理由：一是要能被测试断言，二是没配 API Key 的机器上也得照常工作。
    模型生成留给 source="agent"，以后接进来不用改接口和前端。
    """
    if stats.total == 0:
        return (
            "这天没有安排任何活动",
            ["没有这天的安排记录，可能是休息日，也可能当天没打开过助手。"],
        )

    points = [
        f"计划 {stats.total} 条，完成 {stats.completed} 条，完成率 {stats.completion_rate}%。"
    ]
    if stats.postponed:
        points.append(
            f"{stats.postponed} 条被推迟。同一时段反复被推迟的话，把它挪到另一个窗口比继续推迟更省事。"
        )
    if stats.skipped:
        points.append(f"{stats.skipped} 条标记为「今天不方便」，属于正常取舍，不算失败。")
    if stats.pending:
        points.append(f"还有 {stats.pending} 条停在待处理，可以补记一下完成情况。")

    if stats.completion_rate >= 100:
        headline = "全部完成，节奏保持住了"
    elif stats.completion_rate >= 60:
        headline = "完成过半，整体节奏还在"
    elif stats.completion_rate > 0:
        headline = "完成了一部分，明天可以少排一点"
    else:
        headline = "这天一条都没完成"

    return headline, points


def generate_daily_summary(
    db: Session, user_id: str, raw_date: str | None = None
) -> DailySummaryOut:
    """生成并落库。重复生成会覆盖，generated_at 刷新。"""
    day = check_day(raw_date)
    stats = daily_stats(db, user_id, day)
    headline, points = _compose(stats)

    row = db.scalar(
        select(DailySummary).where(
            DailySummary.user_id == user_id, DailySummary.day == day
        )
    )
    if row is None:
        row = DailySummary(user_id=user_id, day=day)
        db.add(row)

    row.headline = headline
    row.points = json.dumps(points, ensure_ascii=False)
    row.source = "rule"
    row.generated_at = now_local().isoformat(timespec="seconds")

    db.commit()
    db.refresh(row)

    return DailySummaryOut(
        date=day,
        state="generated",
        stats=stats,
        summary=_summary_body(row),
    )
