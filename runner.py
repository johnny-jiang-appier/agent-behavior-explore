"""Conversation loop: session -> multi-turn send/receive -> result."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from client.orchestrator import OrchestratorClient
from controller.decide import decide_next_step
from controller.reviewer import review_session
from dashboard import ScenarioStatus

logger = logging.getLogger(__name__)


async def run_scenario(
    client: OrchestratorClient,
    session_id: str,
    prompt: str,
    *,
    scenario_name: str | None = None,
    controller_instructions: str | None = None,
    steps: list[dict] | None = None,
    review_instructions: str | None = None,
    responses: list[dict] | None = None,
    max_turns: int = 30,
    progress_cb: Callable[..., None] | None = None,
) -> dict:
    """
    Run a full conversation scenario.

    Returns result dict with status, history, tool calls, raw events, etc.
    """
    history = []
    token_usage_turns = []
    token_totals = {"prompt_token_count": 0, "candidates_token_count": 0, "total_token_count": 0}
    user_input = prompt

    if progress_cb:
        progress_cb(status=ScenarioStatus.RUNNING, turn=0, max_turns=max_turns)
    else:
        logger.info("=== Starting scenario: %s ===", scenario_name)

    for turn in range(1, max_turns + 1):
        if not progress_cb:
            logger.info("[Turn %d/%d] User -> %s", turn, max_turns, user_input[:80])

        # Send message and get response
        turn_data = await client.send_message(session_id, user_input)

        agent_text = turn_data["agent"]

        if progress_cb:
            progress_cb(turn=turn, detail=f"Agent \u2192 {agent_text[:150]}")
        else:
            logger.info("[Turn %d/%d] Agent -> %s", turn, max_turns, agent_text[:100])
            if turn_data["langfuse_trace_url"]:
                logger.info("[Turn %d/%d] Langfuse: %s", turn, max_turns, turn_data["langfuse_trace_url"])
            if turn_data["tool_calls"]:
                logger.info("[Turn %d/%d] Tool calls: %s", turn, max_turns,
                            [tc["name"] for tc in turn_data["tool_calls"]])

        # Record turn
        turn_record = {
            "user": user_input,
            "agent": agent_text,
            "langfuse_trace_url": turn_data["langfuse_trace_url"],
            "tool_calls": turn_data["tool_calls"],
        }
        history.append(turn_record)

        # Ask controller what to do next (retry with backoff)
        decision = None
        usage = None
        ctrl_delays = [10, 30, 60]
        ctrl_history = [{
            "user": h["user"],
            "agent": h["agent"],
            "tool_calls": [tc["name"] for tc in h.get("tool_calls", [])],
        } for h in history]
        ctrl_kwargs = dict(
            history=ctrl_history, last_user_input=user_input, agent_response=agent_text,
            controller_instructions=controller_instructions, steps=steps,
        )
        for ctrl_attempt in range(1, 4):
            try:
                decision, usage = decide_next_step(**ctrl_kwargs)
                break
            except Exception as e:
                delay = ctrl_delays[ctrl_attempt - 1]
                logger.error("[Turn %d/%d] Controller error (attempt %d/3, wait %ds): %s", turn, max_turns, ctrl_attempt, delay, e)
                await asyncio.sleep(delay)

        if decision is None:
            logger.warning("[Turn %d/%d] Controller failed 3 times, waiting 180s for final attempt", turn, max_turns)
            await asyncio.sleep(180)
            try:
                decision, usage = decide_next_step(**ctrl_kwargs)
            except Exception as e:
                logger.error("[Turn %d/%d] Controller failed after final attempt, forcing continue: %s", turn, max_turns, e)
                decision = {"verdict": "continue", "result": "pass", "reason": f"Controller error: {e}", "next_user_input": "請繼續"}

        if usage:
            token_usage_turns.append(usage)
            for k in ("prompt_token_count", "candidates_token_count", "total_token_count"):
                token_totals[k] += usage.get(k, 0)

        if progress_cb:
            reason_text = decision.get("reason", "")[:120]
            progress_cb(detail=f"Controller \u2192 {decision['verdict']}: {reason_text}")
        else:
            logger.info("[Turn %d/%d] Controller -> verdict=%s result=%s reason=%s",
                        turn, max_turns, decision["verdict"], decision.get("result"), decision.get("reason", "")[:80])

        if decision["verdict"] == "stop":
            test_result = decision.get("result", "pass")
            status = "completed" if test_result == "pass" else "failed"
            result = _build_result(
                session_id=session_id,
                scenario_name=scenario_name,
                status=status,
                reason=decision.get("reason"),
                prompt=prompt,
                controller_instructions=controller_instructions,
                steps=steps,
                history=history,
                token_usage_turns=token_usage_turns,
                token_totals=token_totals,
            )
            await _run_review(result, review_instructions, responses, progress_cb)
            if progress_cb:
                _report_done(result, progress_cb)
            return result

        next_input = decision.get("next_user_input")
        if not next_input:
            next_input = "Please continue."
            logger.warning("[Turn %d/%d] Empty next_user_input, using fallback", turn, max_turns)
        user_input = next_input

    if not progress_cb:
        logger.info("Max turns reached (%d)", max_turns)
    result = _build_result(
        session_id=session_id,
        scenario_name=scenario_name,
        status="max_turns_reached",
        reason=f"Max turns ({max_turns}) reached",
        prompt=prompt,
        controller_instructions=controller_instructions,
        steps=steps,
        history=history,
        token_usage_turns=token_usage_turns,
        token_totals=token_totals,
    )
    await _run_review(result, review_instructions, responses, progress_cb)
    if progress_cb:
        _report_done(result, progress_cb)
    return result


def _report_done(result: dict, progress_cb: Callable[..., None]) -> None:
    """Report final status to dashboard."""
    status_str = result["status"]
    sid = result.get("session_id", "")[:8]
    scores = result.get("review", {}).get("scores", {})
    if scores:
        passed = sum(1 for v in scores.values() if v == 1)
        total = len(scores)
        detail = f"{status_str} \u00b7 {sid} \u00b7 {passed}/{total} passed"
    else:
        detail = f"{status_str} \u00b7 {sid}"

    ds = ScenarioStatus.DONE if status_str in ("completed", "max_turns_reached") else ScenarioStatus.ERROR
    progress_cb(status=ds, detail=detail)


async def _run_review(
    result: dict,
    review_instructions: str | None,
    responses: list[dict] | None,
    progress_cb: Callable[..., None] | None = None,
) -> None:
    """Run session-level review if instructions and responses are provided."""
    if not review_instructions or not responses:
        return

    if progress_cb:
        progress_cb(status=ScenarioStatus.REVIEWING, review_total=len(responses), review_done=0)
    else:
        logger.info("Running session review (%d response metrics)...", len(responses))

    review = await review_session(
        history=result["history"],
        review_instructions=review_instructions,
        responses=responses,
        progress_cb=progress_cb,
    )
    result["review"] = review

    if not progress_cb:
        logger.info("Review complete: %s", {k: v for k, v in review["scores"].items()})


def _build_result(**kwargs) -> dict:
    return {
        "session_id": kwargs["session_id"],
        "scenario_name": kwargs["scenario_name"],
        "status": kwargs["status"],
        "reason": kwargs["reason"],
        "prompt": kwargs["prompt"],
        "controller_instructions": kwargs["controller_instructions"],
        "steps": kwargs["steps"],
        "timestamp": datetime.now().isoformat(),
        "turns": len(kwargs["history"]),
        "history": kwargs["history"],
        "token_usage": {
            "by_turn": kwargs["token_usage_turns"],
            "total": kwargs["token_totals"],
        },
    }
