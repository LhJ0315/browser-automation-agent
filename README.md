# Browser Automation Agent

自然语言 / JSON 计划 → Playwright 浏览器执行 → 截图与报告归档。提供 CLI、FastAPI + Redis/RQ 异步任务、模板变量、Webhook 通知与闭环 Agent 模式。

**核心链路：** `plan → execute → archive`

## 功能总览

- **两种执行模式**
  - 固定计划：规则模板或 LLM 一次性生成步骤列表，逐步执行
  - 闭环 Agent：每轮重新观察页面 → LLM 决策 → 执行，失败步骤带反馈重试，硬上限 18 步
- **浏览器执行器**：`goto` / `click` / `type` / `wait` / `screenshot` / `extract`，支持多标签页（`new_tab` / `switch_tab` / `close_tab`）
- **任务编排**：FastAPI + RQ 队列；排队任务可撤单，运行中任务步间协作停止；SSE 实时推送进度与轨迹
- **存储可切换**：任务/模板支持文件或 PostgreSQL（SQLAlchemy 2.x + Alembic 自动迁移）；产物支持本地或 MinIO/S3
- **模板系统**：CRUD + `match` 关键词路由 + `{{var}}` 变量渲染 + 独立 run 端点（缺变量返回 422）
- **Webhook**：任务终态回调，支持 HMAC-SHA256 签名与重试
- **多租户**：用户 / API Key / JWT / RBAC（admin / user / read_only）/ 资源 owner 隔离
- **部署形态**：容器化 worker（镜像内置 Chromium）+ 按队列深度自动扩缩的 worker 池
- **防幻觉**：Agent 宣告完成时必须给出页面原文 evidence，代码校验其确实出现且包含数字，否则拒绝并继续循环

## 架构

```mermaid
flowchart LR
    U[CLI / Web Console / curl] --> A[FastAPI<br/>任务 · 模板 · 鉴权 · SSE]
    A --> Q[(Redis / RQ 队列)]
    Q --> W[Worker 池<br/>Playwright · Agent Loop]
    W --> B[Chromium]
    W --> S[(任务库<br/>file / PostgreSQL)]
    W --> R[(产物<br/>runs/ · MinIO / S3)]
    W --> H[Webhook<br/>HMAC-SHA256]
```

