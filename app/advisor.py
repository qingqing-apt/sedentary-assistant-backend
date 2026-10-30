"""Agent 层。

三条硬规则，写死了的：

1. Agent **没有写权限**。它只能拿只读上下文 + 返回建议，落库必须走 confirm 接口。
2. 建议要过两遍校验：生成时一遍（给用户看为什么被拒），确认时再一遍
   （因为用户可能在这中间改了设置）。
3. 模型不通就降级成规则版，绝不因为模型挂了让整个功能不可用。
"""

from __future__ import annotations

import json
import re

import httpx
from sqlalchemy.orm import Session

from . import catalog, scheduler, services
from .config import get_config
from .errors import AppError

SYSTEM_PROMPT = """你是「久坐活动助手」的安排建议模块。

你的任务是：根据用户的工作时段、可活动时段、免打扰时段和今天已有的安排，
提出接下来的活动安排建议。

严格约束：
- 只能在给定的「可安排时隙」里选时间点。
- 每个时间点必须带时区偏移，格式如 2026-10-30T16:00:00+08:00。
- 活动类型只能从这些里选：walk（起身走动）、water（接水）、shoulder（肩颈放松）、stretch（站立伸展）。
- 不要给任何医疗、诊断、治疗、用药或运动处方类建议。只描述「做什么动作」。
- 不给建议时宁可不给，不要编造。

只输出 JSON，不要输出任何解释性文字或 Markdown 代码块：
{"note": "一句话说明这次建议的依据", "suggestions": [{"activity_type": "walk", "scheduled_at": "..."}]}
"""

_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}


def _requested_count(message: str, default: int) -> int:
    """离线规则版也能听懂「今天只想安排两次」这种话。"""
    match = re.search(r"([1-9]|一|两|二|三|四|五|六)\s*次", message or "")
    if not match:
        return default
    token = match.group(1)
    value = _CN_NUM.get(token) or int(token)
    return max(1, min(value, 10))


def _context_text(spec, day, slots, existing) -> str:
    lines = [
        f"今天是 {day.isoformat()}。",
        f"工作时段 {spec.work_start.strftime('%H:%M')}-{spec.work_end.strftime('%H:%M')}。",
        f"可活动时段：上午 {spec.morning[0].strftime('%H:%M')}-{spec.morning[1].strftime('%H:%M')}，"
        f"下午 {spec.afternoon[0].strftime('%H:%M')}-{spec.afternoon[1].strftime('%H:%M')}。",
        f"每天期望安排 {spec.daily_count} 次。",
    ]
    if spec.quiet:
        quiet = "、".join(f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in spec.quiet)
        lines.append(f"免打扰时段：{quiet}。")
    else:
        lines.append("没有设置免打扰时段。")

    if existing:
        done = "；".join(
            f"{item.title} {services.parse_iso(item.scheduled_at).strftime('%H:%M')}（{item.status}）"
            for item in existing
        )
        lines.append(f"今天已有安排：{done}。")
    else:
        lines.append("今天还没有安排。")

    if slots:
        windows = "、".join(
            f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}" for start, end in slots
        )
        lines.append(f"当前可安排时隙：{windows}。")
    else:
        lines.append("当前没有任何可安排时隙。")

    return "\n".join(lines)


def parse_content(text: str) -> dict:
    """模型经常把 JSON 裹进 ```json 代码块里，这里剥掉围栏再解析。

    解析失败会抛异常，由上层捕获后降级为规则版——宁可降级，不要半截数据。
    """
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    return json.loads(cleaned)


async def _call_llm(cfg, context: str, message: str) -> dict:
    payload = {
        "model": cfg.llm_model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"{context}\n\n用户补充说明：{message or '（无）'}"},
        ],
    }
    url = f"{cfg.llm_base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=cfg.llm_timeout) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

    content = data["choices"][0]["message"]["content"]
    return parse_content(content)


def _validate(raw: dict, spec, day, slots, now) -> dict:
    suggestions: list[dict] = []
    rejected: list[dict] = []
    seen: set[str] = set()

    for item in (raw.get("suggestions") or [])[:10]:
        raw_time = str(item.get("scheduled_at", ""))
        try:
            moment = services.parse_iso(raw_time)
        except ValueError:
            rejected.append(
                {"scheduled_at": raw_time, "reason": "INVALID_TIME", "message": "时间格式无法识别"}
            )
            continue

        if moment.date() != day:
            rejected.append(
                {"scheduled_at": raw_time, "reason": "NOT_TODAY", "message": "建议不在今天"}
            )
            continue
        if moment < now:
            rejected.append(
                {"scheduled_at": raw_time, "reason": "IN_PAST", "message": "建议时间已经过去"}
            )
            continue

        reason = scheduler.classify(spec, day, moment, services.tz())
        if reason:
            rejected.append(
                {
                    "scheduled_at": raw_time,
                    "reason": reason,
                    "message": scheduler.REASON_TEXT.get(reason, "时间不合法"),
                }
            )
            continue

        try:
            template = catalog.template_for(item.get("activity_type"))
        except AppError as exc:
            rejected.append(
                {"scheduled_at": raw_time, "reason": exc.code, "message": exc.message}
            )
            continue

        stamp = services.to_iso(moment)
        if stamp in seen:
            continue
        seen.add(stamp)

        suggestions.append(
            {
                "activity_type": template.key,
                "title": template.title,
                "description": template.description,
                "scheduled_at": stamp,
            }
        )

    return {
        "mode": "llm",
        "note": str(raw.get("note") or "以下是结合你当前时段给出的建议。"),
        "suggestions": suggestions,
        "rejected": rejected,
    }


