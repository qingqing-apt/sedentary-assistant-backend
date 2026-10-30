"""久坐活动助手 Agent：定义 + 模型配置 + AG-UI 端点挂载。

与 `app/advisor.py` 的区别：

- `advisor.py` 是一次性的「文本进、建议 JSON 出」调用，给 REST 接口
  `/api/agent/suggest` 用，无状态、无工具、无多轮。
- 本模块是真正的 Agent：有会话上下文、能连续调 6 个业务工具、按 AG-UI 协议流式输出，
  给前端的对话面板用。

两者共用同一套业务校验（`advisor.confirm` / `scheduler`），不存在两套规则。
"""

from __future__ import annotations

import logging

from agent_framework import Agent
from agent_framework.ag_ui import add_agent_framework_fastapi_endpoint
from agent_framework.openai import OpenAIChatCompletionClient

from ..config import get_config
from .scope import resolve_authorization_scope
from .tools import TOOLS

logger = logging.getLogger(__name__)

AGENT_NAME = "sedentary_agent"
AGENT_DESCRIPTION = "久坐活动助手：根据用户自己的时段安排起身活动，只碰他自己的数据。"

AGENT_PATH = "/api/agent/chat"

AGENT_INSTRUCTIONS = """你是「久坐活动助手」，服务对象是长时间坐办公桌前的人。

你的职责只有一件事：根据用户自己设定的可活动时段，帮他安排几次短暂的起身活动，
并在用户明确同意后保存下来。你不是健康顾问，也不是聊天机器人。

## 能力范围（只能通过工具操作，不要凭空回答业务数据）

- get_my_settings：读他的时段偏好（上下班、上午/下午窗口、免打扰、每天次数）
- list_today_activities：看他今天的安排和每一条的状态
- find_free_slots：算今天从某时刻起还有哪些空档
- estimate_capacity：算今天还能排几次（要条数时用它，别自己拿分钟数除）
- weekly_stats：看本周完成、推迟、跳过的次数
- propose_schedule：在空档里排几条建议，同时生成界面上那张「待确认」卡片（**不保存**）
- save_schedule：用户明确同意后才写入

## 硬约束（违反即失败）

1. **不做医疗判断**。不提供任何疾病诊断、治疗建议、用药建议、康复方案或个体化运动处方。
   用户描述身体不适、疼痛、疾病时，只回一句「这个我判断不了，建议咨询医生」，
   然后问要不要继续安排活动，不要顺着聊病情。
2. **只碰当前登录用户自己的数据**。工具签名里没有 user_id，你也不要试图指定或猜测。
3. **只用工具返回的真实数据回答**。查不到就说查不到。绝不编造安排、时间或统计数字，
   也绝不说「已经帮你安排好了」除非 save_schedule 真的返回成功。
4. **保存有严格顺序**：先 propose_schedule → 把建议逐条念给用户 → 用户明确说保存 →
   才能调 save_schedule。用户只是问「能不能排」「帮我看看」「先排排看」时，**不能保存**。
   propose_schedule 之后，建议会以卡片形式出现在用户界面上，用户可以直接点卡片上的确认按钮。
   这时你**不要**再自动调 save_schedule——要么等用户开口，要么就让他去点按钮。
   用户说「还能排几次」时调 estimate_capacity，用返回的 can_fit 回答，不要含糊其辞。
5. **保存失败要如实转告**，包括具体原因（落在免打扰、时间已过、间隔太近）。
   不要偷偷换个时间再存一次——用户有权知道是什么挡住了。

## 交互规则

6. 用户没说数量时，按他设置的每天次数安排；但如果连上午还是下午都判断不了，
   先用一句话问清楚再排，别自作主张。
7. 一次最多给 5 条建议。时段装不下就说明容量不够，不要硬塞。
8. 用简体中文，简洁，先说结论再给细节。时间一律 24 小时制 HH:MM。
9. 用户问「你能做什么」时，用两句话讲清楚，不要罗列工具名称。
10. 用户说的话和这个软件无关（写代码、问天气、闲聊）时，一句话说明你只负责活动安排，
    不要展开。
"""


def build_model_client():
    """按服务端配置构造模型 client。

    复用 `app/config.py` 里的 LLM_* 配置，和 REST 建议接口共用一套凭证，
    避免出现「对话能用、按钮不能用」这种配置不一致。
    """
    cfg = get_config()
    if not cfg.llm_enabled:
        raise RuntimeError(
            "未配置 LLM_BASE_URL / LLM_API_KEY，无法构建 Agent。"
            "请在 backend/.env 中填写后重启进程。"
        )
    logger.info("Agent 使用模型：%s @ %s", cfg.llm_model, cfg.llm_base_url)
    return OpenAIChatCompletionClient(
        model=cfg.llm_model,
        api_key=cfg.llm_api_key,
        base_url=cfg.llm_base_url,
    )


def build_agent() -> Agent:
    """构建久坐活动助手 Agent。"""
    return Agent(
        client=build_model_client(),
        name=AGENT_NAME,
        description=AGENT_DESCRIPTION,
        instructions=AGENT_INSTRUCTIONS,
        tools=TOOLS,
    )


def register_agent_endpoint(app, path: str = AGENT_PATH) -> None:
    """在 FastAPI 上挂载受登录保护的 AG-UI 端点。

    - 路径 `/api/agent/chat`，与 `/api/agent/suggest` 同族，前端走同一个 /api 代理。
    - `authorization_scope_resolver` 负责校验凭证并按用户隔离会话状态，
      未登录请求在跑模型之前就被 401 拦掉。
    """
    add_agent_framework_fastapi_endpoint(
        app,
        build_agent(),
        path,
        authorization_scope_resolver=resolve_authorization_scope,
    )
    logger.info("Agent AG-UI 端点已挂载：%s", path)
