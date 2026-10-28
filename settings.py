"""Project settings loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def package_root() -> Path:
    return Path(__file__).resolve().parent


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or (package_root() / ".env")
    if not env_path.is_file():
        return
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if not key:
            continue
        if key not in os.environ or not os.environ.get(key):
            os.environ[key] = val


load_dotenv()


@dataclass(frozen=True)
class Settings:
    redis_url: str = "redis://127.0.0.1:6379/0"
    rq_queue: str = "browser_tasks"
    max_task_seconds: int = 300
    max_browser_workers: int = 1
    runs_root: Path = package_root() / "runs"
    data_root: Path = package_root() / "data"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # Comma-separated shared keys. Empty = auth disabled (local demo).
    api_keys: str = ""
    # file | postgres
    task_store: str = "file"
    # file | postgres
    template_store: str = "file"
    database_url: str = ""
    # local | s3
    artifact_store: str = "local"
    s3_endpoint: str = "http://127.0.0.1:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "browser-runs"
    s3_region: str = "us-east-1"
    # Webhook: optional terminal-event callback (comma-separated URLs allowed).
    webhook_url: str = ""
    webhook_secret: str = ""
    # Multi-tenant / auth: JWT signing secret + token lifetime.
    jwt_secret: str = ""
    jwt_expires_minutes: int = 1440

    @property
    def tasks_dir(self) -> Path:
        return self.data_root / "tasks"

    @property
    def templates_dir(self) -> Path:
        return self.data_root / "templates"

    @classmethod
    def from_env(cls) -> "Settings":
        root = package_root()
        runs = os.environ.get("RUNS_ROOT", "").strip()
        data = os.environ.get("DATA_ROOT", "").strip()
        return cls(
            redis_url=os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0").strip()
            or "redis://127.0.0.1:6379/0",
            rq_queue=os.environ.get("RQ_QUEUE", "browser_tasks").strip() or "browser_tasks",
            max_task_seconds=max(30, int(os.environ.get("MAX_TASK_SECONDS", "300"))),
            max_browser_workers=max(1, int(os.environ.get("MAX_BROWSER_WORKERS", "1"))),
            runs_root=Path(runs) if runs else (root / "runs"),
            data_root=Path(data) if data else (root / "data"),
            api_host=os.environ.get("API_HOST", "127.0.0.1").strip() or "127.0.0.1",
            api_port=int(os.environ.get("API_PORT", "8000")),
            api_keys=os.environ.get("API_KEYS", "").strip(),
            task_store=(os.environ.get("TASK_STORE", "file").strip() or "file").lower(),
            template_store=(
                os.environ.get("TEMPLATE_STORE", "file").strip() or "file"
            ).lower(),
            database_url=os.environ.get("DATABASE_URL", "").strip(),
            artifact_store=(
                os.environ.get("ARTIFACT_STORE", "local").strip() or "local"
            ).lower(),
            s3_endpoint=os.environ.get("S3_ENDPOINT", "http://127.0.0.1:9000").strip()
            or "http://127.0.0.1:9000",
            s3_access_key=os.environ.get("S3_ACCESS_KEY", "minioadmin").strip()
            or "minioadmin",
            s3_secret_key=os.environ.get("S3_SECRET_KEY", "minioadmin").strip()
            or "minioadmin",
            s3_bucket=os.environ.get("S3_BUCKET", "browser-runs").strip() or "browser-runs",
            s3_region=os.environ.get("S3_REGION", "us-east-1").strip() or "us-east-1",
            webhook_url=os.environ.get("WEBHOOK_URL", "").strip(),
            webhook_secret=os.environ.get("WEBHOOK_SECRET", "").strip(),
            jwt_secret=os.environ.get("SECRET_KEY", "").strip()
            or "browser-agent-dev-secret-change-me",
            jwt_expires_minutes=max(
                5, int(os.environ.get("JWT_EXPIRES_MINUTES", "1440"))
            ),
        )


def get_settings() -> Settings:
    return Settings.from_env()
