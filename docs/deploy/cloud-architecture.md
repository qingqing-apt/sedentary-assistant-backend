# 云端架构与 CI/CD 方案（阿里云 · 测试环境）

> 结论先行：这台应用**只有三个进程、一个 SQLite 文件、没有队列和独立数据库**。
> 所以云端不需要「一套微服务」，需要的是**一台 ECS、三个容器、一个唯一入口、一条可回滚的发布流水线**。
> 本文件描述目标形态；`deploy/` 下的实际文件在 P2 阶段才落盘，当前阶段不产生任何服务器变更。

---

## 一、真实服务现状（先对齐，再设计）

任务书假设「前端 + Python API + Agent Runtime + Redis + ARQ Worker + 数据库」六个组件。按仓库代码逐个核对的结果：

| 组件 | 代码中是否存在 | 证据 | 部署结论 |
|---|---|---|---|
| 前端静态站 | 存在 | `frontend/` Vite 构建产物 `dist/` | 打进 nginx 镜像 |
| Python API | 存在 | `backend/app/main.py`，uvicorn :8000 | 容器 `api` |
| Agent Runtime | 存在 | `frontend/runtime/server.ts`，Express + CopilotKit，:3000 | 容器 `runtime` |
| 业务数据库 | 存在但是**文件** | `sqlite:///./data/app.db`，无 DB 服务进程 | 卷 `data`，不是独立服务 |
| Redis | **不存在** | 全仓库无 `redis` 依赖、无连接串 | 不部署 |
| ARQ Worker | **不存在** | 无 `arq`、无 worker 入口、无队列名 | 不部署 |
| 消息队列 / Celery | **不存在** | — | 不部署 |

**这条必须写进部署记录**：任务书任务 5.3 要求「若实际实现使用 Redis + ARQ，验证任务由 Worker 处理」。当前实现里日报是**同步生成**（`app/digest.py` 在一次请求内调模型，失败降级规则版），不存在异步任务。所以这一条只能是「未实现，无法验收」，不能声称队列部署成功，也不能为了对上文档在云端临时拼一套未审核的队列。

真实拓扑：

```
浏览器 / Android App
        │  HTTPS 443
        ▼
   ┌─────────┐
   │  edge   │  nginx：静态资源 + 反向代理 + TLS
   └────┬────┘
        ├── /              → 容器内 dist（前端构建产物）
        ├── /api/copilotkit → runtime:3000（CopilotKit，转发 AG-UI）
        └── /api/*          → api:8000（FastAPI）
                                  │
                                  ├── data 卷：/app/data/app.db（SQLite，单写者）
                                  └── LLM（DeepSeek，可缺省 → 规则降级）
   runtime:3000 ── AGENT_URL ──► api:8000/api/agent/chat
                └── threads 卷：/app/.runtime-data/threads（对话历史落盘）
```

三个必须遵守的推论：

1. **API 只能单 worker。** SQLite 单文件 + 进程内 ContextVar 身份，`uvicorn --workers 1`，不能横向扩。
2. **Runtime 是有状态的。** 对话线程写在 `THREADS_DIR`，必须挂卷，否则重启即丢（第七天刚修过这件事）。
3. **前端和 API 必须同源。** 后端 CORS 白名单只写死了 localhost；同源部署才不需要改 CORS，也才躲得开跨域预检。

---

## 二、为什么不是「前端放 OSS/S3 + 函数计算」

可以放静态文件，但**这个前端不是纯静态站**：

| 约束 | 后果 |
|---|---|
| `/api/copilotkit` 必须由 Node Runtime 转发 AG-UI 流 | OSS 静态托管不会代理到 :3000，助手页签直接失效（就是第七天那个「发消息没反应」） |
| 后端 CORS 只允许 localhost | 静态站换成 `xxx.oss-cn-hangzhou.aliyuncs.com` 后，所有 `/api` 请求变成跨域，必须改后端白名单并处理预检 |
| 登录态是 `Authorization: Bearer` | 跨域 + 自定义头 = 每个请求一次 OPTIONS，测试环境不值得 |
| 两个发布单元（静态站 + API） | 版本错配时前端会静默打到旧接口，排查成本高于省下的那点带宽 |

