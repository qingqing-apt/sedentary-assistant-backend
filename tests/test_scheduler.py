"""排程算法的单元测试。

覆盖的三个关键边界（也是最容易写错的三个）：
1. 免打扰嵌在活动窗口中间 —— 必须把窗口切成两段，而不是整段丢弃
2. 免打扰横跨两个窗口之间的空档 —— 不应该影响任何窗口
3. 窗口容量不足以安排请求的次数 —— 裁剪而不是报错
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import scheduler
from app.scheduler import Spec

TZ = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 10, 30)


def make_spec(**overrides) -> Spec:
    base = dict(
        work_start=time(9, 0),
        work_end=time(18, 0),
        morning=(time(10, 0), time(12, 0)),
        afternoon=(time(14, 0), time(17, 30)),
        quiet=(),
        daily_count=3,
    )
    base.update(overrides)
    return Spec(**base)


def hhmm(value: str) -> datetime:
    hour, minute = value.split(":")
    return datetime.combine(DAY, time(int(hour), int(minute)), tzinfo=TZ)


def as_text(intervals) -> list[str]:
    return [f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}" for start, end in intervals]


def test_merge_joins_overlapping_and_adjacent():
    merged = scheduler.merge([(hhmm("10:00"), hhmm("11:00")), (hhmm("11:00"), hhmm("12:00"))])
    assert as_text(merged) == ["10:00-12:00"]


def test_subtract_hollows_out_middle():
    remaining = scheduler.subtract(
        [(hhmm("10:00"), hhmm("12:00"))], [(hhmm("11:00"), hhmm("11:30"))]
    )
    assert as_text(remaining) == ["10:00-11:00", "11:30-12:00"]


def test_free_slots_without_quiet_hours():
    slots = scheduler.free_slots(make_spec(), DAY, TZ)
    assert as_text(slots) == ["10:00-12:00", "14:00-17:30"]


def test_quiet_hour_inside_afternoon_splits_it():
    """关键用例：免打扰嵌在窗口中间，窗口必须被切成两段。"""
    spec = make_spec(quiet=((time(15, 0), time(15, 30)),))
    slots = scheduler.free_slots(spec, DAY, TZ)
    assert as_text(slots) == ["10:00-12:00", "14:00-15:00", "15:30-17:30"]


def test_quiet_hour_between_windows_changes_nothing():
    """午休 12:00-13:30 落在两个窗口之间的空档，不应该切掉任何东西。"""
    spec = make_spec(quiet=((time(12, 0), time(13, 30)),))
    slots = scheduler.free_slots(spec, DAY, TZ)
    assert as_text(slots) == ["10:00-12:00", "14:00-17:30"]


def test_quiet_hour_covering_whole_day_leaves_nothing():
    spec = make_spec(quiet=((time(0, 0), time(23, 59)),))
    assert scheduler.free_slots(spec, DAY, TZ) == []
    points, placed = scheduler.plan_day([], spec.daily_count)
    assert points == [] and placed == 0


def test_plan_day_never_lands_inside_quiet_hours():
    spec = make_spec(quiet=((time(15, 0), time(15, 30)),), daily_count=5)
    slots = scheduler.free_slots(spec, DAY, TZ)
    points, placed = scheduler.plan_day(slots, spec.daily_count)

    assert placed == 5
    for point in points:
        assert not (hhmm("15:00") <= point < hhmm("15:30")), point


def test_plan_day_spreads_evenly_by_capacity():
    slots = scheduler.free_slots(make_spec(daily_count=3), DAY, TZ)
    points, placed = scheduler.plan_day(slots, 3)
    assert placed == 3
    assert [p.strftime("%H:%M") for p in points] == ["10:55", "14:45", "16:35"]


def test_plan_day_shrinks_when_window_too_small():
    """容量不足时裁剪，不报错、不硬塞。"""
    spec = make_spec(morning=(time(10, 0), time(10, 40)), afternoon=(time(10, 0), time(10, 40)))
    slots = scheduler.free_slots(spec, DAY, TZ)
    points, placed = scheduler.plan_day(slots, 3)
    assert placed == 1
    assert len(points) == 1


def test_busy_activity_blocks_its_neighbourhood():
    busy = (hhmm("14:00"),)
    slots = scheduler.free_slots(make_spec(), DAY, TZ, busy=busy)
    assert as_text(slots) == ["10:00-12:00", "14:10-17:30"]


def test_next_slot_finds_later_time():
    slots = scheduler.free_slots(make_spec(), DAY, TZ)
    assert scheduler.next_slot(slots, hhmm("16:35")).strftime("%H:%M") == "17:05"


def test_next_slot_returns_none_when_day_is_full():
    """推迟到无可推迟时必须返回 None，由上层转成明确提示。"""
    slots = scheduler.free_slots(make_spec(), DAY, TZ)
    assert scheduler.next_slot(slots, hhmm("17:25")) is None


def test_next_slot_skips_quiet_hours():
    spec = make_spec(quiet=((time(15, 0), time(15, 30)),))
    slots = scheduler.free_slots(spec, DAY, TZ)
    assert scheduler.next_slot(slots, hhmm("14:55")).strftime("%H:%M") == "15:30"


@pytest.mark.parametrize(
    "moment,expected",
    [
        ("09:30", "OUTSIDE_ACTIVITY_WINDOW"),
        ("12:30", "OUTSIDE_ACTIVITY_WINDOW"),
        ("19:00", "OUTSIDE_WORK_HOURS"),
        ("10:30", None),
        ("14:00", None),
    ],
)
def test_classify(moment, expected):
    assert scheduler.classify(make_spec(), DAY, hhmm(moment), TZ) == expected


def test_classify_rejects_quiet_hour_edges():
    spec = make_spec(quiet=((time(15, 0), time(15, 30)),))
    assert scheduler.classify(spec, DAY, hhmm("15:00"), TZ) == "IN_QUIET_HOURS"
    assert scheduler.classify(spec, DAY, hhmm("15:29"), TZ) == "IN_QUIET_HOURS"
    assert scheduler.classify(spec, DAY, hhmm("15:30"), TZ) is None


def test_plan_day_ignores_moments_before_now():
    slots = scheduler.free_slots(make_spec(), DAY, TZ, not_before=hhmm("15:00"))
    assert as_text(slots) == ["15:00-17:30"]
    points, placed = scheduler.plan_day(slots, 3)
    assert all(point >= hhmm("15:00") for point in points)
