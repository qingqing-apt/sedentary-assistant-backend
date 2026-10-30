"""Agent 包：Microsoft Agent Framework 实现的久坐活动助手。

- `scope`：登录校验与用户身份隔离
- `tools`：6 个业务工具（5 读 1 受控写）
- `sedentary_agent`：Agent 定义与 AG-UI 端点挂载
"""

from .sedentary_agent import AGENT_NAME, AGENT_PATH, build_agent, register_agent_endpoint

__all__ = ["AGENT_NAME", "AGENT_PATH", "build_agent", "register_agent_endpoint"]
