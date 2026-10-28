"""FastAPI application entry."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes_auth import router as auth_router
from api.routes_tasks import router as tasks_router
from api.routes_templates import router as templates_router
from api.schemas import HealthResponse
from settings import get_settings
from worker.queue import ping_redis

app = FastAPI(
    title="Browser Automation Agent API",
    version="0.3.0",
    description=(
        "P1: FastAPI + Redis/RQ + optional Bearer API_KEYS; "
        "file or Postgres TaskStore; local or S3/MinIO artifacts; "
        "multi-tenant auth (users / API keys / JWT / RBAC)."
    ),
)
# Local self-hosted console (static index.html) calls the API directly from the
# browser; allow any local origin (file://, http://localhost/127.0.0.1).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(tasks_router)
app.include_router(templates_router)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    ok = ping_redis()
    return HealthResponse(
        status="ok" if ok else "degraded",
        redis=ok,
        queue=settings.rq_queue,
    )
