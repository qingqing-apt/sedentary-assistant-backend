# 久坐活动助手 · 后端

FastAPI + SQLite。负责时段设置、活动排程、状态流转、周统计，以及 Agent 建议的生成与校验。

## 本地启动

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env          # 不配 LLM 也能跑，Agent 会自动降级成规则版
uvicorn app.main:app --reload --port 8000
```

服务地址 `http://127.0.0.1:8000`，接口文档 `http://127.0.0.1:8000/docs`。

## 测试

```bash
pytest
```

| 文件 | 覆盖内容 |
|------|----------|
| `tests/test_scheduler.py` | 排程算法：区间求差集、免打扰切窗口、容量裁剪、推迟找空位、时间合法性判定 |
| `tests/test_api.py` | 接口链路：设置校验、生成幂等、完成/推迟/跳过、终止态拦截、越权访问、免打扰拒绝、Agent 建议与确认 |
| `tests/test_agent_llm.py` | 建议引擎的模型容错：脏数据逐条拦截、断连降级、空建议降级、二次校验、时间围栏剥离 |

测试用了一个可替换的时钟（`services.set_clock`），所以不管几点跑都稳定。全部 39 个用例 1.4 秒跑完，不联网。

### Agent 行为评测

```bash
python -m tests.agent_evaluate --label v1
```

跑 15 个用例（11 个真调模型 + 3 个工具层 + 1 个协议层），覆盖能力边界、工具使用、容量口径、越权、未确认保存、医疗红线、鉴权。报告写到 `tests/agent_results/<label>.md`。

也可以只跑指定用例：`--cases C05,C06`；重复跑看稳定性：`--repeat 3`。

## 目录

```
app/
  scheduler.py   排程算法（纯函数，无 IO，先看这个）
  services.py    业务层：状态流转、生成、统计
  advisor.py     按钮式建议引擎：单次 LLM 调用 + 校验 + 确认落库
  proposals.py   Agent 提议单：建议先挂起，用户确认才写活动表
  agent/         对话式 Agent（Microsoft Agent Framework + AG-UI）
    scope.py           登录校验、按用户隔离的会话 scope
    tools.py           7 个业务工具（5 读 1 写提议单 1 受控写活动）
    sedentary_agent.py Agent 定义、模型 client、端点挂载
  models.py      ORM 模型
  schemas.py     出入参模型
  errors.py      统一错误体 {code, message, field}
  deps.py        鉴权（练习版，可替换成第二天的登录）
  routers/       路由
  catalog.py     活动模板库
```

**`advisor.py` 和 `agent/` 是两套东西，不是重复实现。** 前者是无状态的一次性调用，给按钮用；后者有会话上下文和工具，给对话面板用。两者共用同一套业务校验（`advisor.confirm` / `scheduler`），不存在两套规则。

## 几个刻意的设计取舍

**时间存成带 `+08:00` 的 ISO 字符串，不用 SQLite 的 DateTime。**
SQLite 不保留时区，读回来是 naive，和 `datetime.now()` 一比就整体偏 8 小时。存字符串没有这个坑。另外冗余一个 `day` 列（`YYYY-MM-DD`），今日安排和周统计都按它查。

**推迟 = 原记录终结 + 派生新记录。**
不是原地改 `scheduled_at`。因为周统计要数「推迟了几次」，原地改会让这个指标永远归零，也丢掉追溯链。旧记录变 `postponed`，新记录带 `postponed_from` 指回旧 id。

**排程是纯函数。**
`scheduler.py` 不碰数据库、不碰时钟、不碰全局状态，边界情况可以在毫秒级跑完。业务层只负责取数据、调函数、写库。

**Agent 没有写权限。**
只能调只读上下文、返回建议；落库必须走 `/api/activities/confirm`，那里会重新跑一遍校验。用户可能在这中间改了设置，所以第二遍不能省。

## 环境变量

见 `.env.example`。`LLM_BASE_URL` / `LLM_API_KEY` 留空时 Agent 走规则版，功能完整可用。

接入 DeepSeek 只需三行：

```
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxxxxxxx
LLM_MODEL=deepseek-chat
```

改完重启进程（`--reload` 不会监听 `.env`），`/api/health` 返回 `"llm_enabled": true` 即生效。前端 Agent 面板的副标题会从「规则建议」自动变成「模型建议」。

### 实测表现（DeepSeek deepseek-chat）

| 场景 | 结果 |
|---|---|
| 单次调用耗时 | 1.1 – 1.4s |
| 连续 5 轮不同说法 | 5/5 命中真模型，无一次降级 |
| 输出格式 | 时区偏移、活动类型全部合规，无需人工修 |
| 模型返回脏数据 | 6 条中 5 条被精确拦截（免打扰/过去时间/非法类型/跨天/格式错），1 条合规入库 |
| 模型完全不可用 | 自动降级 `mode=rule`，接口仍 200 且有可用建议 |

脏数据与降级的用例已固化为 `tests/test_agent_llm.py`（7 个），跑测试不联网。
