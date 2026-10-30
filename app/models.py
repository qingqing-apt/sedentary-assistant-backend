"""ORM 模型。

两个设计取舍，写在这里免得后人踩坑：

1. 时间统一存「带 +08:00 偏移的 ISO 字符串」。SQLite 的 DateTime 不保留时区，
   读回来是 naive，容器里再和 datetime.now() 一比就整体偏 8 小时。存字符串没有这个坑。
2. 额外冗余一个 day 列（YYYY-MM-DD 本地日期）。周统计和「今日安排」都按天查，
   用字符串前缀匹配比每次解析时间戳快也简单。
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _uid() -> str:
    return uuid.uuid4().hex


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class ActivitySettings(Base):
    """每个用户一行。时间字段统一用 "HH:MM" 字符串，跨库无坑。"""

    __tablename__ = "activity_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    work_start: Mapped[str] = mapped_column(String(5), default="09:00")
    work_end: Mapped[str] = mapped_column(String(5), default="18:00")
    morning_start: Mapped[str] = mapped_column(String(5), default="10:00")
    morning_end: Mapped[str] = mapped_column(String(5), default="12:00")
    afternoon_start: Mapped[str] = mapped_column(String(5), default="14:00")
    afternoon_end: Mapped[str] = mapped_column(String(5), default="17:30")

    # [{"start": "12:00", "end": "13:30"}, ...]
    quiet_hours: Mapped[str] = mapped_column(Text, default="[]")

    daily_count: Mapped[int] = mapped_column(Integer, default=3)
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_during_work: Mapped[bool] = mapped_column(Boolean, default=True)
    postpone_strategy: Mapped[str] = mapped_column(String(32), default="next_slot")

    updated_at: Mapped[str] = mapped_column(String(40), default=_now_iso)


class Activity(Base):
    __tablename__ = "activities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)

    activity_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(255))

    scheduled_at: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), default="planned", index=True)
    source: Mapped[str] = mapped_column(String(16), default="user")
    postponed_from: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[str] = mapped_column(String(40), default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String(40), default=_now_iso)


class Proposal(Base):
    """Agent 提议单（用户还没确认的那一堆建议）。

    为什么要单独一张表，而不是直接把建议写成 planned 的 Activity：

    「用户没确认就不能有安排」这条规则，如果只写在 prompt 里，本质是靠模型自觉。
    把提议单独立出来之后，确认之前 `activities` 表一行都不会多——这条保证从
    「说好了不写」变成「数据结构上就没有写入口」。同时用户刷新页面、换设备，
    待确认的建议都还在，不会因为一次刷新丢掉。

    提议单不进任何统计，且同一用户同一天只保留一张 pending（新的覆盖旧的）。
    """

    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uid)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)

    # [{"activity_type": "walk", "title": "起身走动", "scheduled_at": "..."}]
    items: Mapped[str] = mapped_column(Text, default="[]")
    note: Mapped[str] = mapped_column(String(255), default="")
    capacity_note: Mapped[str] = mapped_column(String(255), default="")

    # pending / confirmed / discarded / superseded
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    source: Mapped[str] = mapped_column(String(16), default="agent")

    created_at: Mapped[str] = mapped_column(String(40), default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String(40), default=_now_iso)