**结论**：测试环境用一个 nginx 容器同时干「静态托管 + 反代」，前端构建时 `VITE_API_BASE` 留空走相对路径，天然同源。
生产阶段若确实要 CDN，也应该是 **OSS/CDN 只承载静态资源，`/api` 与 `/api/copilotkit` 仍走统一入口**，并且要先把前端改成绝对地址 + 配 CORS —— 那是另一次变更，不在今天范围。

---

## 三、目标拓扑（测试环境）

```
                    公网（仅 443）
                         │
        ┌────────────────┴────────────────┐
        │            阿里云 VPC            │
        │  ┌───────────────────────────┐  │
        │  │ ECS（2C2G，Ubuntu 22.04）  │  │
        │  │  EIP ── 安全组：443 / 22*  │  │
        │  │                           │  │
        │  │  docker compose           │  │
        │  │   ├─ edge   :80 :443      │  │
        │  │   ├─ api    (不发布端口)   │  │
        │  │   └─ runtime(不发布端口)   │  │
        │  │                           │  │
        │  │  卷 data / threads        │  │
        │  │  /opt/sedentary/          │  │
        │  │    .env  certs/ backups/  │  │
        │  └───────────────────────────┘  │
        │            │                    │
        │      ACR（镜像仓库，同地域）      │
        └─────────────────────────────────┘
* 22 沿用现有管理员策略，不因本次部署改动
```

### 容器清单

| 服务 | 镜像 | 端口 | 挂载 | 重启策略 |
|---|---|---|---|---|
| `edge` | `sedentary-web:<sha>`（nginx:alpine + dist + conf） | 宿主 80/443 | `/opt/sedentary/certs:/etc/nginx/certs:ro` | `unless-stopped` |
| `api` | `sedentary-api:<sha>` | 仅 expose 8000 | `data:/app/data` | `unless-stopped` |
| `runtime` | `sedentary-runtime:<sha>` | 仅 expose 3000 | `threads:/app/.runtime-data` | `unless-stopped` |

关键指令（示意，实际文件在 `frontend/deploy/compose.yaml`；部署脚本 `frontend/deploy/deploy.sh`，手册 `frontend/deploy/README.md`）：

> `deploy/` 放在前端仓库：三个镜像里有两个（web、runtime）由它构建，边缘 nginx 配置也在这里。
> `nginx.conf` 在构建时烤进镜像，不从宿主机挂载 —— 路由规则跟着 tag 走，回滚镜像时路由一起回滚。

```yaml
services:
  api:
    image: ${ACR}/sedentary-api:${API_TAG:?missing}
    env_file: [.env]
    expose: ["8000"]
    volumes: [data:/app/data]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 20s
    init: true

  runtime:
    image: ${ACR}/sedentary-runtime:${RUNTIME_TAG:?missing}
    environment:
      AGENT_URL: http://api:8000/api/agent/chat
      THREADS_DIR: /app/.runtime-data/threads
    expose: ["3000"]
    volumes: [threads:/app/.runtime-data]
    depends_on:
      api: {condition: service_healthy}
    healthcheck:
      test: ["CMD", "node", "-e", "fetch('http://127.0.0.1:3000/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]

  edge:
    image: ${ACR}/sedentary-web:${WEB_TAG:?missing}
    ports: ["80:80", "443:443"]
    volumes: ["/opt/sedentary/certs:/etc/nginx/certs:ro"]
    depends_on:
      api: {condition: service_healthy}
      runtime: {condition: service_healthy}
```

`${TAG:?missing}` 是刻意的：缺 tag 时 compose 直接报错，不会退化成 `latest` 悄悄拉错版本。

### 健康检查的三层含义

| 层 | 检查 | 通过说明什么 |
|---|---|---|
| 容器进程 | `docker ps` / healthcheck | 进程没退出 |
| 服务 | 容器内 `GET /api/health`、`GET /health` | FastAPI / Runtime 能应答 |
| 链路 | **从 CI runner 上**请求 `https://<域名>/api/health` | 证书、安全组、nginx、反代、后端全通 |

任务书要求「健康检查不能只检查容器进程存在」，所以三层都要有；第三层必须在**外部机器**上执行，服务器本机 curl 不算验收。

`edge` 的 healthcheck 只证明 nginx 活着，不能代表后端通 —— 所以 edge 的健康检查单独看，链路用第三层兜。

