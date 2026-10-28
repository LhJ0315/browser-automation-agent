"""FastAPI request/response schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class TaskCreateRequest(BaseModel):
    demo: bool = False
    task_nl: str | None = None
    json_plan: dict[str, Any] | None = None
    agent: bool = False
    headed: bool = False
    max_replans: int = Field(default=0, ge=0, le=2)
    # Optional per-task webhook URL; overrides global WEBHOOK_URL.
    webhook_url: str | None = None

    @model_validator(mode="after")
    def exactly_one_source(self) -> "TaskCreateRequest":
        if self.task_nl is not None:
            self.task_nl = self.task_nl.strip() or None
        if self.agent:
            # closed-loop agent mode: task_nl is the goal
            if not self.task_nl:
                raise ValueError("agent=true requires task_nl (the goal)")
            return self
        sources = sum(
            [
                bool(self.demo),
                bool(self.task_nl),
                self.json_plan is not None,
            ]
        )
        if sources != 1:
            raise ValueError("provide exactly one of demo=true, task_nl, or json_plan")
        return self


class TaskSummary(BaseModel):
    task_id: str
    status: str
    source: str
    created_at: str
    updated_at: str
    run_id: str | None = None
    summary: str | None = None
    cancel_requested: bool = False


class TaskDetail(TaskSummary):
    task_nl: str | None = None
    headed: bool = False
    max_replans: int = 0
    rq_job_id: str | None = None
    run_ids: list[str] = Field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    step_count: int | None = None
    progress_summary: str | None = None
    webhook_url: str | None = None


class TaskCreateResponse(BaseModel):
    task_id: str
    status: Literal["queued"]
    rq_job_id: str | None = None


class CancelResponse(BaseModel):
    task_id: str
    status: str
    cancel_requested: bool
    detail: str


class ScreenshotInfo(BaseModel):
    name: str
    path: str


class HealthResponse(BaseModel):
    status: str
    redis: bool
    queue: str


# ---- Plan templates (CRUD) ----


class TemplatePayload(BaseModel):
    name: str
    description: str = ""
    match: str = ""
    steps: list[dict[str, Any]] = Field(default_factory=list)
    # Variable defaults: {"var": "default text"} for {{var}} placeholders in steps.
    variables: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_steps(self) -> "TemplatePayload":
        from browser_agent.schema import PlanValidationError, parse_plan

        try:
            parse_plan(
                {"name": self.name, "description": self.description, "steps": self.steps},
                source="template",
            )
        except PlanValidationError as exc:
            raise ValueError(str(exc)) from exc
        return self


class TemplateSummary(BaseModel):
    name: str
    description: str = ""
    match: str = ""
    step_count: int = 0
    variable_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class TemplateDetail(TemplateSummary):
    steps: list[dict[str, Any]] = Field(default_factory=list)
    variables: dict[str, str] = Field(default_factory=dict)


class TemplateUpdate(BaseModel):
    description: str | None = None
    match: str | None = None
    steps: list[dict[str, Any]] | None = None
    variables: dict[str, str] | None = None


class TemplateRunRequest(BaseModel):
    # Run-time values for {{var}} placeholders (override template defaults).
    variables: dict[str, str] = Field(default_factory=dict)
    headed: bool = False
    max_replans: int = Field(default=0, ge=0, le=2)
    # Optional per-task webhook URL; overrides global WEBHOOK_URL.
    webhook_url: str | None = None


# ---- Auth / multi-tenant (P1) ----


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    user_id: str
    username: str
    role: str
    status: str
    created_at: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserOut


class MeResponse(BaseModel):
    principal_id: str
    username: str | None = None
    role: str
    auth_type: str
    is_admin: bool


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)
    role: Literal["admin", "user", "read_only"] = "user"


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    owner_username: str = Field(min_length=1, max_length=64)
    role: Literal["admin", "user", "read_only"] | None = None
    expires_at: str | None = None


class ApiKeyCreatedResponse(BaseModel):
    key_id: str
    name: str
    key_prefix: str
    owner_id: str
    owner_username: str
    role: str | None = None
    created_at: str
    api_key: str  # plaintext, shown exactly once


class ApiKeySummary(BaseModel):
    key_id: str
    name: str
    key_prefix: str
    owner_id: str
    role: str | None = None
    expires_at: str | None = None
    revoked_at: str | None = None
    last_used_at: str | None = None
    created_at: str
