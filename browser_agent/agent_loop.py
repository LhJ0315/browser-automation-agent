"""Closed-loop agent executor.

Unlike execute_plan (which mechanically runs a pre-written step list), the agent
loop works goal-first: every round it *observes* the live page, asks the LLM
what to do next, executes that one step, feeds the result back, and repeats
until the goal is reached or the step budget is exhausted.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .actions import ActionResult, run_step
from . import memory
from .archive import RunArchive, StepRecord, utc_now
from .planner import _call_doubao, _call_doubao_vision, _doubao_config
from .safety import SafetyConfig, assert_url_allowed
from .schema import Step, validate_step

MAX_STEP_BUDGET = 18  # hard cap per agent run


_OBSERVE_JS = r"""
() => {
  const items = [];
  const SEL = 'a[href], button, input, select, textarea, [role="button"], [onclick]';
  function push(el) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    let sel = '';
    if (el.id) sel = '#' + el.id;
    else if (el.name) sel = el.tagName.toLowerCase() + '[name="' + el.name + '"]';
    items.push({
      ref: '@' + items.length,
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      name: el.name || '',
      type: el.type || '',
      placeholder: el.placeholder || '',
      text: (el.innerText || el.value || '').trim().slice(0, 50),
      href: el.getAttribute('href') || '',
      selector: sel
    });
  }
  // walk light DOM + open shadow roots recursively
  const walker = document.createTreeWalker(document, NodeFilter.SHOW_ELEMENT);
  let node;
  while ((node = walker.nextNode()) && items.length < 20) {
    if (node.matches && node.matches(SEL)) push(node);
    if (node.shadowRoot) {
      const sw = node.shadowRoot.createTreeWalker(node.shadowRoot, NodeFilter.SHOW_ELEMENT);
      let sn;
      while ((sn = sw.nextNode()) && items.length < 20) {
        if (sn.matches && sn.matches(SEL)) push(sn);
      }
    }
  }
  return {
    url: location.href,
    title: document.title,
    text: (document.body && document.body.innerText || '').slice(0, 600),
    elements: items
  };
}
"""


def _prompt(goal: str, history: list[dict], obs: dict, *, same_page: bool = False) -> str:
    lines = ["You are a browser automation agent. You are controlling one web page.",
             f"Goal: {goal}", ""]
    lines.append("What you have done so far:")
    if not history:
        lines.append("- (nothing yet)")
    for i, h in enumerate(history[-10:], 1):
        lines.append(f"{i}. action={h['action']} ok={h['ok']} -> {h['summary']}")
    lines.append("")
    lines.append("Current page:")
    lines.append(f"URL: {obs.get('url')}")
    lines.append(f"Title: {obs.get('title')}")
    lines.append("Visible page text (truncated, quote evidence from here):")
    lines.append((obs.get("text") or "(empty)").strip() or "(empty)")
    if same_page:
        lines.append("Visible interactive elements: same as previous round (reuse @refs).")
    else:
        lines.append("Visible interactive elements (up to 40):")
        for e in obs.get("elements", []):
            lines.append(
                f"  - {e.get('ref','')} <{e['tag']}> text={e['text']!r} placeholder={e['placeholder']!r} "
                f"href={e['href']!r} selector={e['selector']!r}"
            )
    lines.append("")
    lines.append("""Respond with ONE JSON object only (no markdown):
{"thought": "one short sentence", "action": {"action": "click", "selector": "css"}, "done": false}
- action is one of: goto{url,new_tab?}, click{selector}, type{selector,text}, wait{ms},
  extract{field,selector?}, new_tab{url?}, switch_tab{tab_index}, close_tab{tab_index}.
  Do NOT use screenshot; every round auto-screenshots.
  Tabs are numbered from 0. Use new_tab to open a new tab, switch_tab to focus one.
- Prefer the exact selector from the element list. If you must open a new page, use goto.
- When the goal is achieved, set "done": true. Required fields in that object:
  "summary" must contain the actual answer WITH its number and unit (e.g. "佛山当前温度 34°C").
  "evidence" must be a verbatim short phrase copied from the page text (e.g. "34°C Mostly sunny"),
  not a description. If the page shows "34°C" and "12 km/h", the temperature is 34, not 12 — match the unit to the question.
  Example done object: {"thought":"...","action":null,"done":true,"summary":"佛山当前温度 34°C","evidence":"34°C Mostly sunny"}
