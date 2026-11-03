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


class DailyStats(BaseModel):
    """某一天的客观统计，永远从 activities 现算，不受总结生成与否影响。"""

    date: str
    total: int
    completed: int
    postponed: int
    skipped: int
    # 仍是 planned 的条数；命名与 WeeklyOut.pending 保持一致
    pending: int
    # 完成率 = completed / total。用 total 当分母是有意的：
    # 推迟会派生新记录，分母变大反而更接近「这天实际需要处理几次」。
    completion_rate: int


class DailySummaryBody(BaseModel):
    headline: str
    points: list[str]
    source: str
    generated_at: str


class DailySummaryOut(BaseModel):
    """`state` 是给前端做分支用的唯一判据。

    - `not_generated`：这天没有生成过总结，`summary` 必为 null。
      前端必须显示「尚未生成」，不能拿 stats 拼一段话冒充。
    - `generated`：`summary` 有值。
    """

    date: str
    state: str
    stats: DailyStats
    summary: DailySummaryBody | None = None


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