def _rule_based(spec, slots, now, pending_count: int, message: str, prefix: str) -> dict:
    wanted = _requested_count(message, max(0, spec.daily_count - pending_count))
    points, placed = scheduler.plan_day(slots, wanted)

    suggestions = [
        {
            "activity_type": template.key,
            "title": template.title,
            "description": template.description,
            "scheduled_at": services.to_iso(point),
        }
        for point, template in zip(points, (catalog.rotating(index) for index in range(placed)))
    ]

    if not slots:
        note = f"{prefix}今天没有剩余的可活动时段了，可以放宽免打扰或改到明天。"
    elif not suggestions:
        note = f"{prefix}今天已经排满 {spec.daily_count} 次，不再增加新安排。"
    else:
        note = f"{prefix}按你的时段挑了 {len(suggestions)} 个空档，确认后才会保存。"

    return {"mode": "rule", "note": note, "suggestions": suggestions, "rejected": []}


async def suggest(db: Session, user_id: str, message: str) -> dict:
    cfg = get_config()
    row = services.get_or_create_settings(db, user_id)
    spec = services.spec_from_row(row)
    moment = services.now_local()
    day = moment.date()

    existing = services.day_rows(db, user_id, day)
    pending = [item for item in existing if item.status == "planned"]
    busy = tuple(services.parse_iso(item.scheduled_at) for item in pending)
    slots = scheduler.free_slots(spec, day, services.tz(), busy=busy, not_before=moment)

    if not cfg.llm_enabled:
        return _rule_based(spec, slots, moment, len(pending), message, "（未配置模型，使用规则建议）")

    context = _context_text(spec, day, slots, existing)
    try:
        raw = await _call_llm(cfg, context, message)
    except Exception:
        return _rule_based(spec, slots, moment, len(pending), message, "（模型暂时不可用，已降级为规则建议）")

    result = _validate(raw, spec, day, slots, moment)
    if not result["suggestions"] and not result["rejected"] and slots:
        return _rule_based(spec, slots, moment, len(pending), message, "（模型没有给出可用建议，改用规则建议）")
    return result


def confirm(db: Session, user_id: str, items: list) -> list:
    """用户确认后的落库入口。这里必须重跑一遍校验，不能因为「是 Agent 给的」就放行。"""
    row = services.get_or_create_settings(db, user_id)
    spec = services.spec_from_row(row)
    moment = services.now_local()
    day = moment.date()

    existing = services.day_rows(db, user_id, day)
    busy = [services.parse_iso(item.scheduled_at) for item in existing if item.status == "planned"]

    if not items:
        raise AppError(400, "EMPTY_CONFIRM", "没有需要保存的建议")

    created = []
    for index, item in enumerate(items):
        try:
            target = services.parse_iso(item.scheduled_at)
        except ValueError:
            raise AppError(400, "INVALID_TIME_FORMAT", "建议时间格式无法识别", "scheduled_at") from None

        if target.date() != day:
            raise AppError(400, "NOT_TODAY", "只能保存今天的安排", "scheduled_at")

        reason = scheduler.classify(spec, day, target, services.tz())
        if reason:
            raise AppError(
                400,
                reason,
                f"{target.strftime('%H:%M')} {scheduler.REASON_TEXT.get(reason, '时间不合法')}，请重新选择时间",
                "scheduled_at",
            )

        if any(abs((target - busy_at).total_seconds()) < 600 for busy_at in busy):
            raise AppError(
                400,
                "TOO_CLOSE",
                f"{target.strftime('%H:%M')} 和已有安排间隔太近，换一个时间点吧",
                "scheduled_at",
            )

        template = catalog.template_for(item.activity_type)
        record = services.Activity(
            user_id=user_id,
            day=day.isoformat(),
            activity_type=template.key,
            title=template.title,
            description=template.description,
            scheduled_at=services.to_iso(target),
            status="planned",
            source="agent",
        )
        db.add(record)
        created.append(record)
        busy.append(target)

    db.commit()
    return [services.activity_out(item, moment) for item in created]