---

## 四、网络与安全组

| 规则 | 来源 | 端口 | 用途 |
|---|---|---|---|
| 入站 | `0.0.0.0/0`（或负责人指定白名单） | 443 | 唯一公开入口 |
| 入站 | 管理员 IP | 22 | 沿用现有策略，**本次不改** |
| 出站 | 全部 | — | 拉镜像、调模型、系统更新 |
| 其余入站 | 拒绝 | — | 8000 / 3000 / 2375 / 2376 / 3306 / 5432 一律不开放 |

- 容器层面只有 `edge` 发布端口，`api`/`runtime` 只 `expose` 在 compose 内部网络，**安全组和 Docker 端口映射两道都不通**。
- Docker daemon 只用本机 Unix socket，**不监听 TCP**（不碰 2375/2376）。
- 没有域名/证书时：不发布 80/443 到公网，安全组只放行管理员 IP，用自签证书或 SSH 隧道访问。任务书明确禁止「把登录页以无保护 HTTP 公开到互联网」。
- 这是 demo 登录（任意用户名即可换 token），没有真实鉴权。即使上了 HTTPS，也建议在 nginx 加一层 Basic Auth 作为临时第二道门，等真实鉴权上线再撤。

---

## 五、数据、备份与回滚

| 数据 | 位置 | 重要性 | 备份方式 |
|---|---|---|---|
| 业务库 `app.db` | 卷 `data` → `/app/data` | 高 | SQLite 在线备份 API（不是 `cp`） |
| 对话线程 | 卷 `threads` → `/app/.runtime-data` | 中 | 打包 tar |
| `.env`（模型 Key） | `/opt/sedentary/.env`，权限 600 | 高 | 不进 Git、不进镜像、不进 CI 日志；由负责人线下保管 |

**为什么不能用 `cp` 备份 SQLite**：数据库可能处于 WAL 模式并有未合并的 `-wal` 文件，`cp` 出来的文件可能不一致。正确做法是走 SQLite 的 backup API：

```bash
docker run --rm \
  -v sedentary_data:/data:ro \
  -v /opt/sedentary/backups:/backup \
  <api-image> python -c "
import sqlite3
src = sqlite3.connect('/data/app.db')
dst = sqlite3.connect('/backup/app-$(date +%Y%m%d-%H%M%S).db')
src.backup(dst); dst.close(); src.close()
"
```

回滚演练（任务书任务 4）按「不碰现网数据」执行：

1. 回退镜像 tag 到 `state/last-good.json` 里记录的上一版 → 验证 `/api/health` 与页面；
2. 把备份**还原到独立位置**（`/opt/sedentary/restore/app-test.db`），以只读方式打开并数行数，证明备份可读；
3. **不覆盖**当前 `app.db`。要覆盖必须单独申请批准。

明令禁止当作常规手段：`docker compose down -v`、`docker system prune --volumes`、`rm -rf data/`。

---

## 六、CI/CD（GitHub Actions）

### 6.1 流水线

```
PR ──► backend-ci / frontend-ci（测试 + 构建）
         │ 通过 + 人工 review
         ▼
      merge master
         │
         ▼
   打 tag v* / 手动触发 release
         │
         ├─ 构建镜像，tag = sha-<7位>（不可变）+ vX.Y.Z（人读）
         └─ 推送 ACR
                │
                ▼
     deploy.yml（手动触发 + GitHub Environment 人工批准）
         │
         ├─ 预检：镜像存在？磁盘够？配置合法？
         ├─ 备份 SQLite
         ├─ pull → 起 api → 等健康 → 起 runtime → 起 edge
         ├─ 外部链路健康检查（从 runner 打域名）
         └─ 成功写部署记录；失败自动回滚到 last-good
```

### 6.2 仓库与职责

| 仓库 | 负责 | workflow |
|---|---|---|
| `sedentary-assistant-backend` | API 镜像、`deploy/`（compose/nginx/脚本）、部署与回滚 | `ci.yml`（已落盘）、`release.yml`、`deploy.yml`、`rollback.yml` |
| `sedentary-assistant-frontend` | web 镜像、runtime 镜像、APK（手动） | `ci.yml`（已落盘）、`release.yml` |

部署配置放后端仓库的原因：它管着「怎么把三个镜像拼起来跑」，前端仓库不该知道服务器布局。

