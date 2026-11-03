"""每日总结的模型生成（失败降级到规则版）。

和 `advisor.py` 一样的立场：**模型是增强，不是依赖**。

- 没配 `LLM_BASE_URL` / `LLM_API_KEY` → 直接用规则版，`source="rule"`。
- 模型超时、报错、返回脏数据 → 同样降级，`source="rule"`，不让接口 500。
- 只有返回结构合法（headline 是字符串、points 是非空字符串数组）才用，
  `source="agent"`。

为什么校验这么严：这段文字会直接展示给用户，还要落库。模型偶尔会把 JSON
裹进代码块、少给字段、或者把 headline 写成三段话——任何一种都该降级，
而不是把半成品存进去。
"""

from __future__ import annotations

import json
import re

import httpx

from .config import get_config

SYSTEM_PROMPT = """你是「久坐活动助手」的每日总结模块。

根据给定的当天统计，写一段给用户看的简短总结。要求：
- headline：一句话结论，不超过 30 个字，不要感叹号堆砌。
- points：1 到 3 条，每条不超过 60 字，只陈述事实和一条可执行建议。
- 不要编造统计里没有的数字，不要提「作为 AI」。
- 语气平实，像同事提醒，不像客服。

只输出 JSON：{"headline": "...", "points": ["...", "..."]}"""

MAX_HEADLINE = 40
MAX_POINTS = 3
MAX_POINT_LEN = 90


def _strip_fence(text: str) -> str:
    """模型爱把 JSON 裹进 ```json 代码块，剥掉围栏再解析。"""
    return re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()


def _user_prompt(stats) -> str:
    return (
        f"日期：{stats.date}\n"
        f"计划条数：{stats.total}\n"
        f"完成：{stats.completed}\n"
        f"推迟：{stats.postponed}\n"
        f"跳过（今天不方便）：{stats.skipped}\n"
        f"待处理：{stats.pending}\n"
        f"完成率：{stats.completion_rate}%"
    )


def parse_summary(text: str) -> tuple[str, list[str]]:
    """解析并校验模型输出。任何不合规都抛 ValueError，由上层降级。"""
    raw = json.loads(_strip_fence(text))
    if not isinstance(raw, dict):
        raise ValueError("不是对象")

    headline = raw.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        raise ValueError("headline 缺失")
    headline = headline.strip()
    if len(headline) > MAX_HEADLINE:
        raise ValueError("headline 太长")

    points_raw = raw.get("points")
    if not isinstance(points_raw, list) or not points_raw:
        raise ValueError("points 缺失")

    points: list[str] = []
    for item in points_raw[:MAX_POINTS]:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("points 里有空项")
        text_item = item.strip()
        if len(text_item) > MAX_POINT_LEN:
            raise ValueError("point 太长")
        points.append(text_item)

    return headline, points


async def compose(stats) -> tuple[str, list[str], str]:
    """优先用模型，失败返回规则版。返回 (headline, points, source)。"""
    cfg = get_config()
    if not cfg.llm_enabled:
        from .services import _compose  # 延迟导入，避免循环依赖

        headline, points = _compose(stats)
        return headline, points, "rule"

    payload = {
        "model": cfg.llm_model,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(stats)},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=cfg.llm_timeout) as client:
            response = await client.post(
                f"{cfg.llm_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        headline, points = parse_summary(content)
        return headline, points, "agent"
    except Exception:
        # 超时、断连、Key 失效、返回脏数据——全部走规则版。
        # 这里故意不记录具体异常到响应里：用户不需要知道模型怎么了，
        # 但排查时看后端日志能看到（下面这行）。
        from .services import _compose

        headline, points = _compose(stats)
        return headline, points, "rule"
