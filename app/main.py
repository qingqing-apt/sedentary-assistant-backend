from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_config
from .db import init_db
from .errors import install_error_handlers
from .routers import activities, agent, auth, proposals, settings, stats

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title=get_config().app_name, version="1.0.0", lifespan=lifespan)

# 本地开发允许 Vite 的默认端口跨域；生产环境应改成具体域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        # Capacitor APK：WebView 的 origin（androidScheme=http → http://localhost）
        "http://localhost",
        "https://localhost",
        "capacitor://localhost",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

install_error_handlers(app)

app.include_router(auth.router)
app.include_router(settings.router)
app.include_router(activities.router)
app.include_router(stats.router)
app.include_router(proposals.router)
app.include_router(agent.router)


def _mount_agent_endpoint() -> None:
    """挂载 Agent Framework 的 AG-UI 对话端点。

    模型没配、或 agent-framework 没装时只记一条警告就跳过——对话功能降级，
    不能因为它把整个应用拖崩。按钮式建议（/api/agent/suggest）不受影响。
    """
    app.state.agent_chat_ready = False
    if not get_config().llm_enabled:
        logger.warning("未配置模型，跳过 Agent 对话端点（/api/agent/chat 不可用）")
        return
    try:
        from .agent import AGENT_PATH, register_agent_endpoint

        register_agent_endpoint(app)
        app.state.agent_chat_ready = True
        logger.info("Agent 对话端点可用：%s", AGENT_PATH)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Agent 端点挂载失败（%s: %s），对话功能不可用", type(exc).__name__, exc
        )


_mount_agent_endpoint()


@app.get("/api/health", tags=["meta"], summary="健康检查")
def health() -> dict:
    return {
        "status": "ok",
        "llm_enabled": get_config().llm_enabled,
        "agent_chat_ready": getattr(app.state, "agent_chat_ready", False),
    }