### 6.3 密钥与权限

| 名称 | 类型 | 存哪 | 内容 |
|---|---|---|---|
| `ACR_REGISTRY` / `ACR_NAMESPACE` | 变量 | 仓库 | `crpi-1bv9mjbeu8xhh314.cn-guangzhou.personal.cr.aliyuncs.com` / `gcu` |
| `ACR_USERNAME` / `ACR_PASSWORD` | 密钥 | 仓库 | **只用于推送**的 ACR 凭据（限定该命名空间） |
| `ECS_HOST` / `ECS_USER` / `ECS_SSH_KEY` | 密钥 | Environment `test` | 部署通道 |
| `DEPLOY_DOMAIN` | 变量 | Environment `test` | 用于外部健康检查 |
| LLM Key / DB 密码 | **不进 CI** | 仅服务器 `/opt/sedentary/.env`（600） | 由负责人按安全渠道提供 |

- GitHub Environment `test` 配置 **Required reviewers = 负责人**，部署必须人工点批准。
- 所有 workflow 顶部 `permissions: contents: read`，不授予 `write`。
- 不用 `pull_request_target`，不让 fork PR 拿到任何密钥。
- 官方 `actions/*` 用主版本标签；第三方 action 一律钉完整 commit SHA（本方案的实际 YAML 里没有第三方 action，SSH 用系统 `ssh` 命令）。

### 6.4 部署通道：首选 self-hosted runner

| 方案 | 优点 | 代价 |
|---|---|---|
| **ECS 上装 self-hosted runner（推荐）** | 只需出网，**不需要把 22 放开给 GitHub**；部署在本机执行，链路最短 | 业务机上多一个常驻 agent，且它在 `docker` 组≈root 权限 |
| GitHub 托管 runner + SSH | 不改服务器 | 需要安全组放行 22 给 GitHub 的出网 IP（且 IP 会变），与「不改 SSH 策略」冲突 |

任务书要求「不要直接改 SSH 端口 / 规则，避免切断管理员访问」，所以默认选 self-hosted runner。
降低风险的三条：runner 只注册到这两个私有仓库、只被 `deploy.yml` 使用、部署 job 必须经过 Environment 人工批准。

### 6.5 已落盘的 CI（现在就能跑）

| 文件 | 触发 | 内容 |
|---|---|---|
| `backend/.github/workflows/ci.yml` | PR / push master | Python 3.13 + `pip install -r requirements.txt` + `pytest`（75 例） |
| `frontend/.github/workflows/ci.yml` | PR / push master | Node 22 + `npm ci` + `npm test`（25 例）+ `npm run build` + Playwright 响应式回归（6 例） |

CI 里没有 Docker 构建 —— 镜像构建放在 `release.yml`，等 Dockerfile 合入后再启用，避免提交一个必定失败的流水线。

### 6.6 发布/部署契约（`deploy/deploy.sh`）

| 子命令 | 作用 | 会改数据吗 |
|---|---|---|
| `check` | 只读：磁盘、compose 配置合法性（`docker compose config --quiet`）、当前 tag、容器状态 | 否 |
| `apply <api> <web> <runtime>` | 备份 → pull → 按依赖起容器 → 健康检查 → 写部署记录 | 备份文件 |
| `status` | 当前 tag、健康状态、近 20 行日志 | 否 |
| `rollback [last-good\|<record>]` | 用记录里的旧 tag 重新 `up -d`，不动卷 | 否 |

硬规则写进脚本，不靠人记：

1. 不写 `.env`（CI 永远不碰它）；
2. 不用 `down -v` / `prune` / 删卷；
3. 不执行数据库迁移（本项目没有迁移框架，改表要走代码评审 + 手工备份）；
4. `apply` 任一步失败 → 自动 `rollback last-good` 并让 job 失败；
5. 每次 `apply` 前把旧 tag 写进 `state/last-good.json`，这是回滚唯一的依据。

状态文件只有三个（都在 `deploy/state/`，不进 Git）：

| 文件 | 含义 |
|---|---|
| `current.json` | 现在正在跑的 api/web/runtime tag + 部署时间 |
| `last-good.json` | 上一次验证通过的版本；`rollback` 默认读它，回滚后会与 current 互换，所以能滚回去 |
| `history.jsonl` | 每次 apply/rollback 追加一行，事后溯源用 |

