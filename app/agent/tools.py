"""久坐活动助手的专属工具集。

这里的工具不是通用工具，每一个都直接对应这个软件的业务动作：

| 工具 | 对应业务 | 写库 |
|---|---|---|
| get_my_settings   | 用户的时段偏好 | 否 |
| list_today_activities | 今日安排列表 | 否 |
| find_free_slots   | 可用时隙求解 | 否 |
| estimate_capacity | 还能排几次（条数，不是分钟数） | 否 |
| weekly_stats      | 周统计 | 否 |
| propose_schedule  | 排几条建议 + 写一张待确认提议单 | 只写 proposals 表 |
| save_schedule     | 用户确认后落库 | 是（受控） |

安全约束（与 memo 版一致，另加一条写入约束）：

1. **数据隔离**：`user_id` 只从后端已验证身份（ContextVar）读取，工具签名不暴露该参数，
   模型和浏览器都无法指定用户。
2. **拿不到身份直接拒绝**，绝不返回任何数据。
3. **写入只有一条通道**：`save_schedule` 是 `advisor.confirm()` 的薄封装，内部跑的是
   和 REST 接口完全相同的校验（免打扰 / 过去时间 / 间隔 / 当天）。校验不过就原样报错，
   不给模型"我改一下再试"的空间——那等于绕过业务规则。
   `propose_schedule` 确实会写库，但只写 `proposals`（待确认提议单），
   不写 `activities`。所以「用户没确认就没有安排」这条规则在数据结构上成立，
   不依赖模型是否听话。
4. **数量与长度都有上限**，避免一次把过多内容塞进上下文。
"""

from __future__ import annotations

import json
import logging

from agent_framework import tool

from .. import advisor, catalog, proposals, scheduler, services
from ..db import SessionLocal
from ..errors import AppError
from ..schemas import ConfirmItem
from .scope import get_current_user_id

logger = logging.getLogger(__name__)

MAX_ITEMS = 12
MAX_PROPOSALS = 5
MAX_SAVE_ITEMS = 3

NO_IDENTITY_MSG = "拒绝访问：未获取到已登录用户身份，请先登录再试。"
DB_ERROR_MSG = "查询失败：数据库暂时不可用（错误码 DB_UNAVAILABLE），请稍后重试。"


