# Browser Automation Agent — 项目文档

> 文档日期：2026-09-19 ｜ 文档版本：2.0 ｜ 适用代码：仓库当前 HEAD（P0 + P1 + 闭环 Agent Loop 已落地）
> 一句话定位：
>
> **自然语言 / JSON 计划 → Playwright 浏览器步骤 → 截图 + Markdown 报告归档**
>
> 的浏览器自动化代理。



***

## 目录



1. [项目概述](#1-项目概述)

2. [系统架构](#2-系统架构)

3. [功能特性](#3-功能特性)

4. [快速开始](#4-快速开始)

5. [计划 DSL 规范](#5-计划-dsl-规范)

6. [使用指南](#6-使用指南)

7. [HTTP API 参考](#7-http-api-参考)

8. [任务与运行生命周期](#8-任务与运行生命周期)

9. [配置参考](#9-配置参考)

10. [部署模式](#10-部署模式)

11. [安全模型](#11-安全模型)

12. [测试](#12-测试)

13. [已知问题与注意事项](#13-已知问题与注意事项)

14. [路线图](#14-路线图)



***

## 1. 项目概述

### 1.1 核心循环



```
plan ──▶ execute ──▶ archive

&#x20;计划      执行        归档
```



* **plan**：从内置 demo、自然语言模板、LLM（Doubao/ARK）或 JSON 计划文件解析出**严格校验的步骤列表**。

* **execute**：Playwright（Chromium，默认 headless）逐步执行，fail-fast，支持步骤间协作取消。

* **archive**：每次运行产出 `runs/{run_id}/`，包含 `plan.json`、`meta.json`、`report.md`、`steps/*.png`。

#### 两种执行模式

| 模式 | 触发 | 工作方式 |
| --- | --- | --- |
| 计划模式（默认） | `demo` / `task_nl` / `json_plan` | 一次性出计划 → executor 逐步死跑，失败最多 replan 2 次 |
| **闭环 agent 模式** | `POST /tasks {"agent": true, "task_nl": "<目标>"}` | 目标优先：每轮 observe 页面 → LLM 决策下一步 → 执行 → 结果回灌，直到 LLM 判断 `done` 或 18 步预算耗尽 |

### 1.2 技术栈



| 层      | 技术                                    | 说明                                      |
| ------ | ------------------------------------- | --------------------------------------- |
| 语言     | Python 3.10+（开发 / 实测 3.11）            | `pyproject.toml` requires-python >=3.10 |
| 浏览器    | Playwright（Chromium）                  | 宿主机运行，headless 默认                       |
| API    | FastAPI + Uvicorn                     | 同步路由，`/api/v1/tasks*`                   |
| 队列     | Redis + RQ                            | SimpleWorker（无 fork，Windows 友好）         |
| 任务库    | 文件 JSON（默认）或 Postgres（SQLAlchemy 2.x） | 工厂模式按 `TASK_STORE` 切换                   |
| 产物库    | 本地 `runs/`（默认）或 MinIO/S3（boto3）       | 工厂模式按 `ARTIFACT_STORE` 切换               |
| LLM 规划 | OpenAI SDK（兼容 Doubao/ARK）             | 自然语言任务未命中模板时使用                          |

### 1.3 里程碑状态



| 阶段    | 内容                                                      | 状态                                  |
| ----- | ------------------------------------------------------- | ----------------------------------- |
| P0    | CLI + `run_plan` 服务 + FastAPI + Redis/RQ + 文件 TaskStore | ✅ 已验收                               |
| P1-M1 | API Key 鉴权（`API_KEYS` Bearer）                           | ✅ 已实现并端到端验证（2026-09-08 落地，09-10 复验） |
| P1-M2 | Postgres 任务库（`TASK_STORE=postgres`）                     | ✅ 已实现并端到端验证                         |
| P1-M3 | MinIO/S3 产物存储（`ARTIFACT_STORE=s3`）                      | ✅ 已实现并端到端验证                         |
| P1-M4 | 模板 CRUD（`/api/v1/templates` + planner 关键词路由）           | ✅ 已实现并端到端验证（2026-09-15 落地，36 项测试通过） |
| P1-M5 | Webhook 通知（终态回调 + HMAC-SHA256 签名，全局/任务级 URL） | ✅ 已实现并端到端验证（2026-09-15 落地，46 项测试通过） |
| P1-M6 | Alembic 迁移（Postgres 表结构版本化，启动自动升级）          | ✅ 已实现并端到端验证（2026-09-15 落地，48 项测试通过） |
| P1-M7 | 多租户 / 权限（用户体系、API Key 表、JWT/RBAC、owner 隔离） | ✅ 已实现并端到端验证（2026-09-15 落地，61 项测试通过） |
| P1-M8 | 浏览器容器化（worker + Chromium 进 Docker，系统 Chromium 方案）  | ✅ 已实现并端到端验证（2026-09-15 落地，容器内执行 7 步 demo 任务） |
| P1-M9 | worker 自动扩缩（按队列深度动态增减 worker，仅缩空闲进程）    | ✅ 已实现并端到端验证（2026-09-15 落地，67 项测试通过，真实队列扩 2→并行→缩回） |
| P1-M10 | 历史数据迁移（`data/tasks/*.json` → Postgres，幂等可重入）   | ✅ 已实现并端到端验证（2026-09-15 落地，真实库导入 11 条，重复运行 skip 幂等） |
| P1-M11 | 模板增强（Postgres 模板库 + 变量替换 + 多用户共享）         | ✅ 已实现并端到端验证（2026-09-15 落地，真实库 postgres 模板 → 变量渲染 → worker 执行 succeeded） |
| P1-M12 | 闭环 Agent Loop（observe→LLM 决策→act 循环，目标驱动）    | ✅ 已实现并端到端验证（2026-09-19 落地，真实 agent 任务 6 步完成必应搜天气并自主 done，78 项测试通过） |



***

## 2. 系统架构

### 2.1 架构总览



```mermaid
flowchart TB
    subgraph 入口层
        CLI[CLI<br/>python -m browser_agent]
        API[FastAPI<br/>uvicorn api.main:app]
        WK[RQ Worker<br/>python -m worker]
    end

    subgraph 核心层
        SVC[run_plan 服务<br/>browser_agent/service.py]
        PL[Planner<br/>demo / NL模板 / LLM / replan]
        EX[Executor<br/>Playwright 逐步执行]
        AGENT[Agent Loop<br/>observe / LLM / act closed loop]
        SF[Safety<br/>host 白名单 / 超时 / headless]
        AR[Archive<br/>runs/{run_id} 归档]
    end

    subgraph 基础设施层
        TS[(TaskStore<br/>file JSON ｜ Postgres)]
        AS[(ArtifactStore<br/>local runs ｜ MinIO/S3)]
        RQ[(Redis / RQ 队列)]
        CH[Playwright Chromium<br/>宿主机 headless]
        LLM[Doubao / ARK LLM]
    end

    CLI --> SVC
    API --> SVC
    WK --> SVC
    SVC --> PL
    SVC --> EX
    SVC --> AGENT
    SVC --> SF
    SVC --> AR
    PL -.可选.-> LLM
    EX --> CH
    AR --> AS
    WK --> RQ
    API --> RQ
    API --> TS
    WK --> TS
```

### 2.2 数据流（一次 API 任务）



1. `POST /api/v1/tasks` → 任务记录落库（`pending` → `queued`），RQ 入队。

2. RQ Worker 取出任务 → 状态置 `running`，调用 `run_plan()`。

3. `run_plan` 解析计划来源（demo /task\_nl/json\_plan），`SafetyConfig` 校验白名单与步数。

4. Playwright 按步骤执行（goto/click/type/wait/screenshot/extract），每步结果增量写入 `meta.json`。

5. 结束：归档 `report.md`；任务状态置终态；产物 `upload_run()` 同步到配置的 ArtifactStore。

6. API 侧通过 `get_artifact_store()` 读报告与截图（本地直读 / S3 服务端流式代理）。

### 2.3 目录结构



```
browser-automation-agent/

├── browser\_agent/          # 核心包

│   ├── schema.py           # 计划 DSL：Step/Plan 定义 + 校验

│   ├── planner.py          # demo 计划 / NL 模板映射 / Doubao LLM 规划

│   ├── executor.py         # Playwright 逐步执行（fail-fast + 协作取消）

│   ├── agent_loop.py      # closed-loop agent: observe / LLM decide / act

│   ├── actions.py          # 原子动作：goto/click/type/wait/screenshot/extract

│   ├── safety.py           # 安全护栏：host 白名单、action 白名单、超时

│   ├── archive.py          # 归档：plan.json / meta.json / report.md / steps/\*.png

│   ├── service.py          # run\_plan 共享服务（CLI 与 worker 共用）

│   └── cli.py              # 命令行入口

├── api/                    # FastAPI

│   ├── main.py             # 应用入口（/health）

│   ├── routes\_tasks.py     # /api/v1/tasks\* 路由

│   ├── auth.py             # Bearer API Key 鉴权

│   └── schemas.py          # Pydantic 请求/响应模型

├── worker/                 # RQ

│   ├── jobs.py             # execute\_task（RQ 入口）

│   ├── queue.py            # enqueue / cancel / ping

│   └── \_\_main\_\_.py         # SimpleWorker 进程

├── store/                  # 存储层（工厂 + 双后端）

│   ├── task\_store.py       # 文件 TaskStore + TaskRecord

│   ├── postgres\_task\_store.py  # Postgres 任务库（SQLAlchemy 2.x）

│   ├── artifact\_store.py   # LocalArtifactStore + S3ArtifactStore

│   └── factory.py          # get\_task\_store() / get\_artifact\_store() 单例工厂

├── plans/                  # JSON 计划文件示例（example\_com.json）

├── scripts/                # start-redis-wsl.ps1（无 Docker 时 WSL Redis 脚本）

├── tests/                  # pytest（78 项通过）

├── data/tasks/             # 文件任务库（gitignored）

├── runs/                   # 运行归档（gitignored）

├── settings.py             # 配置加载（.env）

├── docker-compose.yml      # Redis + Postgres + MinIO

├── .env.example            # 配置模板

├── PROJECT.md              # 本文档

└── README.md               # 快速上手 README
```



***

## 3. 功能特性

### 3.1 已实现



| 模块       | 能力                                                                                                                             | 关键文件                            |
| -------- | ------------------------------------------------------------------------------------------------------------------------------ | ------------------------------- |
| 计划 DSL   | 6 种动作 + extract 4 字段 + 严格校验（≤40 步、http (s) URL、选择器 / 文本必填）                                                                     | `browser_agent/schema.py`       |
| 规划器      | 内置 demo 计划、自然语言模板映射（open / 访问、httpbin、[example.com](https://example.com)）、Doubao/ARK LLM 生成 JSON 计划                            | `browser_agent/planner.py`      |
| 失败重规划    | 任务失败后由 LLM 重生成更安全计划，最多 2 次（`--max-replans` / API `max_replans`）                                                                | `browser_agent/service.py`      |
| 执行器      | Playwright 逐步执行、fail-fast、步骤间协作取消、导航 / 动作超时                                                                                    | `browser_agent/executor.py`     |
| 闭环 Agent  | 目标驱动 observe→LLM 决策→act：每轮注入 JS 观察 DOM（URL/标题/正文 600 字/20 个可交互元素）+ 自动截图，LLM 回 JSON 决策动作，执行结果回灌；动作失败不致死，LLM 下一轮自主换策略；硬上限 18 步；goto 仍过 host 白名单；LLM 决策失败自动重试一次再整体 failed | `browser_agent/agent_loop.py`  |
| Agent 轨迹 API + 时间线 UI | `GET /api/v1/tasks/{id}/steps` 返回 meta.json 里逐步轨迹（mode/goal/steps[]，运行中可读）；网页控制台详情页新增"Agent 轨迹"卡片，每步 thought/action/结果并排，绿/红边区分成败；新建任务 tabs 加"Agent 闭环"模式 | `api/routes_tasks.py`、`web-console/index.html` |
| 实时日志流（SSE） | `GET /api/v1/tasks/{id}/stream` 用 Server-Sent Events 推 status / step / done 事件；网页控制台用 EventSource 接收，新步骤即时追加到时间线，不再 2.5 秒轮询；SSE 失败自动回退轮询 | `api/routes_tasks.py`、`web-console/index.html` |
| Worker 崩溃自动恢复 | 入队时带 `Retry(max=1, interval=5)`：worker 进程崩溃 / RQ job_timeout 后 5 秒自动重试一次；worker 容器 `restart: unless-stopped`，autoscale 按队列深度补起新 worker | `worker/queue.py`、`docker-compose.yml` |
| 多标签页支持 | agent action 新增 `new_tab{url?}` / `switch_tab{tab_index}` / `close_tab{tab_index}`；goto 加 `new_tab: true` 可在新标签打开；prompt 告知 LLM 可多 tab 操作 | `browser_agent/schema.py`、`browser_agent/agent_loop.py` |
| 安全护栏     | host 白名单（`BROWSER_ALLOWED_HOSTS`）、action 白名单、步数上限、headless 默认、`--allow-all-hosts` 逃生门                                          | `browser_agent/safety.py`       |
| 归档       | `plan.json` + `meta.json`（增量写入）+ `report.md` + `steps/*.png`                                                                   | `browser_agent/archive.py`      |
| CLI      | `--demo` / `--task` / `--plan` / `--print-plan` / `--headed` / `--output` / `--run-id` / `--max-replans` / `--allow-all-hosts` | `browser_agent/cli.py`          |
| HTTP API | `/health`、任务创建 / 列表 / 详情 / 取消、报告与截图读取（防路径穿越）                                                                                   | `api/`                          |
| 队列       | RQ enqueue/cancel、`job_id=task_id`、超时控制、SimpleWorker 无 fork                                                                    | `worker/`                       |
| 任务库      | file JSON（默认）与 Postgres（SQLAlchemy 2.x）双后端，工厂单例切换                                                                              | `store/`                        |
| 产物库      | 本地 `runs/`（默认）与 MinIO/S3（boto3）双后端，服务端流式代理                                                                                     | `store/artifact_store.py`       |
| 鉴权       | `API_KEYS` 逗号分隔 Bearer，空 = 关闭，`/health` 匿名，缺头 / 错 key → 401                                                                    | `api/auth.py`                   |
| 模板 CRUD   | 模板库持久化（`data/templates/*.json`）、CRUD API、planner 关键词路由（自定义模板命中，`source=template`）          | `store/template_store.py`、`api/routes_templates.py` |
| Webhook     | 任务终态事件回调（succeeded/failed/cancelled）、HMAC-SHA256 签名头、全局 `WEBHOOK_URL` + 任务级 `webhook_url`、失败重试并记入任务 error | `worker/webhook.py` |
| Alembic     | Postgres 表结构版本化管理：`alembic.ini` + `alembic/env.py` + 迁移链（0001 初始表、0002 webhook_url）；`PostgresTaskStore` 启动时程序化 `upgrade head`，旧库自动 stamp 基线后升级，幂等 | `alembic/`、`store/postgres_task_store.py` |
| 多租户/权限 | 用户体系（pbkdf2 密码）+ API Key 表（可吊销/过期）+ JWT 登录（HS256）+ RBAC（admin/user/read_only）；任务/模板按 owner 隔离（admin 与静态 key 见全部，普通用户仅本人；全局模板对非 owner 只读） | `api/security.py`、`store/auth_store.py`、`api/routes_auth.py`、`scripts/auth_cli.py` |
| 容器化部署 | `Dockerfile.worker` 构建 worker 镜像（python:3.11-slim + Debian 系统 Chromium，USTC 镜像加速），`docker-compose` 增加 worker 服务：容器内执行 Playwright，runs/data 双向绑定到宿主路径，Redis/Postgres 走 compose 服务名；容器内强制 headless（`BROWSER_FORCE_HEADLESS`） | `Dockerfile.worker`、`docker-compose.yml`、`browser_agent/executor.py` |
| 自动扩缩 | `python -m worker.autoscale` 按队列深度维护 worker 子进程池：目标 = clamp(ceil(深度/SCALE_UP_THRESHOLD), WORKER_MIN, WORKER_MAX)；缩容仅对 RQ 状态为 idle 的进程（读 Redis `rq:worker:{host}.{pid}`），Windows/容器安全、不打断运行中任务；SIGINT/SIGTERM 优雅停机 | `worker/autoscale.py` |
| 历史数据迁移 | `python -m scripts.migrate_tasks` 把文件库 `data/tasks/*.json` 导入 Postgres：按 created_at 升序、默认跳过已存在（幂等）、`--overwrite` 覆盖、`--dry-run` 预览、`--limit` 限量，失败单条记录不中断 | `scripts/migrate_tasks.py` |
| 模板增强 | 模板库可切 Postgres（`TEMPLATE_STORE=postgres`，alembic 0004 `templates` 表）；模板支持 `{{var}}` 占位符 + 变量默认值，`POST /api/v1/templates/{name}/run` 渲染后直接入队执行（缺变量 422）；全局/私有模板 owner 隔离（admin 全见，非 owner 只读全局） | `store/postgres_template_store.py`、`store/template_render.py`、`api/routes_templates.py`、`alembic/versions/0004_add_templates.py` |
| 部署       | docker-compose 一键起 Redis + Postgres + MinIO + worker（容器内 Chromium + 自动扩缩）；无 Docker 时 WSL Redis 脚本 + 宿主 worker（可 `python -m worker.autoscale`） | `docker-compose.yml`、`Dockerfile.worker`、`worker/autoscale.py` |

### 3.2 未实现 / 规划中



| 项                | 说明                                               |
| ---------------- | ------------------------------------------------ |
6. **历史数据迁移脚本**：`data/tasks/*.json` 导入 Postgres。



***

## 4. 快速开始

### 4.1 环境要求



* Python 3.10+（推荐 3.11）

* 可选：Docker Desktop（P1 部署）/ WSL（Redis 后备）

* 可选：Doubao/ARK API Key（LLM 规划）

### 4.2 安装



```
cd f:\kon\browser-automation-agent

python -m venv .venv

.\\.venv\Scripts\Activate.ps1

python -m pip install -r requirements.txt

python -m playwright install chromium

copy .env.example .env   # 需要 LLM 时填写 DOUBAO\_API\_KEY / DOUBAO\_MODEL
```

> ⚠️ 注意：P1 后端（Postgres/MinIO）依赖 
>
> `sqlalchemy`
>
> 、
>
> `psycopg[binary]`
>
> 、
>
> `boto3`
>
> ，
>
> **尚未写入&#x20;**
>
> `requirements.txt`
>
> ，见
>
> [已知问题](#13-已知问题与注意事项)
>
> 。

### 4.3 首次运行（demo，无需任何 Key）



```
python -m browser\_agent --demo          # 打开 example.com 并截图归档

python -m browser\_agent --demo --print-plan   # 只看解析出的计划
```

产物落在 `runs/run-YYYYMMDD-HHMMSS/`：



```
run-20260910-153000/

├── plan.json

├── meta.json

├── report.md

└── steps/

&#x20;   ├── 03\_home.png

&#x20;   └── 07\_home-full.png
```

### 4.4 冒烟验证



```
python -m pytest tests -q          # 78 passed（不启动 Chromium）

python -m browser\_agent --demo     # 真实浏览器冒烟
```



***

### 4.5 本地一键跑任务 CLI（`scripts/run_task.py`）

本地起好 API + worker 后，无需手算 curl：

```
python -m scripts.run_task demo                  # 内置示例
python -m scripts.run_task "打开 https://www.bing.com 提取标题"   # 自然语言
python -m scripts.run_task --file plans\bing-check.json         # 计划文件（推荐，避免 shell 引号问题）
```

自动轮询到终态，打印状态、摘要、报告路径与截图路径。可选 `--api`（默认 `http://127.0.0.1:8001`）、`--headed`、`--max-replans N`、`--timeout`。

### 4.6 本地网页控制台（`web-console/index.html`）

单文件原生 HTML/CSS/JS，浏览器双击打开即用，直连本地 API，无登录（本地自用）。功能：任务列表/新建/详情/取消、报告 Markdown 渲染、截图网格（点图放大）、模板列表与「运行」变量表单。

前提：API 已加 CORS（`api/main.py` 的 `CORSMiddleware`，本地允许任意来源），启动 API：

```
python -m uvicorn api.main:app --host 127.0.0.1 --port 8001
```

再用浏览器打开 `web-console/index.html`，右上角「API」可切换后端地址。它只连本地 API，与云端托管控制台数据独立。


***

## 5. 计划 DSL 规范

### 5.1 动作表



| action       | 必填字段                      | 可选字段                                             | 说明                               |
| ------------ | ------------------------- | ------------------------------------------------ | -------------------------------- |
| `goto`       | `url`（http/https 且带 host） | —                                                | 导航到页面，等待 `domcontentloaded`      |
| `click`      | `selector`（CSS）           | —                                                | 点击首个匹配元素                         |
| `type`       | `selector`、`text`         | —                                                | 向首个匹配元素填入文本                      |
| `wait`       | —                         | `ms`（0–60000，默认 1000）                            | 固定等待                             |
| `screenshot` | —                         | `name`、`full_page`                               | 截图，文件名为 `{index:02d}_{name}.png` |
| `extract`    | —                         | `field`（title/url/text/html，默认 title）、`selector` | 提取页面数据                           |

通用字段：`note`（说明文字，不进 JSON 输出）。

### 5.2 校验规则（`schema.py`）



* `plan.steps` 必须是非空列表，且 **≤ 40 步**。

* `goto.url` 必须是带 host 的 http/https URL。

* `click`/`type` 必须提供非空 `selector`；`type` 必须提供字符串 `text`。

* `wait.ms` 必须为 0–60000 的整数。

* `screenshot.name` 会被清洗为仅字母数字与 `-`/`_`。

* `extract.field` 只能是 `title` / `url` / `text` / `html`。

* `extract` 的 `text` 与 `html` 有长度截断（text 8000 字符、html 20000 字符）。

### 5.3 示例（`plans/example_com.json`）



```
{

&#x20; "name": "example-com-basic",

&#x20; "description": "Open example.com, screenshot, extract title and h1",

&#x20; "steps": \[

&#x20;   {"action": "goto", "url": "https://example.com/"},

&#x20;   {"action": "wait", "ms": 500},

&#x20;   {"action": "screenshot", "name": "home"},

&#x20;   {"action": "extract", "field": "title"},

&#x20;   {"action": "extract", "field": "text", "selector": "h1"},

&#x20;   {"action": "screenshot", "name": "home-full", "full\_page": true}

&#x20; ]

}
```



***

## 6. 使用指南

### 6.1 CLI



```
\# 内置 example.com 演示（无需 API Key）

python -m browser\_agent --demo

\# 仅打印计划不执行

python -m browser\_agent --demo --print-plan

\# JSON 计划文件

python -m browser\_agent --plan plans\example\_com.json

\# 自然语言（模板优先；未命中且有 DOUBAO\_\* 配置则走 LLM）

python -m browser\_agent --task "open https://example.com and screenshot"

\# 有头模式 + 失败重规划 2 次

python -m browser\_agent --task "..." --headed --max-replans 2

\# 关闭 host 白名单（慎用）

python -m browser\_agent --demo --allow-all-hosts
```

退出码：`0` 成功；`1` 失败 / 取消；`2` 参数或计划校验错误。

### 6.2 自然语言任务与 LLM 规划

规划优先级：`--demo` → **模板映射** → **LLM**（配置了 Doubao/ARK）→ 报错。

内置模板：



| 输入                                                          | 命中 | 生成计划                                                         |
| ----------------------------------------------------------- | -- | ------------------------------------------------------------ |
| `demo` / `demo task` / `run demo` / 含 "示例"                  | ✅  | demo 计划                                                      |
| `open https://xxx` / `goto` / `visit` / `打开/访问 https://xxx` | ✅  | goto + wait + screenshot + extract(title/url)                |
| `example.com` + (open/goto/visit/ 打开 / 访问 / 截图)             | ✅  | demo 计划                                                      |
| 含 `httpbin`                                                 | ✅  | httpbin-html 计划                                              |
| 其他且未配置 LLM                                                  | ❌  | `PlanValidationError`（提示配置 DOUBAO\_API\_KEY + DOUBAO\_MODEL） |

LLM 配置（任一来源即可）：



```
DOUBAO\_API\_KEY / ARK\_API\_KEY / VOLCENGINE\_API\_KEY

DOUBAO\_MODEL  / ARK\_MODEL  / DOUBAO\_ENDPOINT\_ID

DOUBAO\_BASE\_URL / ARK\_BASE\_URL   # 默认 https://ark.cn-beijing.volces.com/api/v3

DOUBAO\_TIMEOUT / ARK\_TIMEOUT     # 默认 120s
```

LLM 输出被约束为单个 JSON 计划对象，经 `parse_plan` 严格校验后再执行。

### 6.3 失败重规划



* 通过 `--max-replans N`（0–2）或 API `max_replans` 开启。

* 执行失败后，以「原任务 + 失败摘要 + 请改用白名单内公共站点」重新请求 LLM 生成新计划。

* 每次重试使用独立 run 目录：`{run_id}-replan{n}`。

### 6.4 归档产物



| 文件            | 内容                                     |
| ------------- | -------------------------------------- |
| `plan.json`   | 实际执行的计划（含 source：manual/demo/llm/file） |
| `meta.json`   | 运行元数据 + 逐步结果（增量写入，可观察进度）               |
| `report.md`   | 人类可读报告：计划、每步状态、提取值、截图引用                |
| `steps/*.png` | 步骤截图（`{index:02d}_{name}.png`）         |



***

### 6.5 闭环 Agent 模式（agent=true）

和计划模式的根本区别：**不预先出步骤列表，每一步都重新看页面**。

```
POST /api/v1/tasks
{
  "agent": true,
  "task_nl": "打开 https://www.bing.com ，搜索 佛山天气 并告诉我当前温度"
}
```

每轮执行：

1. **观察**：抽当前 URL、标题、body.innerText 前 600 字、最多 20 个可见可交互元素（tag/id/name/placeholder/text/selector）。
2. **留痕**：自动截一张 NN_obs.png。
3. **决策**：把目标 + 最近 10 步历史 + 当前观察拼成 prompt，调 Doubao/ARK，要求只回一个 JSON：{thought, action, done}。
4. **执行**：动作经 validate_step 校验；goto 仍过 host 白名单；用现有 run_step 执行。
5. **回灌**：结果（ok/error/message）追加进 history，LLM 下一轮能看到。

关键行为：

- **失败不致死**：单步异常被 catch 成 ActionResult(ok=False) 回灌，LLM 下一轮自主换路子（真实跑过 click 搜索按钮失败后改按回车、extract 具体选择器失败后改 extract 全文）。
- **自主结束**：LLM 判断目标达成时回 done:true 并附 summary。
- **硬上限**：MAX_STEP_BUDGET = 18 步。
- **安全复用**：动作执行、URL 白名单、归档全部复用现有件。
- **模型影响**：慢模型单步 60-90 秒，18 步可能跑不完；快模型单步 5-10 秒，真实任务 6 步即可 done（2026-09-19 必应搜天气用例）。

前置条件：DOUBAO_API_KEY + DOUBAO_MODEL（worker 通过 env_file: .env 注入）；建议 .env 配 DOUBAO_TIMEOUT=180、MAX_TASK_SECONDS=900。&#x0A;&#x0A;LLM 决策失败（超时 / JSON 解析错）时等 2 秒自动重试一次，第二次才整体 failed；单步浏览器动作失败不重试、直接回灌 history。&#x0A;&#x0A;观察脚本已支持 shadow DOM（TreeWalker 递归 open shadow root）与同源 iframe（遍历 page.frames[1:] 合并元素列表，总数仍 ≤20）。&#x0A;&#x0A;LLM JSON 解析兜底：自动剥离 ```json fence、截取最外层 {...}、清理尾逗号；裸 JSON / markdown 包裹 / 前后多余文字 / 尾逗号四种常见输出都能解析，解析失败才走 LLM 重试。&#x0A;&#x0A;Evidence grounding（防瞎编数字）：done 时 prompt 要求 LLM 同时给 summary（带数字单位，如"佛山当前温度 34°C"）和 evidence（从页面原文抄的短语，如"34°C Mostly sunny"）；代码做三层校验——evidence 归一化后必须能在最近一次观察到的页面文本里子串命中、必须含数字、长度 ≥4；任一不过则把 done 打回、回灌 history 让 LLM 重看，记 (done rejected) 轨迹。参考 browser-use 官方 prompting guide 的 few-shot 示例做法。

***

## 7. HTTP API 参考

### 7.1 鉴权



* `API_KEYS` 未设置或为空 → **鉴权关闭**（本地演示）。

* `API_KEYS=k1,k2` → `/api/v1/*` 必须携带 `Authorization: Bearer <key>`，key ∈ 集合；`/health` 始终匿名。

* 缺头 / 错 key → `401`。

### 7.2 端点



| 方法   | 路径                                           | 说明                                         |
| ---- | -------------------------------------------- | ------------------------------------------ |
| GET  | `/health`                                    | 存活探针 + Redis ping（`status: ok/degraded`）   |
| POST | `/api/v1/tasks`                              | 创建任务：`demo` / `task_nl` / `json_plan` 恰好一个；`agent=true` 走闭环模式（必须带 `task_nl` 作为目标） |
| GET  | `/api/v1/tasks`                              | 最近任务列表（`limit` 1–100，默认 50，按创建时间倒序）        |
| GET  | `/api/v1/tasks/{task_id}`                    | 任务详情（状态、run\_ids、逐步进度）                     |
| POST | `/api/v1/tasks/{task_id}/cancel`             | 取消：queued → 撤单；running → 步骤间协作停止           |
| GET  | `/api/v1/tasks/{task_id}/report`             | 完成后返回 `report.md`                          |
| GET  | `/api/v1/tasks/{task_id}/steps`             | 返回 meta.json 逐步轨迹（agent 时间线；运行中可读） |
| GET  | `/api/v1/tasks/{task_id}/stream`            | SSE 实时流：status / step / done 事件，任务运行中每步推送 |
| GET  | `/api/v1/tasks/{task_id}/screenshots`        | 截图列表                                       |
| GET  | `/api/v1/tasks/{task_id}/screenshots/{name}` | 单张截图 PNG（防路径穿越）                            |
| GET  | `/api/v1/templates`                          | 模板列表（name/description/match/step_count）      |
| POST | `/api/v1/templates`                          | 创建模板（steps 经计划 DSL 校验）                     |
| GET  | `/api/v1/templates/{name}`                   | 模板详情（含 steps）                               |
| PUT  | `/api/v1/templates/{name}`                   | 部分更新模板（description/match/steps）            |
| DELETE | `/api/v1/templates/{name}`                 | 删除模板（204）                                   |

### 7.3 请求示例



```
\# 健康检查

Invoke-RestMethod http://127.0.0.1:8000/health

\# 创建 demo 任务（无鉴权模式）

\$r = Invoke-RestMethod -Method POST http://127.0.0.1:8000/api/v1/tasks \`

&#x20; -ContentType 'application/json' -Body '{"demo":true}'

\# 创建自然语言任务（鉴权模式）

\$h = @{ Authorization = "Bearer dev-secret" }

\$r = Invoke-RestMethod -Method POST http://127.0.0.1:8000/api/v1/tasks \`

&#x20; -Headers \$h -ContentType 'application/json' \`

&#x20; -Body '{"task\_nl":"open https://example.com and screenshot"}'

\# 查询 / 取消

Invoke-RestMethod "http://127.0.0.1:8000/api/v1/tasks/\$(\$r.task\_id)"

Invoke-RestMethod -Method POST "http://127.0.0.1:8000/api/v1/tasks/\$(\$r.task\_id)/cancel"

\# 产物（报告 / 截图）

Invoke-RestMethod "http://127.0.0.1:8000/api/v1/tasks/\$(\$r.task\_id)/report"

Invoke-RestMethod "http://127.0.0.1:8000/api/v1/tasks/\$(\$r.task\_id)/screenshots"
```

### 7.4 创建任务请求体



```
{

&#x20; "demo": true,

&#x20; "task\_nl": null,

&#x20; "json\_plan": null,

&#x20; "agent": false,&#x0A;&#x20; "headed": false,

&#x20; "max\_replans": 0

}
```

约束：`demo` / `task_nl` / `json_plan` 必须**恰好一个**非空；`max_replans` 0–2。`agent=true` 时必须带 `task_nl`（作为 agent 目标），走闭环 observe-decide-act 循环，忽略 `max_replans`。



***

## 8. 任务与运行生命周期

### 8.1 任务状态机



```mermaid
stateDiagram-v2
    [*] --> pending: POST /tasks
    pending --> queued: RQ 入队成功
    pending --> failed: 入队失败（503）
    queued --> running: worker 取出
    queued --> cancelled: cancel（未启动撤单）
    running --> succeeded: 全部步骤成功
    running --> failed: 步骤失败 / 异常
    running --> cancelled: cancel_requested，步骤间停止
    succeeded --> [*]
    failed --> [*]
    cancelled --> [*]
```



* **终态**：`succeeded` / `failed` / `cancelled`。

* `task_id` 是 API 生成的 UUID；`run_id` 是归档目录名（`run-YYYYMMDD-HHMMSS`）。一次任务若发生 replan 会积累多个 `run_ids`。

### 8.2 取消语义



* **queued**：取消 RQ job（`job.cancel()`），状态置 `cancelled`，摘要 "cancelled before start"。

* **running**：置 `cancel_requested=true`，worker 在**每个步骤执行前**检查并停止，摘要 "cancelled"。

* **终态**：幂等返回，不重复处理。



***

## 9. 配置参考

所有配置经 `settings.py` 从环境变量 / `.env` 加载。完整变量表：



| 变量                                                      | 默认值                                        | 用途                                            |
| ------------------------------------------------------- | ------------------------------------------ | --------------------------------------------- |
| `DOUBAO_API_KEY` / `ARK_API_KEY` / `VOLCENGINE_API_KEY` | —                                          | LLM 规划 API Key                                |
| `DOUBAO_MODEL` / `ARK_MODEL` / `DOUBAO_ENDPOINT_ID`     | —                                          | LLM 模型 / 接入点 ID                               |
| `DOUBAO_BASE_URL` / `ARK_BASE_URL`                      | `https://ark.cn-beijing.volces.com/api/v3` | LLM 网关                                        |
| `DOUBAO_TIMEOUT` / `ARK_TIMEOUT`                        | `120`                                      | LLM 请求超时（秒）                                   |
| `BROWSER_ALLOWED_HOSTS`                                 | 内置公共演示站点                                   | 逗号分隔 host 白名单                                 |
| `BROWSER_ALLOW_ALL_HOSTS`                               | 空（=0）                                     | `1`/`true`/`yes` 时关闭 host 白名单，放行所有站点（本地自用；worker/API 链路可用的逃生门） |
| `BROWSER_EXECUTABLE`                                     | 空（Playwright 自带浏览器）                      | 指定 Chromium 可执行文件（容器内 = `/usr/bin/chromium`） |
| `BROWSER_FORCE_HEADLESS`                                  | 空                                          | `1` 时强制 headless（容器内无显示服务器）              |
| `REDIS_URL`                                             | `redis://127.0.0.1:6379/0`                 | RQ 队列（Windows 上建议 `127.0.0.1` 而非 `localhost`） |
| `RQ_QUEUE`                                              | `browser_tasks`                            | 队列名                                           |
| `MAX_TASK_SECONDS`                                      | `300`（最小 30）                               | RQ job 超时                                     |
| `MAX_BROWSER_WORKERS`                                   | `1`（最小 1）                                  | 单 worker 内浏览器并发上限                             |
| `WORKER_MIN`                                             | `1`                                           | 自动扩缩池下限                                   |
| `WORKER_MAX`                                             | `4`                                           | 自动扩缩池上限                                   |
| `SCALE_UP_THRESHOLD`                                      | `1`                                           | 每个 worker 对应的队列任务数（深度 / 该值 = 目标数）        |
| `AUTOSCALE_POLL_SECONDS`                                  | `5`                                           | 扩缩轮询间隔（秒）                              |
| `RUNS_ROOT` / `DATA_ROOT`                               | `./runs` / `./data`                        | 归档 / 数据路径                                     |
| `API_HOST` / `API_PORT`                                 | `127.0.0.1` / `8000`                       | API 监听地址                                      |
| `API_KEYS`                                              | 空                                          | 逗号分隔 Bearer 密钥；空 = 关闭鉴权                       |
| `TASK_STORE`                                            | `file`                                     | `file` 或 `postgres`                           |
| `TEMPLATE_STORE`                                        | `file`                                     | `file` 或 `postgres`（需 `DATABASE_URL`）      |
| `DATABASE_URL`                                          | 空                                          | Postgres 连接串（`postgresql+psycopg://...`）      |
| `ARTIFACT_STORE`                                        | `local`                                    | `local` 或 `s3`                                |
| `S3_ENDPOINT`                                           | `http://127.0.0.1:9000`                    | MinIO/S3 端点                                   |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY`                       | `minioadmin` / `minioadmin`                | S3 凭据                                         |
| `S3_BUCKET`                                             | `browser-runs`                             | 产物桶                                           |
| `S3_REGION`                                             | `us-east-1`                                | 区域                                            |
| `WEBHOOK_URL`                                            | 空                                          | 逗号分隔终态回调 URL；任务级 `webhook_url` 可覆盖        |
| `WEBHOOK_SECRET`                                          | 空                                          | HMAC-SHA256 签名密钥（配置后发 `X-Webhook-Signature`） |
| `SECRET_KEY`                                              | `browser-agent-dev-secret-change-me`       | JWT 签名密钥（生产必改为 32+ 字节随机串）              |
| `JWT_EXPIRES_MINUTES`                                     | `1440`                                      | 登录令牌有效期（分钟）                               |



***

## 10. 部署模式

### 10.1 P0 本地模式（默认）

不设置 `API_KEYS`、`TASK_STORE`、`ARTIFACT_STORE`：



* 任务元数据在 `data/tasks/{id}.json`；产物在 `runs/`。

* 仅需 Redis（Docker `docker compose up -d redis` 或 WSL 脚本）。

* API 开放无鉴权，仅本机监听。



```
docker compose up -d redis        # 或 .\scripts\start-redis-wsl.ps1

python -m uvicorn api.main:app --host 127.0.0.1 --port 8000

python -m worker
```

### 10.2 P1 生产模式

`docker-compose.yml` 提供 Redis + Postgres + MinIO 三服务，API/worker 仍在宿主机（Playwright 需要）：



```
docker compose up -d    # redis + postgres(healthy) + minio
```

`.env` 启用（示例）：



```
API\_KEYS=dev-secret

TASK\_STORE=postgres

DATABASE\_URL=postgresql+psycopg://browser:browser@127.0.0.1:5432/browser\_agent

ARTIFACT\_STORE=s3

S3\_ENDPOINT=http://127.0.0.1:9000

S3\_ACCESS\_KEY=minioadmin

S3\_SECRET\_KEY=minioadmin

S3\_BUCKET=browser-runs
```

行为差异：



* 新任务元数据只进 Postgres（文件库只读遗留）。

* worker 在任务结束后将 `runs/{run_id}/` 整体上传至 MinIO `s3://browser-runs/runs/{run_id}/`。

* API 的 report/screenshots 经 `S3ArtifactStore` 服务端流式代理读取（密钥不进浏览器）。

* **产物同步失败不降级**：任务保持终态，错误记录在任务的 `error` 字段。

### 10.3 Windows / WSL 注意事项（Redis）

已知问题：WSL2 在无交互终端时约 20 秒后自动关闭 Ubuntu 虚拟机，导致 `redis-server` 被杀，worker 出现 `Connection closed → 10053 → 10061` 级联日志（**这是同一个故障**，不是三个 bug）。

修复（一次性）：



```
.\scripts\start-redis-wsl.ps1

\# 写入 %USERPROFILE%\\.wslconfig 的 vmIdleTimeout=-1 并重启 WSL
```

其他要点：



1. `REDIS_URL` 优先 `127.0.0.1` 而非 `localhost`（IPv6 `::1` 可能挂起）。

2. Redis 需 `daemonize no` + `protected-mode no` + `bind 0.0.0.0`（脚本强制）。

3. 使用 Docker Redis 可完全规避此类问题。



***

## 11. 安全模型



| 防线         | 机制                                                                                           |
| ---------- | -------------------------------------------------------------------------------------------- |
| Host 白名单   | `BROWSER_ALLOWED_HOSTS` 逗号分隔；精确 host 或父子域匹配（`example.com` 可放行 `www.example.com`）；默认仅内置公共演示站点 |
| Action 白名单 | 仅 6 种动作，未知 action 拒绝                                                                         |
| 步数限制       | 计划 ≤ 40 步（`max_steps` 可调）                                                                    |
| 超时         | 导航 30s、动作 15s 默认（可调）                                                                         |
| 无头默认       | `headless=True`，除非显式 `--headed`                                                              |
| 逃生门        | CLI `--allow-all-hosts`，或环境变量 `BROWSER_ALLOW_ALL_HOSTS=1`（worker / HTTP API 链路）；启用后放行所有 host，仅限本地自用  |
| API 鉴权     | `API_KEYS` Bearer；空 = 关闭；`/health` 匿名                                                        |
| 路径穿越防护     | `run_id` / 截图名双重校验（`/`、`\`、`..` 拒绝）；解析后路径必须落在 `runs_root` 内                                  |
| 产物读取       | 服务端流式代理，不向前端暴露 S3 凭据                                                                         |



***

## 12. 测试



```
python -m pytest tests -q   # 78 passed（不启动 Chromium）
```



| 文件                         | 覆盖                   |
| -------------------------- | -------------------- |
| `tests/test_agent.py`      | 计划校验、动作、归档渲染         |
| `tests/test_api.py`        | API 端点、鉴权四象限、取消、路径穿越 |
| `tests/test_service.py`    | run\_plan 服务、计划来源解析  |
| `tests/test_task_store.py` | 文件 TaskStore CRUD    |
| `tests/test_templates.py`  | 模板库 CRUD、模板 API、planner 关键词路由 |
| `tests/test_webhook.py`    | 签名向量、payload、发送重试、终态触发集成 |
| `tests/test_alembic.py`    | 离线 SQL 双版本校验；独立库 upgrade/downgrade 集成（无容器时跳过） |
| `tests/test_auth.py`      | 密码哈希/JWT/API Key、principal 解析、RBAC、owner 隔离、Postgres 端到端 |
| `tests/test_autoscale.py` | 扩缩策略纯函数；真实队列集成（扩到 2 → 并行消费 → 缩回，无 Redis 时跳过） |
| `tests/test_migrate_tasks.py` | 文件库 → Postgres 迁移：导入 / 幂等跳过 / 覆盖更新 / 字段核验（独立测试库） |
| `tests/test_template_variables.py` | 变量渲染纯函数；Postgres 模板库 CRUD/可见性（独立库）；run 端点端到端（缺变量 422 / 默认值 / 覆盖 / webhook） |

**已知测试缺口**：



* Postgres / MinIO 后端无自动化测试（P1 采用手工端到端验证，证据存于 `_p1_evidence/`：report.md + 2 张截图，以及验证记录文档 `memoized-napping-wreath.md`）。

* 真实浏览器（Playwright）不在单测范围内，需用 `--demo` 或 API demo 任务冒烟。



***

## 13. 已知问题与注意事项



| # | 问题                                                                                     | 影响 / 建议                                                                                         |
| - | -------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| 1 | ~~`requirements.txt` / `pyproject.toml` 未含 P1 依赖~~（2026-09-15 已补齐）                     | ✅ 已解决：`sqlalchemy` / `psycopg[binary]` / `boto3` 已写入两份依赖清单                |
| 2 | ~~版本号不同步~~（2026-09-15 已统一）                                                           | ✅ 已解决：`pyproject.toml` 与 FastAPI 应用均统一为 0.3.0                                      |
| 3 | ~~README「Out of scope (later)」段落过时~~（2026-09-15 已更新）                                 | ✅ 已解决：过时段落已修正，Layout / Config / Endpoints 同步                                        |
| 4 | ~~Postgres 建表用启动时 `create_all`，无 Alembic 迁移~~（✅ 已解决：Alembic 版本化管理，启动自动升级） | 表结构演进无版本化管理 → 已有 `alembic/` 迁移链（0001 初始表 + 0002 webhook_url）                             |
| 5 | 无 Postgres/MinIO 自动化测试                                                                       | 依赖手工验证                                                                                          |
| 6 | `data/tasks/*.json` → Postgres 历史迁移脚本未做                                                | 切到 Postgres 后旧任务不可见（仅本地文件）                                                                      |
| 7 | ~~仓库根 `readme.md` 未同步~~（2026-09-15 已更新）                                                | ✅ 已解决：状态表同步至模板 CRUD 落地                                                                        |
| 8 | 当前 `.env` 仅配置 `DOUBAO_API_KEY` / `DOUBAO_MODEL`                                        | 项目默认运行在 P0 本地模式；启用 P1 需按 `.env.example` 设置                                                      |
| 9 | ~~`project_tree.txt` 过时~~（2026-09-15 已重写）                                                  | ✅ 已解决：以 UTF-8 重写，含 `api/`、`worker/`、`store/`、`tests/test_templates.py`            |



***

## 14. 路线图

按优先级建议：

### 阶段一：闭环 Agent 稳定性（已完成）
- LLM 决策失败自动重试一次
- 观察脚本支持 shadow DOM / 同源 iframe
- LLM JSON 解析兜底（fence / 尾逗号 / 前后文字）
- Agent 轨迹 API + 网页时间线 UI
- Evidence grounding（done 时必须附页面原文证据，代码三层校验）

### 阶段二：可观测性与韧性（已完成）
- 任务实时日志流（SSE /tasks/{id}/stream）
- Worker 崩溃自动恢复（RQ Retry + restart:unless-stopped + autoscale）
- 任务取消真正生效（合作式 cancel_check + finally 关浏览器）

### 阶段三：能力补全（已完成）
- 多标签页支持（new_tab / switch_tab / close_tab / goto new_tab:true）

### 阶段四：收尾打磨（已完成）
- 视觉 grounding（截图喂 VLM）— 跳过，需换多模态模型
- 二次自检：done 后让 LLM 对照页面文本核对 summary 里的数字
- 元素编号化：观察时给元素 @N 编号，LLM 决策必须引用，执行时翻译成真实 selector
- 任务模板变量在 agent 模式 — 跳过，需 DB schema 迁移
- 前端任务列表加来源筛选下拉
- 部署文档：docs/DEPLOYMENT.md（本地自用 + 生产 Nginx/HTTPS/SSE 反代）

### 阶段五：高级能力规划（待启动）

参考 browser-use / Stagehand v3 / Skyvern 的架构演进，分三批：

**第一批：元素缓存（Stagehand v3 思路，已完成）**
- 实现：同 URL 连续两轮观察时，prompt 里不列元素列表，只提示"和上一轮相同，复用 @refs"
- 效果：每步省 ~500 token（元素列表约 15 行）
- 78 测试全过，worker 重建完成

**第二批：跨会话记忆（WebCoach 思路，已完成）**
- 实现：browser_agent/memory.py，SQLite 存历史成功记录
- 新任务前按关键词重叠度检索 top-2 相似经验，塞进 system prompt
- 长任务不重复踩坑；memory 异常不影响主流程
- 78 测试全过，实际任务 succeeded

**第三批：视觉 grounding（Skyvern 思路，已完成开关）**
- 实现：planner.py 加 _call_doubao_vision（messages 带 image_url base64）
- .env 加 USE_VISION=0/1 开关，agent_loop 按开关选文本/视觉调用
- 当前 USE_VISION=0 走纯文本；填视觉模型 Key 后改 1 即启用
- 78 测试全过







***

*本文档由代码、README、P1 设计 / 验证记录（*`memoized-napping-wreath.md`*）与测试结果核对生成；数据口径以仓库实际代码为准。*
