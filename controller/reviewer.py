"""Session-level reviewer: evaluates entire conversation against response metrics."""

import asyncio
import json
import logging
from collections.abc import Callable

from config import get_config
from controller.llm import generate_json

_REVIEW_SEMAPHORE = asyncio.Semaphore(2)

logger = logging.getLogger(__name__)

_BASE_SYSTEM_PROMPT = """You are a QA reviewer evaluating a completed conversation between a USER and an AI AGENT.

You will be given:
1. Review instructions describing the evaluation context
2. The full conversation history (including tool call inputs/outputs)
3. ONE specific response metric to evaluate

Score the metric:
- 1 = the metric is satisfied
- 0 = the metric is NOT satisfied

Output ONLY valid JSON:
{
  "score": 1,
  "detail": "Brief explanation of why this score was given."
}
"""


def _review_single(
    history_json: str,
    review_instructions: str,
    response: dict,
    model: str | None = None,
) -> dict:
    """Review a single response metric (sync, for use in thread executor)."""
    system_prompt = (
        _BASE_SYSTEM_PROMPT
        + f"\nReview instructions:\n{review_instructions.strip()}\n"
    )

    user_prompt = f"""Response metric to evaluate:
- Name: {response['name']}
- Description: {response['description']}

Full conversation history:
{history_json}
"""

    try:
        result, usage = generate_json(system_prompt, user_prompt, model=model)
        return {
            "name": response["name"],
            "score": result.get("score", 0),
            "detail": result.get("detail", ""),
            "token_usage": usage,
        }
    except Exception as e:
        logger.error("Review failed for %s: %s", response["name"], e)
        return {
            "name": response["name"],
            "score": 0,
            "detail": f"Review failed: {e}",
            "token_usage": None,
        }


async def review_session(
    history: list[dict],
    review_instructions: str,
    responses: list[dict],
    progress_cb: Callable[..., None] | None = None,
) -> dict:
    """
    Review a completed conversation session. Each response metric is evaluated
    independently and in parallel.

    Args:
        history: List of turn dicts with user/agent/tool_calls
        review_instructions: Instructions for the reviewer (what to check)
        responses: List of response metrics, each with:
            - name: metric name (e.g., "scenario_id_binding")
            - description: what to check

    Returns:
        {
            "scores": {"res_name": 1, ...},
            "details": {"res_name": "explanation", ...},
            "review_detail": "combined summary",
            "token_usage": {...}
        }
    """
    if not history:
        return {
            "scores": {r["name"]: 0 for r in responses},
            "details": {r["name"]: "No conversation history" for r in responses},
            "review_detail": "No conversation history to review.",
            "token_usage": None,
        }

    # Build history once (shared across all reviews)
    history_for_review = []
    for turn in history:
        entry = {"user": turn["user"], "agent": turn["agent"]}
        if turn.get("tool_calls"):
            entry["tool_calls"] = turn["tool_calls"]
        history_for_review.append(entry)
    history_json = json.dumps(history_for_review, indent=2, ensure_ascii=False)

    # Run all reviews in parallel using thread executor (litellm is sync)
    # Semaphore limits concurrent review calls to avoid rate limiting
    loop = asyncio.get_event_loop()
    cfg = get_config()
    review_model = cfg.litellm_review_model
    _done_count = 0

    async def _review_with_progress(response: dict) -> dict:
        nonlocal _done_count
        async with _REVIEW_SEMAPHORE:
            result = await loop.run_in_executor(
                None, _review_single, history_json, review_instructions, response, review_model,
            )
        _done_count += 1
        if progress_cb:
            label = "\u2713" if result["score"] == 1 else "\u2717"
            progress_cb(
                review_done=_done_count,
                review_part=f"{result['name']}={label}",
            )
        return result

    results = await asyncio.gather(*[_review_with_progress(r) for r in responses])

    # Aggregate
    scores = {}
    details = {}
    total_usage = {"prompt_token_count": 0, "candidates_token_count": 0, "total_token_count": 0}
    for r in results:
        scores[r["name"]] = r["score"]
        details[r["name"]] = r["detail"]
        if r["token_usage"]:
            for k in total_usage:
                total_usage[k] += r["token_usage"].get(k, 0)

    review_detail = "\n".join(f"- {name}: {'PASS' if s == 1 else 'FAIL'} — {details[name]}" for name, s in scores.items())

    return {
        "scores": scores,
        "details": details,
        "review_detail": review_detail,
        "token_usage": total_usage,
    }