def _safe(action: str, fn):
    """执行并在工具层兜底异常。

    只把错误码交给模型，完整异常写服务端日志，避免堆栈经模型回复泄露给前端。
    """
    try:
        return fn()
    except AppError as exc:
        return json.dumps(
            {"error": exc.code, "message": exc.message}, ensure_ascii=False
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent tool failed: action=%s error=%s", action, type(exc).__name__)
        return DB_ERROR_MSG


def _parse_after(raw: str, now):
    """把模型给的「从几点之后」解析成时间点；解析不出来就从现在开始。"""
    text = (raw or "").strip()
    if not text:
        return now
    try:
        clock = scheduler.hhmm(text)
    except (ValueError, AttributeError):
        return now
    candidate = scheduler.at(now.date(), clock, services.tz())
    return candidate if candidate > now else now


def _settings_payload(user_id: str) -> str:
    db = SessionLocal()
    try:
        row = services.get_or_create_settings(db, user_id)
        spec = services.spec_from_row(row)
        return json.dumps(
            {
                "work_start": spec.work_start.strftime("%H:%M"),
                "work_end": spec.work_end.strftime("%H:%M"),
                "morning": f"{spec.morning[0]:%H:%M}-{spec.morning[1]:%H:%M}",
                "afternoon": f"{spec.afternoon[0]:%H:%M}-{spec.afternoon[1]:%H:%M}",
                "quiet_hours": [
                    f"{start:%H:%M}-{end:%H:%M}" for start, end in spec.quiet
                ]
                or [],
                "daily_count": spec.daily_count,
            },
            ensure_ascii=False,
        )
    finally:
        db.close()


def _today_payload(user_id: str) -> str:
    db = SessionLocal()
    try:
        now = services.now_local()
        rows = services.day_rows(db, user_id, now.date())
        items = [
            {
                "time": services.parse_iso(row.scheduled_at).strftime("%H:%M"),
                "title": row.title,
                "status": row.status,
                "overdue": row.status == "planned"
                and services.parse_iso(row.scheduled_at) < now,
            }
            for row in rows
        ][:MAX_ITEMS]
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        return json.dumps(
            {
                "date": now.date().isoformat(),
                "now": now.strftime("%H:%M"),
                "total": len(rows),
                "by_status": counts,
                "items": items,
            },
            ensure_ascii=False,
        )
    finally:
        db.close()


def _slots_payload(user_id: str, after_time: str) -> str:
    db = SessionLocal()
    try:
        now = services.now_local()
        row = services.get_or_create_settings(db, user_id)
        spec = services.spec_from_row(row)
        busy = tuple(
            services.parse_iso(item.scheduled_at)
            for item in services.day_rows(db, user_id, now.date())
            if item.status == "planned"
        )
        start = _parse_after(after_time, now)
        slots = scheduler.free_slots(
            spec, now.date(), services.tz(), busy=busy, not_before=start
        )
        slots = slots[:MAX_ITEMS]
        total = int(scheduler.total_len(slots).total_seconds() // 60)
        if not slots:
            return json.dumps(
                {
                    "date": now.date().isoformat(),
                    "from": start.strftime("%H:%M"),
                    "slots": [],
                    "total_minutes": 0,
                    "note": "今天从该时刻起已经没有可用时段了。",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "date": now.date().isoformat(),
                "from": start.strftime("%H:%M"),
                "slots": [f"{a:%H:%M}-{b:%H:%M}" for a, b in slots],
                "total_minutes": total,
            },
            ensure_ascii=False,
        )
    finally:
        db.close()


def _weekly_payload(user_id: str) -> str:
    db = SessionLocal()
    try:
        stats = services.weekly(db, user_id)
        return json.dumps(
            {
                "week": f"{stats.week_start}~{stats.week_end}",
                "total": stats.total,
                "completed": stats.completed,
                "postponed": stats.postponed,
                "skipped": stats.skipped,
                "pending": stats.pending,
            },
            ensure_ascii=False,
        )
    finally:
        db.close()


def _capacity(slots) -> int:
    """这些空档最多还能放几次活动。

    直接复用 plan_day 的放置逻辑（它内部按 MIN_GAP 算容量），不要再写一套除法——
    两套算法迟早会给出不一样的数字，用户看到的就是「你刚才说能排 3 次，怎么只排了 2 条」。
    count 传一个大数，实际安排数就是容量。
    """
    _, placed = scheduler.plan_day(slots, 99)
    return placed


def _propose_payload(user_id: str, count: int, after_time: str) -> str:
    db = SessionLocal()
    try:
        now = services.now_local()
        row = services.get_or_create_settings(db, user_id)
        spec = services.spec_from_row(row)
        busy = tuple(
            services.parse_iso(item.scheduled_at)
            for item in services.day_rows(db, user_id, now.date())
            if item.status == "planned"
        )
        start = _parse_after(after_time, now)
        slots = scheduler.free_slots(
            spec, now.date(), services.tz(), busy=busy, not_before=start
        )
        wanted = max(1, min(int(count or 1), MAX_PROPOSALS))
        points, placed = scheduler.plan_day(slots, wanted)
        fits = _capacity(slots)

        offers = []
        for index, point in enumerate(points):
            template = catalog.rotating(index)
            offers.append(
                {
                    "activity_type": template.key,
                    "title": template.title,
                    "scheduled_at": services.to_iso(point),
                    "time": point.strftime("%H:%M"),
                }
            )

        if not offers:
            return json.dumps(
                {
                    "date": now.date().isoformat(),
                    "proposals": [],
                    "remaining_capacity": 0,
                    "capacity_note": "该时刻之后没有可用时段，建议放宽免打扰或改到明天。",
                    "must_confirm": True,
                },
                ensure_ascii=False,
            )

        note = f"时段容量只够放 {placed} 条（你要了 {wanted} 条）。" if placed < wanted else ""

        # 只写提议单，不写活动表 —— 用户确认前 activities 一行都不会多
        proposal = proposals.create(
            db,
            user_id,
            [
                {
                    "activity_type": item["activity_type"],
                    "title": item["title"],
                    "scheduled_at": item["scheduled_at"],
                }
                for item in offers
            ],
            note=note,
            capacity_note=f"今天还能排 {fits} 次" if fits else "",
        )

        return json.dumps(
            {
                "date": now.date().isoformat(),
                "proposal_id": proposal.id,
                "proposals": offers,
                "remaining_capacity": fits,
                "capacity_note": note,
                "must_confirm": True,
                "hint": (
                    "这些建议已经显示在用户界面的「待确认」卡片上，用户可以直接点按钮保存。"
                    "它们还没有保存：只有用户明确说要保存时，才可调用 save_schedule。"
                ),
            },
            ensure_ascii=False,
        )
    finally:
        db.close()


def _capacity_payload(user_id: str, after_time: str) -> str:
    db = SessionLocal()
    try:
        now = services.now_local()
        row = services.get_or_create_settings(db, user_id)
        spec = services.spec_from_row(row)
        planned = [
            item
            for item in services.day_rows(db, user_id, now.date())
            if item.status == "planned"
        ]
        busy = tuple(services.parse_iso(item.scheduled_at) for item in planned)
        start = _parse_after(after_time, now)
        slots = scheduler.free_slots(
            spec, now.date(), services.tz(), busy=busy, not_before=start
        )
        fits = _capacity(slots)
        return json.dumps(
            {
                "date": now.date().isoformat(),
                "from": start.strftime("%H:%M"),
                "can_fit": fits,
                "free_windows": [f"{a:%H:%M}-{b:%H:%M}" for a, b in slots[:MAX_ITEMS]],
                "already_planned": len(planned),
                "daily_count": spec.daily_count,
                "note": (
                    f"从 {start:%H:%M} 起还能安排 {fits} 次活动"
                    if fits
                    else f"从 {start:%H:%M} 起已经没有空档了"
                ),
            },
            ensure_ascii=False,
        )
    finally:
        db.close()


def _save_payload(user_id: str, items_json: str) -> str:
    """受控写入：走 advisor.confirm，校验规则与 REST 接口完全一致。"""
    try:
        raw = json.loads(items_json)
    except (TypeError, ValueError):
        return json.dumps(
            {
                "error": "INVALID_JSON",
                "message": "items 必须是 JSON 数组，例如 "
                '[{"activity_type":"walk","scheduled_at":"2026-10-30T15:40+08:00"}]',
            },
            ensure_ascii=False,
        )

    if not isinstance(raw, list) or not raw:
        return json.dumps(
            {"error": "EMPTY_ITEMS", "message": "没有需要保存的安排。"},
            ensure_ascii=False,
        )
    if len(raw) > MAX_SAVE_ITEMS:
        return json.dumps(
            {
                "error": "TOO_MANY_ITEMS",
                "message": f"一次最多保存 {MAX_SAVE_ITEMS} 条，请分批确认。",
            },
            ensure_ascii=False,
        )

    try:
        parsed = [ConfirmItem(**item) for item in raw]
    except Exception:  # noqa: BLE001
        return json.dumps(
            {
                "error": "INVALID_ITEM",
                "message": "每条建议都需要 activity_type 和 scheduled_at 两个字段。",
            },
            ensure_ascii=False,
        )

    db = SessionLocal()
    try:
        created = advisor.confirm(db, user_id, parsed)
        return json.dumps(
            {
                "saved": len(created),
                "items": [
                    {"time": item.scheduled_at[11:16], "title": item.title}
                    for item in created
                ],
            },
            ensure_ascii=False,
        )
    except AppError as exc:
        # 校验不过就如实返回，不替用户改时间重试
        return json.dumps(
            {"error": exc.code, "message": exc.message}, ensure_ascii=False
        )
    finally:
        db.close()


@tool(
    name="get_my_settings",
    description=(
        "读取当前用户自己的时段偏好：上下班时间、上午/下午可活动窗口、免打扰时段、"
        "每天期望次数。只读。用户问「我的设置」「我什么时候能活动」时调用；"
        "回答一般性问题（如打招呼、问你能做什么）时不要调用。"
    ),
)
def get_my_settings() -> str:
    """读取当前登录用户的时段设置。"""
    user_id = get_current_user_id()
    if user_id is None:
        return NO_IDENTITY_MSG
    return _safe("settings", lambda: _settings_payload(user_id))


@tool(
    name="list_today_activities",
    description=(
        "列出当前用户今天的活动安排：时间、标题、状态（planned/completed/postponed/skipped）、"
        "是否已过期。只读。用户问「今天有什么安排」「还剩几条」「刚才那条怎么样了」时调用。"
    ),
)
def list_today_activities() -> str:
    """列出今天的活动安排。"""
    user_id = get_current_user_id()
    if user_id is None:
        return NO_IDENTITY_MSG
    return _safe("today", lambda: _today_payload(user_id))


@tool(
    name="find_free_slots",
    description=(
        "计算今天从某个时刻起还有哪些可安排的时间段（已扣掉免打扰、已占用和过去时间）。只读。"
        "用户问「下午还有空吗」「三点以后什么时候能活动」时调用。"
        "after_time 用 24 小时制 HH:MM，例如 15:00；用户没说具体时刻就留空字符串。"
    ),
)
def find_free_slots(after_time: str = "") -> str:
    """计算今天剩余的可安排时隙。"""
    user_id = get_current_user_id()
    if user_id is None:
        return NO_IDENTITY_MSG
    return _safe("slots", lambda: _slots_payload(user_id, after_time))


@tool(
    name="weekly_stats",
    description=(
        "查询本周（周一起算）的统计：计划总数、已完成、已推迟、已跳过、待处理。只读。"
        "用户问「这周完成了几次」「我推迟了多少次」时调用。"
    ),
)
def weekly_stats() -> str:
    """查询本周完成情况。"""
    user_id = get_current_user_id()
    if user_id is None:
        return NO_IDENTITY_MSG
    return _safe("weekly", lambda: _weekly_payload(user_id))


@tool(
    name="estimate_capacity",
    description=(
        "计算从某个时刻起今天还能安排几次活动（直接给出条数，已扣掉免打扰、已占用和过去时间）。只读。"
        "用户问「还能排几次」「还排得下吗」「今天还有空吗」时调用。"
        "不要自己拿时隙分钟数去除，直接用返回的 can_fit——口径要和真正排程时一致。"
        "after_time 用 24 小时制 HH:MM，用户没说具体时刻就留空字符串。"
    ),
)
def estimate_capacity(after_time: str = "") -> str:
    """计算今天剩余可容纳的活动次数。"""
    user_id = get_current_user_id()
    if user_id is None:
        return NO_IDENTITY_MSG
    return _safe("capacity", lambda: _capacity_payload(user_id, after_time))


@tool(
    name="propose_schedule",
    description=(
        "在用户可用的时段里排几条活动建议。这些建议会立刻显示在用户界面的「待确认」卡片上，"
        "但**不写入活动表**，用户没点确认之前不算已安排。"
        "用户表达「安排一下」「排两次」「下午想活动一下」这类意图时调用。"
        "count 是想要几条（1-5）；after_time 是起点 HH:MM，没说就留空字符串。"
        "调用后把结果念给用户，并说明「还没保存，界面上点确认或告诉我保存即可」。"
    ),
)
def propose_schedule(count: int = 1, after_time: str = "") -> str:
    """生成活动安排建议（只写待确认提议单，不写活动）。"""
    user_id = get_current_user_id()
    if user_id is None:
        return NO_IDENTITY_MSG
    return _safe("propose", lambda: _propose_payload(user_id, count, after_time))


@tool(
    name="save_schedule",
    description=(
        "把已经向用户展示过、且用户明确表示同意保存的安排写入数据库。"
        "只有用户说了「保存」「就这么定」「可以」之后才可调用；用户只是在询问或讨论时绝不能调用。"
        "items 是 JSON 数组字符串，元素形如 "
        '{"activity_type":"walk","scheduled_at":"2026-10-30T15:40+08:00"}，'
        "activity_type 只能是 walk/water/shoulder/stretch，时间必须来自 propose_schedule 的结果。"
        "后端会重新校验免打扰、过去时间与间隔，校验不通过会返回错误，此时如实转告用户并重新 propose。"
    ),
)
def save_schedule(items: str) -> str:
    """用户确认后保存安排（受控写入）。"""
    user_id = get_current_user_id()
    if user_id is None:
        return NO_IDENTITY_MSG
    return _safe("save", lambda: _save_payload(user_id, items))


TOOLS = [
    get_my_settings,
    list_today_activities,
    find_free_slots,
    estimate_capacity,
    weekly_stats,
    propose_schedule,
    save_schedule,
]
