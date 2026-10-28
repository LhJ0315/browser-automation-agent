"""Safety guards: host allowlist, action allowlist, timeouts, headless."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .schema import ALLOWED_ACTIONS, Plan, PlanValidationError


DEFAULT_ALLOWED_HOSTS = frozenset(
    {
        "example.com",
        "www.example.com",
        "example.org",
        "www.example.org",
        "example.net",
        "www.example.net",
        "httpbin.org",
        "www.httpbin.org",
        "info.cern.ch",
    }
)


@dataclass
class SafetyConfig:
    allowed_hosts: set[str] = field(default_factory=lambda: set(DEFAULT_ALLOWED_HOSTS))
    allowed_actions: frozenset[str] = ALLOWED_ACTIONS
    headless: bool = True
    navigation_timeout_ms: int = 30_000
    action_timeout_ms: int = 15_000
    max_steps: int = 40
    allow_all_hosts: bool = False  # explicit escape hatch

    @classmethod
    def from_env(cls, *, headless: bool | None = None, allow_all_hosts: bool = False) -> "SafetyConfig":
        hosts_env = os.environ.get("BROWSER_ALLOWED_HOSTS", "").strip()
        if hosts_env:
            hosts = {h.strip().lower() for h in hosts_env.split(",") if h.strip()}
        else:
            hosts = set(DEFAULT_ALLOWED_HOSTS)
        # Containerized workers have no display server; force headless there.
        force_headless = os.environ.get(
            "BROWSER_FORCE_HEADLESS", ""
        ).strip().lower() in {"1", "true", "yes"}
        if force_headless:
            headless = True
        elif headless is None:
            headless = True
        # Escape hatch via env (HTTP API/worker path): BROWSER_ALLOW_ALL_HOSTS=1
        env_allow_all = os.environ.get(
            "BROWSER_ALLOW_ALL_HOSTS", ""
        ).strip().lower() in {"1", "true", "yes"}
        return cls(
            allowed_hosts=hosts,
            headless=bool(headless),
            allow_all_hosts=bool(allow_all_hosts or env_allow_all),
        )


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def assert_url_allowed(url: str, cfg: SafetyConfig) -> None:
    if cfg.allow_all_hosts:
        return
    host = host_of(url)
    if not host:
        raise PlanValidationError(f"url has no host: {url!r}")
    # allow exact host or parent listed (www.example.com if example.com allowed)
    if host in cfg.allowed_hosts:
        return
    # also accept bare domain match when www. variant listed
    if host.startswith("www.") and host[4:] in cfg.allowed_hosts:
        return
    if f"www.{host}" in cfg.allowed_hosts:
        return
    raise PlanValidationError(
        f"host {host!r} not in allowlist; set --allow-all-hosts or BROWSER_ALLOWED_HOSTS"
    )


def assert_plan_safe(plan: Plan, cfg: SafetyConfig) -> None:
    if len(plan.steps) > cfg.max_steps:
        raise PlanValidationError(f"too many steps: {len(plan.steps)} > {cfg.max_steps}")
    for i, step in enumerate(plan.steps):
        if step.action not in cfg.allowed_actions:
            raise PlanValidationError(f"step[{i}] action {step.action!r} not allowed")
        if step.action == "goto" and step.url:
            assert_url_allowed(step.url, cfg)