### 6.7 不做自动化的事

数据库迁移、删卷/清数据、安全组与防火墙变更、SSH 配置变更、重启整台服务器、TLS 证书签发（用宿主上的 certbot/acme.sh 定时任务，不给 CI root）、Android release 签名、模型密钥轮换 —— 这些都需要人的判断，交给人。

---

## 七、可观测性

| 维度 | 手段 | 位置 |
|---|---|---|
| 容器状态 | `docker compose ps`（含 health 列） | 部署记录 + 手册 |
| 分服务日志 | `docker compose logs --tail=200 api\|runtime\|edge` | 服务器 |
| 日志容量 | compose `logging.options.max-size: 10m / max-file: 3` | 防止撑爆磁盘 |
| 资源 | `docker stats --no-stream`、`df -h` | 部署前 `check` 例会 |
| 宿主监控 | 阿里云云监控（CPU/内存/磁盘/网络） | 控制台 |
| 业务断言 | `/api/health` 的 `llm_enabled` / `agent_chat_ready` | 外部健康检查 |

错误分三类，排查顺序固定：
1. **服务不可达** —— nginx 502/504 或 curl 超时 → 看 `docker compose ps` 与 `edge` 日志；
2. **配置错误** —— 502 但容器健康 → 看 `AGENT_URL`、`DATABASE_URL`、`.env` 是否生效；
3. **业务失败** —— 接口 4xx/409 → 看 API 日志的业务错误码（`NO_AVAILABLE_SLOT` 等），不是部署问题。

---

## 八、生产演进（不是今天做，但决定了今天不能踩的坑）

| 现在（测试） | 生产目标 | 触发迁移的条件 |
|---|---|---|
| 单台 ECS | SLB/ALB + 2 台 ECS 或 ACK | 需要 SLA / 滚动发布 |
| SQLite 文件 | RDS PostgreSQL（或 PolarDB） | 并发写冲突、需要多实例 |
| ACR 个人版 | ACR 企业版（VPC 内网拉取、镜像扫描） | 合规要求 / 镜像审计 |
| 单机卷 | NAS（对话历史、上传） | 需要跨实例共享 |
| 宿主 certbot | SLB 证书 + 自动续期 | 域名接入 |
| GitHub Actions 直连 | 云效或 Actions + 审批门禁 | 公司统一发布平台 |
| 无 WAF | WAF + DDoS 基础防护 | 公网真实用户 |

今天必须守住的「未来不返工」点：镜像 tag 用 SHA、配置全走环境变量、数据全在卷里、前端构建不写死 IP（用域名）、日志不 print 密钥。

---

## 九、成本（学生账号，以控制台为准）

| 项 | 档位 | 说明 |
|---|---|---|
| ECS | 2C2G / 40G ESSD，包年 | 三个容器 + Docker 的最低舒适配置；1C1G 会在构建/启动时吃紧 |
| EIP | 按量 | 测试期可带宽计费 |
| ACR | 个人版 | 免费额度足够三个镜像 |
| 证书 | 免费 DV | 单域名 |
| 总计 | 百元级/年 | 不包含域名 |

---

## 十、分阶段执行（对应今天的节奏）

| 阶段 | 内容 | 产出 | 是否动服务器 |
|---|---|---|---|
| **P0** 盘点 | 本文件 + 任务书核对 + commit 冻结 | 架构方案、服务现状表 | 否 |
| **P1** 前置修复 | 3 个小 PR（见下节） | 可构建、可健康检查 | 否 |
| **P2** 容器化 | `Dockerfile`×3、`compose.yaml`、`nginx.conf`、`.dockerignore`、`deploy.sh` | 本机能 `docker compose up` | 否 |
| **P3** CI | 两个仓库 `ci.yml`（已落盘）+ 分支保护 | PR 必过测试 | 否 |
| **P4** CD | ACR、`release.yml`、`deploy.yml`、`rollback.yml`、首次部署 | 可复现发布 | **是**（获授权后） |
| **P5** 验收 | 外部访问 / Android / 备份还原 / 回滚演练 / 手册 | 交付文档 | 是（演练级） |

---

## 十一、部署前置问题（P1，必须先修）

