"""久坐活动助手 Agent 评测执行器。

用法（在 backend 目录下）：
    ./.venv/Scripts/python.exe -m tests.agent_evaluate --label baseline
    ./.venv/Scripts/python.exe -m tests.agent_evaluate --label after --cases C05,C06
    ./.venv/Scripts/python.exe -m tests.agent_evaluate --label after --repeat 2

产物：
    tests/agent_results/<label>.json   完整结果
    tests/agent_results/<label>.md     Markdown 报告

注意：`agent` 类用例会真实调用 DeepSeek，会产生 token 消耗（单条约 1.5k 输入）。
`tool` / `http` 类用例不调模型，不花钱。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

BACKEND_URL = "http://127.0.0.1:8000"
RESULTS_DIR = Path(__file__).parent / "agent_results"

EVAL_USERNAME = "eval_user"

# 固定评测数据（虚构），保证每次跑的结果可比
EVAL_SETTINGS = {
    "work_start": "09:00",
    "work_end": "18:00",
    "morning_start": "10:00",
    "morning_end": "12:00",
    "afternoon_start": "14:00",
    "afternoon_end": "17:30",
    "daily_count": 3,
    "quiet_hours": [{"start": "15:00", "end": "15:30"}],
    "reminders_enabled": True,
}


def prepare_data() -> None:
    """重置评测用户自己的数据，只动 eval_user，不碰演示账号。"""
    from app import models, services
    from app.db import Base, SessionLocal, engine
    from app.schemas import SettingsIn

    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        db.query(models.Activity).filter(models.Activity.user_id == EVAL_USERNAME).delete()
        db.query(models.ActivitySettings).filter(
            models.ActivitySettings.user_id == EVAL_USERNAME
        ).delete()
        db.commit()

        services.save_settings(db, EVAL_USERNAME, SettingsIn(**EVAL_SETTINGS))

        # 造几条今日安排：2 完成、1 跳过、1 待处理，给统计类问题提供可验证的答案
        now = services.now_local()
        today = now.date()
        plan = [
            ("walk", "起身走动", "10:55", "completed"),
            ("water", "去接杯水", "14:45", "completed"),
            ("shoulder", "肩颈放松", "16:35", "skipped"),
            ("stretch", "站立伸展", "17:10", "planned"),
        ]
        for key, title, clock, status in plan:
            hour, minute = clock.split(":")
            moment = datetime.combine(
                today,
                datetime.min.time().replace(hour=int(hour), minute=int(minute)),
                tzinfo=services.tz(),
            )
            db.add(
                models.Activity(
                    user_id=EVAL_USERNAME,
                    day=today.isoformat(),
                    activity_type=key,
                    title=title,
                    description="",
                    scheduled_at=services.to_iso(moment),
                    status=status,
                    source="user",
                )
            )
        db.commit()
    finally:
        db.close()


def extract_tool_calls(resp) -> list[dict]:
    calls: list[dict] = []
    for msg in getattr(resp, "messages", None) or []:
        for item in getattr(msg, "contents", None) or []:
            if getattr(item, "type", None) == "function_call":
                args = getattr(item, "arguments", None)
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:  # noqa: BLE001
                        args = {"_raw": args}
                calls.append(
                    {"name": getattr(item, "name", None), "arguments": args or {}}
                )
    return calls


async def run_agent_case(agent, case) -> "CaseResult":
    from app.agent.scope import _current_user_id
    from tests.agent_cases import CaseResult

    _current_user_id.set(EVAL_USERNAME)
    start = time.time()
    try:
        resp = await agent.run(case.input)
        return CaseResult(
            text=getattr(resp, "text", "") or "",
            tool_calls=extract_tool_calls(resp),
            usage=getattr(resp, "usage_details", None),
            elapsed=time.time() - start,
        )
    except Exception as exc:  # noqa: BLE001
        return CaseResult(error=f"{type(exc).__name__}: {exc}", elapsed=time.time() - start)


def run_tool_case(case) -> "CaseResult":
    """直接调工具函数，绕过模型，结果确定。"""
    from app.agent import tools
    from app.agent.scope import _current_user_id
    from tests.agent_cases import CaseResult

    if case.tool_probe == "no_identity":
        _current_user_id.set(None)
    else:
        _current_user_id.set(EVAL_USERNAME)

    start = time.time()
    try:
        if case.tool_probe == "bad_time":
            out = tools.find_free_slots.func("乱码时间")
        elif case.tool_probe == "propose":
            out = tools.propose_schedule.func(2, "")
        else:
            out = tools.list_today_activities.func()
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            error=f"{type(exc).__name__}: {exc}", elapsed=time.time() - start
        )

    return CaseResult(tool_output=out or "", elapsed=time.time() - start)


async def run_http_case(case) -> "CaseResult":
    from tests.agent_cases import CaseResult

    start = time.time()
    try:
        # trust_env=False：访问本机服务不经过系统代理，避免被代理拦成 502
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            resp = await client.post(
                f"{BACKEND_URL}/api/agent/chat",
                json={
                    "threadId": f"eval-{case.id}",
                    "runId": f"run-{case.id}",
                    "messages": [{"id": "m1", "role": "user", "content": "hi"}],
                    "tools": [],
                    "context": [],
                    "forwardedProps": {},
                },
            )
        return CaseResult(status_code=resp.status_code, elapsed=time.time() - start)
    except Exception as exc:  # noqa: BLE001
        return CaseResult(error=f"{type(exc).__name__}: {exc}", elapsed=time.time() - start)


def write_reports(label: str, records: list[dict]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "label": label,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(records),
        "passed": sum(1 for r in records if r["passed"]),
        "records": records,
    }
    (RESULTS_DIR / f"{label}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    passed = payload["passed"]
    total = payload["total"]
    lines = [
        f"# Agent 评测报告 · {label}",
        "",
        f"- 时间：{payload['generated_at']}",
        f"- 通过：**{passed}/{total}**",
        "",
        "| 用例 | 类别 | 结果 | 说明 | 工具调用 |",
        "|---|---|---|---|---|",
    ]
    for r in records:
        mark = "通过" if r["passed"] else "**失败**"
        tools = ", ".join(r["tool_names"]) or "-"
        lines.append(
            f"| {r['id']} | {r['category']} | {mark} | {r['note']} | {tools} |"
        )
    lines.append("")
    lines.append("## 完整回答")
    for r in records:
        lines.append("")
        lines.append(f"### {r['id']} · {r['input'][:40]}")
        lines.append("")
        lines.append(f"- 期望：{r['expected']}")
        lines.append(f"- 工具：{', '.join(r['tool_names']) or '未调用'}")
        lines.append(f"- 回答：{r['text'][:400]}")

    (RESULTS_DIR / f"{label}.md").write_text("\n".join(lines), encoding="utf-8")


async def main() -> None:
    parser = argparse.ArgumentParser(description="久坐助手 Agent 评测")
    parser.add_argument("--label", default="run", help="本次结果标签，用作文件名")
    parser.add_argument("--cases", default="", help="只跑指定用例，逗号分隔，如 C05,C06")
    parser.add_argument("--repeat", type=int, default=1, help="每个用例重复次数")
    args = parser.parse_args()

    from tests.agent_cases import CASES

    picked = CASES
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",") if c.strip()}
        picked = [c for c in CASES if c.id in wanted]

    needs_agent = any(c.mode == "agent" for c in picked)
    agent = None
    if needs_agent:
        from app.agent.sedentary_agent import build_agent

        prepare_data()
        agent = build_agent()

    records: list[dict] = []
    for case in picked:
        rounds = args.repeat if case.mode == "agent" else 1
        for attempt in range(rounds):
            if case.mode == "agent":
                result = await run_agent_case(agent, case)
            elif case.mode == "tool":
                result = run_tool_case(case)
            else:
                result = await run_http_case(case)

            passed, note = case.check(result)
            names = [c.get("name") or "?" for c in result.tool_calls]
            text = result.text or result.tool_output
            records.append(
                {
                    "id": case.id,
                    "category": case.category,
                    "input": case.input,
                    "expected": case.expected,
                    "passed": passed,
                    "note": note,
                    "tool_names": names,
                    "text": text,
                    "elapsed": round(result.elapsed, 2),
                    "error": result.error,
                    "status_code": result.status_code,
                    "attempt": attempt + 1,
                }
            )
            flag = "PASS" if passed else "FAIL"
            print(f"[{flag}] {case.id} ({result.elapsed:.1f}s) {note}")

    write_reports(args.label, records)
    ok = sum(1 for r in records if r["passed"])
    print(f"\n通过 {ok}/{len(records)}，报告见 {RESULTS_DIR / (args.label + '.md')}")


if __name__ == "__main__":
    asyncio.run(main())
