"""Conversation loop: session -> multi-turn send/receive -> result."""

import logging
from datetime import datetime

from client.orchestrator import OrchestratorClient
from controller.decide import decide_next_step
from controller.reviewer import review_session

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
) -> dict:
    """
    Run a full conversation scenario.

    Returns result dict with status, history, tool calls, raw events, etc.
    """
    history = []
    token_usage_turns = []
    token_totals = {"prompt_token_count": 0, "candidates_token_count": 0, "total_token_count": 0}
    user_input = prompt

    for turn in range(1, max_turns + 1):
        logger.info("[Turn %d/%d] User -> %s", turn, max_turns, user_input[:80])

        # Send message and get response
        turn_data = await client.send_message(session_id, user_input)

        agent_text = turn_data["agent"]
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

        # Ask controller what to do next (retry on failure)
        decision = None
        usage = None
        for ctrl_attempt in range(1, 4):
            try:
                decision, usage = decide_next_step(
                    history=[{"user": h["user"], "agent": h["agent"]} for h in history],
                    last_user_input=user_input,
                    agent_response=agent_text,
                    controller_instructions=controller_instructions,
                    steps=steps,
                )
                break
            except Exception as e:
                logger.error("[Turn %d/%d] Controller error (attempt %d/3): %s", turn, max_turns, ctrl_attempt, e)
                if ctrl_attempt >= 3:
                    logger.error("Controller failed after 3 attempts, forcing continue")
                    decision = {"verdict": "continue", "result": "pass", "reason": f"Controller error: {e}", "next_user_input": "請繼續"}
                    break

        if usage:
            token_usage_turns.append(usage)
            for k in ("prompt_token_count", "candidates_token_count", "total_token_count"):
                token_totals[k] += usage.get(k, 0)

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
            await _run_review(result, review_instructions, responses)
            return result

        next_input = decision.get("next_user_input")
        if not next_input:
            next_input = "Please continue."
            logger.warning("[Turn %d/%d] Empty next_user_input, using fallback", turn, max_turns)
        user_input = next_input

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
    await _run_review(result, review_instructions, responses)
    return result


async def _run_review(result: dict, review_instructions: str | None, responses: list[dict] | None) -> None:
    """Run session-level review if instructions and responses are provided."""
    if not review_instructions or not responses:
        return
    logger.info("Running session review (%d response metrics)...", len(responses))
    review = await review_session(
        history=result["history"],
        review_instructions=review_instructions,
        responses=responses,
    )
    result["review"] = review
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
