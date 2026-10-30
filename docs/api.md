# 久坐活动助手 · 接口说明

Base URL：`http://127.0.0.1:8000`
所有需要登录的接口带 `Authorization: Bearer <token>`。

失败统一返回：

```json
{ "code": "NO_AVAILABLE_SLOT", "message": "今天已经没有更晚的可活动时段了…", "field": "scheduled_at" }
```

`code` 用于前端分支判断，`message` 直接给用户看，`field` 可选。

---

## 认证

### POST /api/auth/login

练习版登录，任意非空用户名即可。

```json
{ "username": "intern" }
```

返回 `{ "token": "demo.intern", "user_id": "intern" }`

---

## 设置

### GET /api/settings

未设置过时返回默认值（09:00–18:00，上午 10:00–12:00，下午 14:00–17:30，每天 3 次）。

### PUT /api/settings

```json
{
  "work_start": "09:00",
  "work_end": "18:00",
  "morning_start": "10:00",
  "morning_end": "12:00",
  "afternoon_start": "14:00",
  "afternoon_end": "17:30",
  "quiet_hours": [{ "start": "12:00", "end": "13:30" }],
  "daily_count": 3,
  "reminders_enabled": true,
  "allow_during_work": true,
  "postpone_strategy": "next_slot"
}
```

校验：时间格式 `HH:MM`；工作开始早于结束；两个活动窗口必须落在工作时段内；免打扰起止合法；`daily_count` 在 1–10。

| 错误码 | 含义 |
|--------|------|
| `INVALID_TIME_FORMAT` | 时间不是 `HH:MM` |
| `INVALID_TIME_RANGE` | 开始时间不早于结束时间 |
| `WINDOW_OUTSIDE_WORK_HOURS` | 活动窗口超出工作时段 |
| `INVALID_DAILY_COUNT` | 次数不在 1–10 |

---

## 活动安排

### GET /api/activities/today

返回今天的活动，按时间升序：

```json
[
  {
    "id": "6f2c…",
    "activity_type": "walk",
    "title": "起身走动",
    "description": "离开座位走动两三分钟，顺便去接杯水再回来。",
    "scheduled_at": "2026-10-30T10:55+08:00",
    "status": "planned",
    "source": "user",
    "postponed_from": null,
    "is_overdue": false
  }
]
```

`status` 取值：`planned` / `completed` / `postponed` / `skipped`。
`is_overdue` 是派生字段：状态还是 `planned` 但时间已经过去。

### POST /api/activities/generate?force=false

按当前设置生成今天的安排。

- 当天已有安排时**不重复生成**，原样返回已有数据，`created` 为 0。
- `force=true` 会先清掉今天所有 `planned` 的记录再重排，已完成的记录保留（统计口径不能动）。
- 时段容量不够时不会报错，而是少排几条，`adjusted=true` 并在 `message` 里说明。

```json
{
  "created": 3,
  "adjusted": false,
  "message": null,
  "activities": [ … ]
}
```

### PATCH /api/activities/{id}/complete

`planned → completed`。已是终止态时返回 409 `INVALID_TRANSITION`。

### PATCH /api/activities/{id}/skip

`planned → skipped`。语义是「今天不方便」，这条活动当天不再出现。

### PATCH /api/activities/{id}/postpone

`planned → postponed`，同时派生一条新的 `planned` 记录（`postponed_from` 指向原 id）。

```json
{
  "previous_id": "6f2c…",
  "activity": { "id": "a1b2…", "scheduled_at": "2026-10-30T17:05+08:00", "postponed_from": "6f2c…", … }
}
```

找不到更晚的空位时返回 **409 `NO_AVAILABLE_SLOT`**，原记录保持不变：

```json
{ "code": "NO_AVAILABLE_SLOT", "message": "今天已经没有更晚的可活动时段了。可以改到明天，或者把免打扰时段缩短一点再试。" }
```

### POST /api/activities/confirm

保存用户确认的 Agent 建议。**这里会重新跑一遍完整校验**，不因为「是 Agent 给的」就放行。

```json
{
  "items": [{ "activity_type": "walk", "scheduled_at": "2026-10-30T16:00:00+08:00" }]
}
```

| 错误码 | 含义 |
|--------|------|
| `IN_QUIET_HOURS` | 落在免打扰时段 |
| `OUTSIDE_ACTIVITY_WINDOW` | 不在可活动时段 |
| `OUTSIDE_WORK_HOURS` | 不在工作时段 |
| `TOO_CLOSE` | 与已有安排间隔小于 10 分钟 |
| `NOT_TODAY` | 不是今天的安排 |
| `INVALID_ACTIVITY_TYPE` | 活动类型不存在 |

---

## Agent 建议单（Proposal）

对话里的 Agent 调 `propose_schedule` 时，建议不会写进活动表，而是落成一张**待确认建议单**。
用户在界面上点确认之前，`GET /api/activities/today` 里看不到这些时间点。

状态机：

