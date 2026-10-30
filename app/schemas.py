"""接口的输入输出模型。"""

from pydantic import BaseModel, Field


class QuietHour(BaseModel):
    start: str = Field(examples=["12:00"])
    end: str = Field(examples=["13:30"])


class SettingsIn(BaseModel):
    work_start: str = "09:00"
    work_end: str = "18:00"
    morning_start: str = "10:00"
    morning_end: str = "12:00"
    afternoon_start: str = "14:00"
    afternoon_end: str = "17:30"
    quiet_hours: list[QuietHour] = Field(default_factory=list)
    daily_count: int = 3
    reminders_enabled: bool = True
    allow_during_work: bool = True
    postpone_strategy: str = "next_slot"


class SettingsOut(SettingsIn):
    pass


class ActivityOut(BaseModel):
    id: str
    activity_type: str
    title: str
    description: str
    scheduled_at: str
    status: str
    source: str
    postponed_from: str | None = None
    is_overdue: bool = False


class GenerateOut(BaseModel):
    created: int
    adjusted: bool
    message: str | None = None
    activities: list[ActivityOut]


class PostponeOut(BaseModel):
    previous_id: str
    activity: ActivityOut


class DayStat(BaseModel):
    date: str
    total: int
    completed: int


class WeeklyOut(BaseModel):
    week_start: str
    week_end: str
    total: int
    completed: int
    postponed: int
    skipped: int
    pending: int
    daily: list[DayStat]


class SuggestIn(BaseModel):
    message: str = ""


class Suggestion(BaseModel):
    activity_type: str
    title: str
    description: str
    scheduled_at: str


class Rejected(BaseModel):
    scheduled_at: str
    reason: str
    message: str


class SuggestOut(BaseModel):
    mode: str
    note: str
    suggestions: list[Suggestion]
    rejected: list[Rejected]


class ConfirmItem(BaseModel):
    activity_type: str
    scheduled_at: str


class ConfirmIn(BaseModel):
    items: list[ConfirmItem]


class ProposalItem(BaseModel):
    activity_type: str
    title: str
    scheduled_at: str


class ProposalOut(BaseModel):
    id: str
    day: str
    items: list[ProposalItem]
    note: str
    capacity_note: str
    status: str
    created_at: str


class ProposalConfirmOut(BaseModel):
    proposal: ProposalOut
    activities: list[ActivityOut]


class LoginIn(BaseModel):
    username: str


class LoginOut(BaseModel):
    token: str
    user_id: str