| # | 问题 | 影响 | 修法 |
|---|---|---|---|
| 1 | `frontend/runtime/durable-runner.ts` 第 32 行含 NUL 字节 | Git 把该文件判为二进制，diff/评审失效，PR 里看不到改动 | 换成普通哨兵字符串，单独一个提交 |
| 2 | `runtime/server.ts` 引入 `cors`，但 `package.json` 未声明 | 干净环境 `npm ci` 后依赖提升顺序变化就可能 `MODULE_NOT_FOUND` | 显式加进 `dependencies` |
| 3 | Runtime 没有 `/health` | 健康检查只能探端口，无法判断「进程活着但服务没用」 | 加 3 行路由，返回 `{status:"ok"}` |
| 4 | `tsx` 在 `devDependencies`，而生产用 `tsx runtime/server.ts` 启动 | Runtime 镜像要么装全量依赖（体积大），要么起不来 | 把 `tsx` 移到 `dependencies`，或在构建阶段编译产物 |
| 5 | Runtime 无优雅退出 | 部署时 `docker stop` 可能截断正在流式返回的对话 | 监听 SIGTERM 后 `server.close()` |
| 6 | 根 `README.md` 仍写「50 个测试」「要持久化需换 Redis 版 runner」 | 与现状（75 个测试、DurableAgentRunner）不一致，误导部署者 | 随 P2 PR 一并更新 |

---

## 十二、风险与未决项

| 项 | 级别 | 说明 | 处置 |
|---|---|---|---|
| Redis + ARQ 不存在 | 高 | 任务书 5.3 无法按原文验收 | 写进部署记录「未实现」，请负责人确认本迭代是否要补 |
| demo 登录无真实鉴权 | 高 | 任意用户名可换 token | 仅限受控访问 + nginx Basic Auth 兜底，不做公网裸奔 |
| 单机单点 + SQLite 单写者 | 中 | 机器挂 = 服务挂；不能多实例 | 测试环境接受；生产前迁 RDS |
| 域名 / 证书未定 | 中 | 没有 HTTPS 就无法做外部验收 | 先封闭访问（IP 白名单/自签），拿到域名再补 |
| ACR 命名空间 / RAM 子账号未定 | 中 | 影响 `release.yml` 能否跑 | 见下节确认项 |
| 部署通道未定 | 中 | self-hosted runner 是否允许装 | 默认按 self-hosted 设计，可切 SSH |
| 无实体手机 | 低 | 只有模拟器 | 联调结论标注「模拟器已验证」 |
| `.runtime-data` 无清理 | 低 | 对话历史只增不减 | 测试期忽略，加容量观察 |

---

## 十三、待确认（已确认 1 项）

| # | 事项 | 状态 |
|---|---|---|
| 1 | CI/CD 平台用 GitHub Actions | ✅ 已确认 |
| 2 | 是否有 ACR 命名空间 / RAM 子账号（若无，用个人版固定凭据先跑通） | ⏳ |
| 3 | ECS 是否允许安装 self-hosted runner（不允许则改 SSH + 临时放行 22） | ⏳ |
| 4 | 域名 / 证书由谁提供（没有则先封闭访问） | ⏳ |

在 2–4 确认前，`release.yml` / `deploy.yml` 只以本文件的骨架形式存在，不会落盘成可执行文件。

---

## 附录 A：`deploy.yml` 骨架（P4 落盘）