## 快速开始（本机运行）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env    # LLM 规划 / Agent 模式需要填 DOUBAO_API_KEY / DOUBAO_MODEL
```

CLI 演示（无需 API Key）：

```powershell
python -m browser_agent --demo                    # 内置 example.com 计划
python -m browser_agent --demo --print-plan       # 只打印解析后的计划
python -m browser_agent --plan plans\example_com.json
python -m browser_agent --task "open https://example.com and screenshot"   # 需 .env
```

产物落在 `runs/<run_id>/`：`plan.json`、`meta.json`、`report.md`、`steps/*.png`。

### API + worker

```powershell
# 终端 0 — Redis（二选一）
docker compose up -d                              # Redis + Postgres + MinIO
.\scripts\start-redis-wsl.ps1                     # 无 Docker 时的 WSL 方案

# 终端 1 — API（在仓库根目录启动，保证 api 包可导入）
python -m uvicorn api.main:app --reload --host 127.0.0.1 --port 8000

# 终端 2 — worker（SimpleWorker，Windows 友好）
python -m worker                                  # 单 worker
python -m worker.autoscale                        # 按队列深度自动扩缩
```

跑一个 demo 任务：

```powershell
$r = Invoke-RestMethod -Method POST http://127.0.0.1:8000/api/v1/tasks `
  -ContentType 'application/json' -Body '{"demo":true}'
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/tasks/$($r.task_id)"
```

本地 Web 控制台：直接用浏览器打开 `web-console/index.html`（单文件，无需构建），支持新建任务、Agent 轨迹时间线与 SSE 实时刷新。

> Windows 提示：`REDIS_URL` 建议写 `127.0.0.1` 而不是 `localhost`，避免解析到 IPv6 `::1` 造成连接挂起。

## API 端点

| Method | Path | 说明 |
| ------ | ---- | ---- |
| GET | `/health` | 存活检查 + Redis ping（匿名） |
| POST | `/api/v1/tasks` | 创建任务；`demo` / `task_nl` / `json_plan` 三选一；`agent:true` + `task_nl` 进入闭环模式 |
| GET | `/api/v1/tasks` | 最近任务列表（limit ≤ 100） |
| GET | `/api/v1/tasks/{task_id}` | 任务状态 + run ids；运行中可含步骤进度 |
| GET | `/api/v1/tasks/{task_id}/steps` | Agent 轨迹（每步 thought / action / result） |
| GET | `/api/v1/tasks/{task_id}/stream` | SSE：`status` / `step` / `done` 事件 |
| POST | `/api/v1/tasks/{task_id}/cancel` | 排队 → 撤销 RQ 作业；运行中 → 步间协作停止 |
| GET | `/api/v1/tasks/{task_id}/report` | 任务完成后的 `report.md` |
| GET | `/api/v1/tasks/{task_id}/screenshots` | 截图列表 |
| GET | `/api/v1/tasks/{task_id}/screenshots/{name}` | 单张 PNG（防路径穿越） |
| GET | `/api/v1/templates` | 计划模板列表 |
| POST | `/api/v1/templates` | 新建模板（`name` / `description` / `match` 关键词 / `steps` / `variables`） |
| GET | `/api/v1/templates/{name}` | 模板详情 |
| PUT | `/api/v1/templates/{name}` | 局部更新模板 |
| DELETE | `/api/v1/templates/{name}` | 删除模板（204） |
| POST | `/api/v1/templates/{name}/run` | 渲染 `{{var}}` 后入队（缺变量 → 422） |
| POST | `/api/v1/auth/login` | 登录换取 JWT（需 Postgres） |

`task_id` 是 API 层的 UUID；浏览器归档仍用 `run_id`（`run-YYYYMMDD-HHMMSS`），一次任务若发生 replan 会累积多个 `run_id`。

## 模板与变量

`match` 是逗号分隔的关键词列表。自然语言任务先过内置规则，未命中时匹配自定义模板（计划 `source=template`）。模板可声明默认变量，步骤中用 `{{var}}` 占位；run 端点做渲染，运行时传值覆盖默认值，缺失变量返回 422。计划 DSL 与校验规则见 [PROJECT.md](PROJECT.md)。

## Webhook 通知

任务进入终态（`succeeded` / `failed` / `cancelled`）后，worker 向配置的 URL 逐一 POST JSON 事件。`WEBHOOK_URL` 支持逗号分隔多个地址，创建任务时传 `webhook_url` 可覆盖全局配置（单任务优先）。

- 请求头：`X-Webhook-Event`（`task.succeeded` / `task.failed` / `task.cancelled`）、`X-Webhook-Timestamp`
- 配置 `WEBHOOK_SECRET` 后附加 `X-Webhook-Signature: sha256=<hex>`（对原始 JSON body 做 HMAC-SHA256）
- 投递失败重试 2 次；最终失败只记录到任务的 `error` 字段，不改变终态

```json
{
  "event": "task.succeeded",
  "task_id": "…",
  "status": "succeeded",
  "source": "demo",
  "run_id": "…",
  "run_ids": ["…"],
  "summary": "…",
  "error": null,
  "created_at": "…",
  "finished_at": "…"
}
```

## 配置

完整清单见 [.env.example](.env.example)。

| 变量 | 默认值 | 用途 |
| ---- | ------ | ---- |
| `DOUBAO_API_KEY` / `DOUBAO_MODEL` | — | LLM 规划与 Agent 决策 |
| `BROWSER_ALLOWED_HOSTS` | 内置示例站点 | 逗号分隔的主机白名单 |
| `BROWSER_ALLOW_ALL_HOSTS` | 关 | `1` 关闭白名单（仅限本地自用） |
| `REDIS_URL` / `RQ_QUEUE` | `redis://127.0.0.1:6379/0` / `browser_tasks` | 队列 |
| `MAX_TASK_SECONDS` | `300` | RQ 作业超时 |
| `RUNS_ROOT` / `DATA_ROOT` | `./runs` / `./data` | 产物与元数据路径 |
| `API_KEYS` | — | Bearer Key 列表；留空 = 关闭鉴权 |
| `TASK_STORE` / `TEMPLATE_STORE` | `file` | `file` 或 `postgres` |
| `DATABASE_URL` | — | Postgres DSN |
| `ARTIFACT_STORE` | `local` | `local` 或 `s3` |
| `S3_ENDPOINT` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` / `S3_BUCKET` | MinIO 本地默认值 | 对象存储产物后端 |
| `WEBHOOK_URL` / `WEBHOOK_SECRET` | — | 终态回调地址 / 签名密钥 |
| `BROWSER_EXECUTABLE` | Playwright 自带 | Chromium 路径（容器内 `/usr/bin/chromium`） |
| `BROWSER_FORCE_HEADLESS` | — | `1` 强制无头（容器环境） |
| `WORKER_MIN` / `WORKER_MAX` | `1` / `4` | 自动扩缩上下限 |
| `SCALE_UP_THRESHOLD` | `1` | 每个 worker 承担的排队任务数 |
| `AUTOSCALE_POLL_SECONDS` | `5` | 扩缩轮询间隔 |
| `SECRET_KEY` / `JWT_EXPIRES_MINUTES` | dev 密钥 / `1440` | JWT 签名密钥与有效期 |

## 多租户与鉴权

配置 `DATABASE_URL` 后启用完整鉴权模型（否则退化为可选的静态 `API_KEYS`）：

- **用户**：`users` 表，PBKDF2 密码哈希，角色 `admin` / `user` / `read_only`
- **登录**：`POST /api/v1/auth/login` 签发 JWT
- **API Key**：可撤销 / 可过期的 `api_keys` 表，绑定用户与角色
- **RBAC**：`read_only` 只读；仅 admin 可管理用户与 Key
- **Owner 隔离**：任务与模板带 `owner_id`，普通用户只见自己的资源；全球模板对其只读

初始化命令（`python -m scripts.auth_cli …`）：

```powershell
$env:DATABASE_URL = "postgresql+psycopg://browser:browser@127.0.0.1:5432/browser_agent"
python -m scripts.auth_cli bootstrap-admin --username admin --password 'change-me'
python -m scripts.auth_cli add-user --username alice --password 'p@ssw0rd' --role user
python -m scripts.auth_cli add-key --username alice --name "alice laptop"   # 明文只打印一次
```

`/health` 与登录接口匿名，其余端点需要 Bearer 凭证。

## 闭环 Agent 模式

```powershell
$body = @{ agent = $true; task_nl = "open https://www.bing.com , search 佛山天气 and tell me the current temperature" } | ConvertTo-Json
$r = Invoke-RestMethod -Method POST http://127.0.0.1:8000/api/v1/tasks -ContentType 'application/json' -Body $body
```

每轮流程：

1. 注入 JS 读取 URL / 标题 / 页面正文（600 字符）/ 最多 20 个可见可交互元素（带 `@N` 引用），自动截取 `NN_obs.png`
2. 将目标 + 历史 + 当前观察发给 LLM，要求返回单个 JSON 决策 `{thought, action, done}`
3. 执行动作；失败以 `ok=false` 回传，LLM 下一轮换策略

可靠性设计：

- 硬上限 18 步；`goto` 仍走主机白名单
- LLM 决策失败（超时 / 坏 JSON）先重试一次（间隔 2s）再判失败；JSON 解析会剥离 code fence、截取最外层对象并去尾逗号
- `done=true` 必须同时返回 `summary`（含数字与单位）和页面原文 `evidence`；evidence 未出现在最新观察文本、不含数字或短于 4 字符时拒绝完成并继续循环（轨迹中记为 `(done rejected)`）
- 观察脚本穿透开放 shadow root，并合并同源 iframe 的元素列表
- 队列作业带 `Retry(max=1, interval=5)`，worker 崩溃或超时后自动重入队

## 容器化 worker 与自动扩缩

```powershell
docker compose build worker     # python:3.11-slim + Debian 系统 Chromium
docker compose up -d worker     # 与宿主 worker 共享同一队列
```

- 镜像使用发行版 Chromium（`BROWSER_EXECUTABLE=/usr/bin/chromium`），不下载 Playwright CDN 浏览器
- `./runs` 与 `./data` 以 bind mount 挂入容器，容器写入的产物/文件任务对宿主 API 立即可见
- 容器内强制 `BROWSER_FORCE_HEADLESS=1`，headed 请求自动降级
- Redis / Postgres 通过 compose 服务名访问

`python -m worker.autoscale` 是自管池：目标规模 `clamp(ceil(depth / SCALE_UP_THRESHOLD), WORKER_MIN, WORKER_MAX)`。扩容立即生效；缩容只停 RQ 状态为 `idle` 的进程，不会打断运行中任务；SIGINT/SIGTERM 优雅退出。

## 数据库迁移与历史数据

Postgres 表结构由 Alembic 管理（`alembic/versions/0001`–`0004`），`PostgresTaskStore` 启动时自动 `upgrade head`；检测到旧 `create_all` 表（无 `alembic_version`）会先 stamp 基线再升级。也可手动执行：

```powershell
alembic upgrade head
alembic downgrade -1
alembic current
```

历史文件任务导入 Postgres（幂等，`task_id` 已存在则跳过）：

```powershell
python -m scripts.migrate_tasks --dry-run    # 预览
python -m scripts.migrate_tasks              # 导入
```

支持 `--overwrite` / `--limit N` / `--from-dir`，单条失败不影响整批。

## 测试

```powershell
python -m pytest tests -q
```

78 个用例，默认全程离线：单元测试不启动 Chromium，也不需要 Redis / Postgres（队列调用被替换，Postgres 相关用例在无库时自动跳过）。真实浏览器冒烟请用 CLI `--demo` 或 POST `{"demo": true}`。

## 项目结构

```text
browser-automation-agent/
├── browser_agent/      # schema、planner、executor、agent_loop、archive、safety、service、CLI
├── api/                # FastAPI：/health + /api/v1/tasks* + /api/v1/templates* + 鉴权
├── worker/             # RQ job、SimpleWorker 入口、autoscale 监管器
├── store/              # TaskStore（file / Postgres）+ ArtifactStore（local / S3）+ TemplateStore
├── alembic/            # 数据库迁移
├── plans/              # 示例计划 JSON
├── scripts/            # run_task / auth_cli / migrate_tasks / start-redis-wsl
├── web-console/        # 单文件本地 Web 控制台
├── docs/               # 部署文档
├── data/               # 运行期元数据（gitignored）
├── runs/               # 运行归档（gitignored）
└── tests/
```

## 文档

- [PROJECT.md](PROJECT.md) — 计划 DSL、校验规则、里程碑与详细设计
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — Nginx + HTTPS + SSE 生产部署
- [.env.example](.env.example) — 全量配置说明

## 后续方向

可视化计划编辑器 · 模板市场 · 监控/SLA 看板。