- Keep total steps under 12. If stuck after a wrong step, try a different selector.""")
    return "\n".join(lines)


def _parse_decision(raw: str) -> dict:
    text = (raw or "").strip()
    # strip ```json ... ``` fences if the model wrapped the JSON
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    candidates = [text]
    # also try the outermost {...} block
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        candidates.append(m.group(0))

    def _loads(cand: str) -> dict:
        # raw tabs/newlines copied from a table make the JSON invalid; they are
        # safe to collapse to spaces because evidence matching ignores whitespace
        cleaned = re.sub(r"[\x00-\x1f]", " ", cand)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            data = json.loads(re.sub(r",\s*([}\]])", r"\1", cleaned))
        if not isinstance(data, dict):
            raise json.JSONDecodeError("decision is not an object", cleaned, 0)
        return data

    for cand in candidates:
        try:
            return _loads(cand)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"agent LLM returned non-JSON decision: {text[:160]!r}")


def _result_summary(result: ActionResult) -> str:
    summary = result.error or result.message
    if result.ok and result.action == "extract":
        value = str(result.data.get("value") or "").strip()
        if value:
            summary = f"{summary}: {value[:300]}"
    return summary[:400]


def run_agent_loop(
    goal: str,
    archive: RunArchive,
    *,
    safety: SafetyConfig,
    cancel_check: Callable[[], bool] | None = None,
) -> RunArchive:
    from playwright.sync_api import sync_playwright  # lazy

    cfg_llm = _doubao_config()
    if not cfg_llm:
        archive.finalize(
            success=False,
            summary="agent mode needs DOUBAO_API_KEY + DOUBAO_MODEL (or ARK_*)",
        )
        return archive

    archive.meta.update(
        {
            "mode": "agent-loop",
            "goal": goal,
            "headless": safety.headless,
            "allow_all_hosts": safety.allow_all_hosts,
        }
    )
    archive.write_plan()

    history: list[dict] = []
    last_summary = ""
    done = False
    cancelled = False
    failed = False
    fail_reason = ""
    step_index = 0
    prev_url = None
    use_vision = os.environ.get("USE_VISION", "0") == "1"
    past = memory.format_for_prompt(goal)

    with sync_playwright() as p:
        launch_kwargs: dict[str, Any] = {"headless": safety.headless}
        executable = os.environ.get("BROWSER_EXECUTABLE", "").strip()
        if executable:
            launch_kwargs["executable_path"] = executable
        launch_kwargs["args"] = [
            "--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu",
            "--disable-extensions", "--disable-background-networking",
            "--disable-sync", "--no-first-run",
        ]
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.set_default_navigation_timeout(safety.navigation_timeout_ms)
        page.set_default_timeout(safety.action_timeout_ms)

        try:
            while step_index < MAX_STEP_BUDGET:
                if cancel_check and cancel_check():
                    cancelled = True
                    last_summary = "cancelled"
                    break

                try:
                    obs = page.evaluate(_OBSERVE_JS)
                except Exception:
                    # page is mid-navigation; wait briefly and retry once
                    page.wait_for_timeout(500)
                    try:
                        obs = page.evaluate(_OBSERVE_JS)
                    except Exception:
                        obs = {"url": "about:blank", "title": "", "text": "", "elements": []}
                # merge observations from same-origin child frames (iframes)
                try:
                    merged_elements = list(obs.get("elements") or [])
                    for fr in page.frames[1:]:
                        try:
                            fobs = fr.evaluate(_OBSERVE_JS)
                            merged_elements.extend(fobs.get("elements") or [])
                        except Exception:
                            continue
                    obs["elements"] = merged_elements[:20]
                except Exception:
                    pass
                # auto screenshot every round for traceability
                page.wait_for_timeout(300)
                shot_path = archive.steps_dir / f"{step_index + 1:02d}_obs.png"
                archive.steps_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shot_path))
                shot_rel = shot_path.relative_to(archive.run_dir).as_posix()

                try:
                    prompt = past + _prompt(goal, history, obs, same_page=(prev_url == obs.get("url")))
                    if use_vision:
                        import base64
                        raw = _call_doubao_vision(prompt, cfg_llm, base64.b64encode(page.screenshot()).decode())
                    else:
                        raw = _call_doubao(prompt, cfg_llm)
                    decision = _parse_decision(raw)
                except Exception as exc:  # noqa: BLE001
                    # one retry before giving up — a single LLM hiccup should not kill the run
                    try:
                        time.sleep(2)
                        prompt = past + _prompt(goal, history, obs, same_page=(prev_url == obs.get("url")))
                        if use_vision:
                            import base64
                            raw = _call_doubao_vision(prompt, cfg_llm, base64.b64encode(page.screenshot()).decode())
                        else:
                            raw = _call_doubao(prompt, cfg_llm)
                        decision = _parse_decision(raw)
                    except Exception as exc2:  # noqa: BLE001
                        failed = True
                        fail_reason = f"LLM decision failed after retry: {exc2}"
                        break

                thought = str(decision.get("thought") or "")
                last_summary = thought
                if decision.get("done"):
                    base = str(decision.get("summary") or thought or "goal done")
                    ev = str(decision.get("evidence") or "").strip()
                    # evidence grounding check: the claimed evidence must actually appear
                    # in the most recent observed page text. If it doesn't, the LLM likely
                    # hallucinated a number — reject this done and keep looping.
                    page_text = (obs or {}).get("text") or ""
                    def _norm(s: str) -> str:
                        return "".join(ch for ch in s.lower() if not ch.isspace())
                    normalized_text = _norm(page_text)
                    if ev and _norm(ev) not in normalized_text:
                        history.append({
                            "step": step_index,
                            "action": "(done rejected)",
                            "summary": f"evidence '{ev}' not found on page; look again carefully",
                            "ok": False,
                        })
                        continue
                    # reject trivially short / unit-less evidence (e.g. just "9")
                    if ev and not re.search(r"\d", ev):
                        history.append({
                            "step": step_index,
                            "action": "(done rejected)",
                            "summary": "evidence must include a number; re-read the page",
                            "ok": False,
                        })
                        continue
                    if ev and len(ev.strip()) < 4:
                        history.append({
                            "step": step_index,
                            "action": "(done rejected)",
                            "summary": "evidence is too short; quote a phrase like '34°C Mostly sunny'",
                            "ok": False,
                        })
                        continue
                    # second-opinion self-check: ask the LLM to verify its own summary
                    # against the page text before accepting done.
                    try:
                        verify_prompt = (
                            "You are a strict fact-checker. The agent just concluded:\n"
                            f"Summary: {base}\nEvidence: {ev}\n\n"
                            f"Page text (truncated):\n{page_text[:2000]}\n\n"
                            "Does the summary accurately match the page text? "
                            "Reply with ONE JSON: {\"agree\": true/false, \"correction\": \"\" or the correct value}. "
                            "Be strict: if the summary says 28°C but the page shows 34°C, agree=false."
                        )
                        raw_v = _call_doubao(verify_prompt, cfg_llm)
                        v = _parse_decision(raw_v)
                        if not v.get("agree", True):
                            correction = str(v.get("correction") or "").strip()
                            history.append({
                                "step": step_index,
                                "action": "(done rejected)",
                                "summary": f"self-check failed: {correction or 'summary does not match page'}",
                                "ok": False,
                            })
                            continue
                    except Exception:
                        # self-check failed to run; accept the done (best effort)
                        pass
                    last_summary = (base + " ｜ 证据: " + ev) if ev else base
                    done = True
                    archive.add_step(StepRecord(
                        index=step_index, action="done", ok=True,
                        message=last_summary, screenshot=shot_rel, data={},
                        started_at=utc_now(), finished_at=utc_now(),
                    ))
                    step_index += 1
                    break

                action_raw = decision.get("action") or {}
                action_raw.setdefault("name", f"step-{step_index + 1}")
                # resolve @N refs to real selectors from the current observation
                sel = action_raw.get("selector")
                if isinstance(sel, str) and sel.startswith("@"):
                    try:
                        idx = int(sel[1:])
                        el = obs.get("elements", [])[idx]
                        real = el.get("selector")
                        if not real and el.get("id"):
                            real = "#" + el["id"]
                        if real:
                            action_raw["selector"] = real
                    except (ValueError, IndexError):
                        pass
                started = utc_now()
                try:
                    step: Step = validate_step(action_raw, step_index)
                    if step.action == "goto" and step.url:
                        assert_url_allowed(step.url, safety)
                    # tab management actions: handled in-process (need context)
                    if step.action == "new_tab":
                        new_page = context.new_page()
                        new_page.set_default_navigation_timeout(safety.navigation_timeout_ms)
                        new_page.set_default_timeout(safety.action_timeout_ms)
                        if step.url and step.url != "about:blank":
                            assert_url_allowed(step.url, safety)
                            new_page.goto(step.url, wait_until="domcontentloaded")
                        page = new_page
                        result = ActionResult(
                            ok=True, action="new_tab",
                            message=f"opened new tab ({len(context.pages)} total)",
                            data={"tab_count": len(context.pages), "url": page.url},
                        )
                    elif step.action == "switch_tab":
                        idx = step.tab_index or 0
                        pages = context.pages
                        if idx >= len(pages):
                            result = ActionResult(ok=False, action="switch_tab",
                                                 error=f"tab_index {idx} out of range (have {len(pages)})")
                        else:
                            page = pages[idx]
                            page.bring_to_front()
                            result = ActionResult(
                                ok=True, action="switch_tab",
                                message=f"switched to tab {idx}",
                                data={"tab_index": idx, "url": page.url},
                            )
                    elif step.action == "close_tab":
                        idx = step.tab_index or 0
                        pages = context.pages
                        if idx >= len(pages):
                            result = ActionResult(ok=False, action="close_tab",
                                                 error=f"tab_index {idx} out of range")
                        else:
                            pages[idx].close()
                            if len(context.pages) > 0:
                                page = context.pages[min(idx, len(context.pages) - 1)]
                                page.bring_to_front()
                            result = ActionResult(
                                ok=True, action="close_tab",
                                message=f"closed tab {idx} ({len(context.pages)} left)",
                                data={"tab_count": len(context.pages)},
                            )
                    elif step.action == "goto" and step.new_tab and step.url:
                        assert_url_allowed(step.url, safety)
                        new_page = context.new_page()
                        new_page.set_default_navigation_timeout(safety.navigation_timeout_ms)
                        new_page.set_default_timeout(safety.action_timeout_ms)
                        new_page.goto(step.url, wait_until="domcontentloaded")
                        page = new_page
                        result = ActionResult(
                            ok=True, action="goto",
                            message=f"navigated new tab to {step.url}",
                            data={"url": page.url, "tab_count": len(context.pages)},
                        )
                    else:
                        result = run_step(
                            page, step,
                            steps_dir=archive.steps_dir,
                            index=step_index,
                            action_timeout_ms=safety.action_timeout_ms,
                        )
                except Exception as exc:  # noqa: BLE001
                    result = ActionResult(ok=False, action=action_raw.get("action", "?"),
                                          error=f"{type(exc).__name__}: {exc}")
                finished = utc_now()

                history.append({
                    "action": action_raw.get("action"),
                    "ok": result.ok,
                    "summary": _result_summary(result),
                })
                archive.add_step(StepRecord(
                    index=step_index,
                    action=action_raw.get("action", "?"),
                    ok=result.ok,
                    message=(thought + " | " + result.message)[:300],
                    error=result.error,
                    screenshot=shot_rel,
                    data=result.data,
                    started_at=started, finished_at=finished,
                ))
                step_index += 1
        finally:
            context.close()
            browser.close()

    if cancelled:
        archive.finalize(success=False, summary="cancelled")
    elif failed:
        archive.finalize(success=False, summary=f"Failed: {fail_reason}")
    elif done:
        archive.finalize(success=True, summary=f"agent done: {last_summary}")
    else:
        archive.finalize(
            success=False,
            summary=f"agent stopped after {step_index} steps (budget {MAX_STEP_BUDGET}): {last_summary}",
        )
    return archive
