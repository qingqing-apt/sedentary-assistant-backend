"""排程算法 —— 全项目唯一的硬逻辑。

刻意做成纯函数：不碰数据库、不碰时钟、不碰全局状态。
好处是 `tests/test_scheduler.py` 能在毫秒级把边界情况全跑一遍。

核心算式只有一行：

    可用区间 = (上午窗口 ∪ 下午窗口) ∩ 工作时段 − 免打扰区间 − 已占用区间

注意是「区间求差集」而不是「判断时间点是否落在免打扰里」。
后者遇到「免打扰嵌在窗口中间」时会把整个窗口丢掉，是本任务最常见的错法。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date, datetime, time, timedelta
from zoneinfo import ZoneInfo

Interval = tuple[datetime, datetime]

ACTIVITY_BUFFER = timedelta(minutes=10)  # 已排活动前后各留出的空档
MIN_GAP = timedelta(minutes=30)  # 两条活动之间的最小间隔
MIN_STEP = timedelta(minutes=30)  # 推迟后至少往后挪这么久，与 MIN_GAP 保持一致
ROUND_TO = timedelta(minutes=5)  # 生成的时间点对齐到 5 分钟


@dataclass(frozen=True)
class Spec:
    """排程所需的全部输入，与 ORM 解耦，方便单测直接构造。"""

    work_start: time
    work_end: time
    morning: tuple[time, time]
    afternoon: tuple[time, time]
    quiet: tuple[tuple[time, time], ...]
    daily_count: int


def hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def at(day: Date, moment: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, moment, tzinfo=tz)


def total_len(intervals: list[Interval]) -> timedelta:
    return sum((end - start for start, end in intervals), timedelta(0))


def merge(intervals) -> list[Interval]:
    """排序并合并重叠/相邻区间。"""
    items = sorted((s, e) for s, e in intervals if e > s)
    merged: list[Interval] = []
    for start, end in items:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract(base, cuts) -> list[Interval]:
    """base 减去 cuts，返回剩下的区间。就是那一步「挖洞」。"""
    result = merge(base)
    for cut_start, cut_end in merge(cuts):
        remaining: list[Interval] = []
        for start, end in result:
            if cut_end <= start or cut_start >= end:
                remaining.append((start, end))
                continue
            if start < cut_start:
                remaining.append((start, cut_start))
            if cut_end < end:
                remaining.append((cut_end, end))
        result = remaining
    return result


def free_slots(
    spec: Spec,
    day: Date,
    tz: ZoneInfo,
    busy: tuple[datetime, ...] = (),
    not_before: datetime | None = None,
) -> list[Interval]:
    """算出某一天真正可以安排活动的区间。"""
    work_start = at(day, spec.work_start, tz)
    work_end = at(day, spec.work_end, tz)

    windows: list[Interval] = []
    for win_start, win_end in (spec.morning, spec.afternoon):
        if win_end <= win_start:
            continue
        start = max(at(day, win_start, tz), work_start)
        end = min(at(day, win_end, tz), work_end)
        if end > start:
            windows.append((start, end))

    cuts: list[Interval] = [
        (at(day, q_start, tz), at(day, q_end, tz))
        for q_start, q_end in spec.quiet
        if q_end > q_start
    ]
    cuts.extend((moment - ACTIVITY_BUFFER, moment + ACTIVITY_BUFFER) for moment in busy)

    slots = subtract(windows, cuts)

    if not_before is not None:
        slots = [(max(start, not_before), end) for start, end in slots if end > not_before]
    return slots


def _snap_nearest(moment: datetime) -> datetime:
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    step = ROUND_TO.total_seconds() / 60
    minutes = round((moment - midnight).total_seconds() / 60 / step) * step
    return midnight + timedelta(minutes=minutes)


def _snap_up(moment: datetime) -> datetime:
    rounded = _snap_nearest(moment)
    return rounded if rounded >= moment else rounded + ROUND_TO


def point_at(slots: list[Interval], offset: timedelta) -> datetime:
    """把「可用时长轴上的偏移量」映射回真实时间点。"""
    consumed = timedelta(0)
    for start, end in slots:
        span = end - start
        if offset <= consumed + span:
            return start + (offset - consumed)
        consumed += span
    return slots[-1][1]


def plan_day(slots: list[Interval], count: int) -> tuple[list[datetime], int]:
    """在可用区间里均匀撒 count 个点。

    返回 (时间点列表, 实际安排数)。实际数小于请求数时说明窗口容量不够，
    调用方据此给出 adjusted 提示 —— 不报错、也不硬塞。

    撒点用「累计时长轴的中点」而不是「每个区间各分几个」：
    后者在两段区间长度悬殊时会明显不均，前者天然按容量分配。
    """
    if count <= 0:
        return [], 0

    total = total_len(slots)
    if total <= timedelta(0):
        return [], 0

    capacity = max(1, int(total / MIN_GAP))
    placed = min(count, capacity)

    points: list[datetime] = []
    for index in range(placed):
        offset = total * (2 * index + 1) // (2 * placed)
        moment = point_at(slots, offset)
        moment = _snap_nearest(moment)
        # 对齐后可能跨到区间外，夹回来
        for start, end in slots:
            if start <= moment <= end:
                break
        else:
            moment = slots[0][0]
        points.append(moment)

    return points, placed


def next_slot(slots: list[Interval], after: datetime) -> datetime | None:
    """找 after 之后最早的可安排时间点；找不到返回 None（不放回任何兜底值）。"""
    for start, end in slots:
        candidate = _snap_up(max(start, after + MIN_STEP))
        if candidate + ROUND_TO <= end:
            return candidate
    return None


def classify(spec: Spec, day: Date, moment: datetime, tz: ZoneInfo) -> str | None:
    """判定某个时间点是否合法。None 表示合法，否则返回原因代码。

    Agent 给出的建议走这里，能把「为什么不行」精确到具体原因，
    而不是笼统地说一句「不合法」。
    """
    if moment.tzinfo is None:
        return "MISSING_TIMEZONE"

    work_start = at(day, spec.work_start, tz)
    work_end = at(day, spec.work_end, tz)
    if not work_start <= moment <= work_end:
        return "OUTSIDE_WORK_HOURS"

    in_window = any(
        at(day, win_start, tz) <= moment <= at(day, win_end, tz)
        for win_start, win_end in (spec.morning, spec.afternoon)
        if win_end > win_start
    )
    if not in_window:
        return "OUTSIDE_ACTIVITY_WINDOW"

    for quiet_start, quiet_end in spec.quiet:
        if quiet_end > quiet_start and at(day, quiet_start, tz) <= moment < at(day, quiet_end, tz):
            return "IN_QUIET_HOURS"

    return None


REASON_TEXT = {
    "OUTSIDE_WORK_HOURS": "不在工作时段内",
    "OUTSIDE_ACTIVITY_WINDOW": "不在你设置的可活动时段内",
    "IN_QUIET_HOURS": "落在免打扰时段内",
    "MISSING_TIMEZONE": "时间缺少时区信息",
    "TOO_CLOSE": "与已有安排间隔过近",
}