```
pending ──确认──> confirmed    （写 activities）
pending ──放弃──> discarded    （什么都不写）
pending ──新建议──> superseded （同一用户同一天只留一张待确认）
```

### GET /api/proposals/pending

当前用户今天的待确认建议，没有就返回 `null`。

```json
{
  "id": "9f2c…",
  "day": "2026-10-30",
  "items": [
    { "activity_type": "walk", "title": "起身走动", "scheduled_at": "2026-10-30T14:30:00+08:00" },
    { "activity_type": "water", "title": "去接杯水", "scheduled_at": "2026-10-30T16:40:00+08:00" }
  ],
  "note": "",
  "capacity_note": "今天还能排 4 次",
  "status": "pending",
  "created_at": "2026-10-30T10:42:00+08:00"
}
```

### POST /api/proposals/{id}/confirm

只保存用户勾选的那几条（可以少于原建议）。落库前仍走 `advisor.confirm()` 的完整校验，
错误码与 `POST /api/activities/confirm` 完全一致。

```json
{ "items": [{ "activity_type": "walk", "scheduled_at": "2026-10-30T14:30:00+08:00" }] }
```

```json
{
  "proposal": { "id": "9f2c…", "status": "confirmed", "items": [ … ] },
  "activities": [ { "id": "a1b2…", "status": "planned", "source": "agent", … } ]
}
```

| 错误码 | 含义 |
|--------|------|
| `PROPOSAL_NOT_FOUND` | 单子不存在，或不是自己的（不泄露「存在过」） |
| `PROPOSAL_NOT_PENDING` | 已经确认/放弃/被新建议顶掉 |
| 其余同 `POST /api/activities/confirm` | 校验未通过时**单子保持 pending**，用户改完还能再点一次 |

### POST /api/proposals/{id}/discard

放弃整张单子，返回更新后的建议单。不产生任何活动。

---

## 统计

### GET /api/stats/weekly

按周一至周日统计。

```json
{
  "week_start": "2026-10-26",
  "week_end": "2026-11-01",
  "total": 5,
  "completed": 2,
  "postponed": 1,
  "skipped": 1,
  "pending": 1,
  "daily": [{ "date": "2026-10-26", "total": 0, "completed": 0 }, …]
}
```

`total` 是本周创建过的全部安排条数，包含由推迟派生出来的那些 —— 所以「推迟」这个数字才能被统计到。

---

## Agent

### POST /api/agent/suggest

```json
{ "message": "下午比较忙，今天只安排两次" }
```

返回建议，**不落库**。

```json
{
  "mode": "llm",
  "note": "按你的时段挑了 2 个空档，确认后才会保存。",
  "suggestions": [
    { "activity_type": "stretch", "title": "站立伸展", "description": "…", "scheduled_at": "2026-10-30T11:20+08:00" }
  ],
  "rejected": [
    { "scheduled_at": "2026-10-30T15:10+08:00", "reason": "IN_QUIET_HOURS", "message": "落在免打扰时段内" }
  ]
}
```

- `mode` 为 `llm`（真模型）或 `rule`（模型未配置/调用失败时的降级）。
- `rejected` 是模型给出但被后端业务规则否掉的建议，前端要把原因展示给用户，不能悄悄丢掉。

### POST /api/agent/chat（AG-UI，对话式 Agent）

不是普通 JSON 接口，走 [AG-UI 协议](https://docs.ag-ui.com/introduction)，响应是 SSE 事件流。

请求：

```json
{
  "threadId": "t-1",
  "runId": "r-1",
  "messages": [{ "id": "m1", "role": "user", "content": "下午三点以后还有空吗？" }],
  "tools": [],
  "context": [],
  "forwardedProps": {}
}
```

必须带 `Authorization: Bearer <token>`。**未登录或凭证无效返回 401，且不会进入模型推理**——校验发生在 `authorization_scope_resolver` 里。

响应片段：

```
data: {"type":"RUN_STARTED","threadId":"t-1","runId":"r-1"}
data: {"type":"TOOL_CALL_START","toolCallName":"find_free_slots",...}
data: {"type":"TOOL_CALL_RESULT","content":"{\"slots\":[\"15:30-17:30\"]}",...}
data: {"type":"TEXT_MESSAGE_CONTENT","delta":"下午"}
data: {"type":"TEXT_MESSAGE_CONTENT","delta":"三点"}
...
```

前端不直接调这个接口，而是经 CopilotKit Runtime（`:3000/api/copilotkit`）转发，Runtime 负责按用户隔离线程。

可用工具：`get_my_settings` / `list_today_activities` / `find_free_slots` / `weekly_stats` / `propose_schedule` / `save_schedule`。前五个只读，`save_schedule` 是受控写入，内部走 `/api/activities/confirm` 同一套校验。

### GET /api/health

```json
{ "status": "ok", "llm_enabled": false, "agent_chat_ready": false }
```

- `agent_chat_ready` 表示 `/api/agent/chat` 是否已挂载。模型未配置或 agent-framework 导入失败时为 `false`，此时按钮式建议仍可用。
