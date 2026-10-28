# 部署指南

## 本地自用（Windows / macOS）

### 前置
- Python 3.11+
- Docker Desktop（跑 worker 容器）
- `.env` 配好 `DOUBAO_API_KEY` + `DOUBAO_MODEL`

### 启动
```powershell
# 1. 起基础设施（redis + postgres + worker）
docker compose up -d redis postgres worker

# 2. 起 API（前台）
uvicorn api.main:app --host 127.0.0.1 --port 8000

# 3. 打开控制台
# 浏览器打开 web-console/index.html，API 地址填 http://127.0.0.1:8000
```

## 生产部署（Linux 服务器）

### 1. 准备
```bash
git clone <repo> /opt/browser-automation-agent
cd /opt/browser-automation-agent
cp .env.example .env
# 编辑 .env：DOUBAO_API_KEY / DOUBAO_MODEL / SECRET_KEY / DATABASE_URL
```

### 2. Docker Compose 全栈
```bash
docker compose up -d --build
```
服务：
- `redis`：RQ 队列
- `postgres`：任务记录
- `worker`：Playwright 浏览器执行器（autoscale 1-3）
- `api`：FastAPI（反代后监听 8000）

### 3. 反向代理（Nginx + HTTPS）
```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;
    ssl_certificate     /etc/ssl/your.crt;
    ssl_certificate_key /etc/ssl/your.key;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 900s;   # agent 任务长
        proxy_buffering off;       # SSE 需要
    }
    location /api/v1/tasks/{id}/stream {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }
}
```

### 4. 安全
- `.env` 必须改 `SECRET_KEY`（`openssl rand -hex 32`）
- 生产环境关掉 `BROWSER_ALLOW_ALL_HOSTS`，配 `BROWSER_ALLOWED_HOSTS` 白名单
- 配好 JWT 用户（默认本地自用无登录）
- worker 容器无 `restart: always` 改 `restart: unless-stopped` 即可

### 5. 验证
```bash
curl http://127.0.0.1:8000/api/v1/health
curl -N http://127.0.0.1:8000/api/v1/tasks/123/stream   # SSE
```

## 故障排查

| 症状 | 排查 |
|---|---|
| API 500 | 看 `uvicorn` 日志；worker 容器 `docker compose logs worker` |
| 任务一直 queued | worker 没起 / Redis 不通：`docker compose ps` |
| 截图全白 | 容器 headless 被反爬，UA 已加 Chrome 120 |
| agent 读错数字 | evidence grounding + 二次自检已开；换更强模型 |
| SSE 不实时 | 反代关 buffering；Nginx 配置见上 |
