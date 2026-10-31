"""Unit tests that do not require a browser binary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from browser_agent.actions import ActionResult
from browser_agent.agent_loop import _parse_decision, _prompt, _result_summary
from browser_agent.planner import demo_plan, template_plan
from browser_agent.safety import SafetyConfig, assert_plan_safe, assert_url_allowed
from browser_agent.schema import PlanValidationError, load_plan_file, parse_plan


def test_parse_plan_valid():
    plan = parse_plan(
        {
            "name": "t",
            "steps": [
                {"action": "goto", "url": "https://example.com/"},
                {"action": "screenshot", "name": "a"},
                {"action": "extract", "field": "title"},
            ],
        }
    )
    assert len(plan.steps) == 3
    assert plan.steps[0].action == "goto"


def test_parse_rejects_bad_action():
    with pytest.raises(PlanValidationError):
        parse_plan({"name": "t", "steps": [{"action": "eval", "code": "1"}]})


def test_parse_rejects_non_http_goto():
    with pytest.raises(PlanValidationError):
        parse_plan({"name": "t", "steps": [{"action": "goto", "url": "file:///etc/passwd"}]})


def test_demo_plan_is_safe():
    plan = demo_plan()
    cfg = SafetyConfig.from_env(headless=True, allow_all_hosts=False)
    assert_plan_safe(plan, cfg)
    assert any(s.action == "screenshot" for s in plan.steps)
    assert any(s.action == "extract" for s in plan.steps)


def test_host_allowlist():
    cfg = SafetyConfig(allowed_hosts={"example.com"}, allow_all_hosts=False)
    assert_url_allowed("https://example.com/x", cfg)
    with pytest.raises(PlanValidationError):
        assert_url_allowed("https://evil.example/x", cfg)


def test_template_open_url():
    plan = template_plan("open https://example.org/foo")
    assert plan is not None
    assert plan.steps[0].action == "goto"
    assert plan.steps[0].url.startswith("https://example.org")


def test_load_bundled_plan():
    path = Path(__file__).resolve().parent.parent / "plans" / "example_com.json"
    plan = load_plan_file(path)
    assert plan.name == "example-com-basic"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert len(raw["steps"]) == len(plan.steps)


def test_agent_prompt_includes_visible_page_text():
    obs = {
        "url": "http://127.0.0.1:8099/",
        "title": "库存管理演示",
        "text": "静音机械键盘 K87 ¥399 12 在售",
        "elements": [],
    }
    prompt = _prompt("找到机械键盘的价格和库存", [], obs)
    assert "静音机械键盘 K87 ¥399 12 在售" in prompt


def test_agent_history_includes_extracted_text():
    result = ActionResult(
        ok=True,
        action="extract",
        message="extracted text",
        data={"field": "text", "value": "静音机械键盘 K87 ¥399 12"},
    )
    summary = _result_summary(result)
    assert summary.startswith("extracted text")
    assert "¥399" in summary


def test_agent_decision_tolerates_raw_control_characters():
    # a model copying table text can emit literal tabs, which invalidate JSON
    raw = (
        '{"thought": "done", "action": null, "done": true, '
        '"summary": "价格 399，库存 12", '
        '"evidence": "静音机械键盘 K87\tKB-087\t¥399\t12"}'
    )
    decision = _parse_decision(raw)
    assert decision["done"] is True
    assert decision["evidence"].startswith("静音机械键盘 K87 KB-087")