```yaml
name: deploy-test

on:
  workflow_dispatch:
    inputs:
      api_tag:     { required: true,  type: string }
      web_tag:     { required: true,  type: string }
      runtime_tag: { required: true,  type: string }
      dry_run:     { required: false, type: boolean, default: false }

permissions:
  contents: read

concurrency:
  group: deploy-test
  cancel-in-progress: false

jobs:
  preflight:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: 校验三个镜像都存在
        env:
          ACR_USER: ${{ secrets.ACR_USERNAME }}
          ACR_PASS: ${{ secrets.ACR_PASSWORD }}
        run: |
          echo "$ACR_PASS" | docker login "${{ vars.ACR_REGISTRY }}" -u "$ACR_USER" --password-stdin
          for ref in \
            "${{ vars.ACR_REGISTRY }}/${{ vars.ACR_NAMESPACE }}/sedentary-api:${{ inputs.api_tag }}" \
            "${{ vars.ACR_REGISTRY }}/${{ vars.ACR_NAMESPACE }}/sedentary-web:${{ inputs.web_tag }}" \
            "${{ vars.ACR_REGISTRY }}/${{ vars.ACR_NAMESPACE }}/sedentary-runtime:${{ inputs.runtime_tag }}"; do
            docker manifest inspect "$ref" > /dev/null
            echo "ok: $ref"
          done

  deploy:
    needs: preflight
    runs-on: [self-hosted, linux, x64, sedentary-test]
    environment: test          # 必须人工批准
    steps:
      - uses: actions/checkout@v4
      - name: 同步部署文件（不碰 .env）
        run: |
          rsync -a --delete deploy/ /opt/sedentary/deploy/
      - name: 部署
        working-directory: /opt/sedentary
        env:
          API_TAG:     ${{ inputs.api_tag }}
          WEB_TAG:     ${{ inputs.web_tag }}
          RUNTIME_TAG: ${{ inputs.runtime_tag }}
          DRY_RUN:     ${{ inputs.dry_run }}
        run: bash /opt/sedentary/deploy/deploy.sh apply "$API_TAG" "$WEB_TAG" "$RUNTIME_TAG"
      - name: 外部链路验收
        if: ${{ !inputs.dry_run }}
        run: |
          curl -fsS --max-time 10 "https://${{ vars.DEPLOY_DOMAIN }}/api/health" | tee /tmp/health.json
          # 助手链路不能被前端 SPA 兜底成 text/html（第七天那个“假成功”）
          ct=$(curl -s -o /dev/null -w '%{content_type}' -X POST "https://${{ vars.DEPLOY_DOMAIN }}/api/copilotkit" \
                -H 'content-type: application/json' -d '{}')
          case "$ct" in text/html*) echo "::error::/api/copilotkit 返回 HTML，Runtime 未生效"; exit 1;; esac
      - name: 失败自动回滚
        if: failure() && !inputs.dry_run
        run: bash /opt/sedentary/deploy/deploy.sh rollback last-good
```

`rollback.yml` 同构，只保留 `rollback` 一个输入与一个步骤。

## 附录 B：环境变量模板（仅占位符）

```ini
# /opt/sedentary/.env   权限 600，不进 Git / 镜像 / CI
DATABASE_URL=sqlite:////app/data/app.db
APP_TIMEZONE=Asia/Shanghai
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=<由负责人提供，不入库>
LLM_MODEL=deepseek-chat
LLM_TIMEOUT=30

# compose 插值
ACR=crpi-1bv9mjbeu8xhh314.cn-guangzhou.personal.cr.aliyuncs.com
```

个人版实例固定在**广州**：一个账号只能在一个地域创建个人版实例，ECS 也应选在同一地域。
同地域 ECS 可改用内网域名 `crpi-1bv9mjbeu8xhh314-vpc.cn-guangzhou.personal.cr.aliyuncs.com` 拉取，
不消耗公网流量；CI 在公网，推送仍用上面那个公网域名。

注意 `DATABASE_URL` 在容器里是**四个斜杠**（`sqlite:///` + 绝对路径 `/app/data/app.db`），与本地相对路径写法不同。

## 附录 C：验收映射（任务书 → 本方案）

| 任务书条目 | 本方案对应 | 交付证据 |
|---|---|---|
| 3.1 服务器与代码盘点 | P0 + 第一节 | 盘点记录、commit 冻结 |
| 3.3 服务拓扑 | 第一节拓扑图 | 本文件 |
| 3.4 Redis/ARQ 核对 | 第一节 | 「未实现」结论 + 负责人确认 |
| 4 容器部署配置 | 第三节 | compose / Dockerfile / nginx.conf |
| 5.1 HTTPS 可访问 | 第四节 | 外部 curl + 截图 |
| 5.2 Android 联调 | P5 | 截图（Token/IP 打码） |
| 5.3 日报链路 | 附录 C 第 4 行 | 「同步生成，无队列」说明 |
| 5.6 内部服务未暴露 | 第四节 | 外部端口探测结果 |
| 6 备份与回滚 | 第五节 | 备份文件清单 + 回滚记录 |
| 7 文档交付 | 本文件 + 部署手册 | PR |
