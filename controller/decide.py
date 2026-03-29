"""Controller decision logic with steps support."""

import json
import logging

from controller.llm import generate_json

logger = logging.getLogger(__name__)

_BASE_SYSTEM_PROMPT = """You are testing an AI agent. You simulate the USER and control the test flow.

Your responsibilities:
1. Evaluate the agent's last response.
2. Decide the next user input.
3. Decide whether to stop the test.

WHEN TO STOP (verdict=stop):
- result=pass: The agent has COMPLETED the entire task — it presented a final result, campaign summary, or confirmation that everything is done. The user has nothing more to do.
- result=fail: ONLY after 3+ consecutive turns of the same unrecoverable issue (stuck loop, repeated identical error, system failure). A single flow deviation is NOT a reason to stop.

WHEN TO CONTINUE (verdict=continue):
- The agent is asking questions, presenting options, or waiting for user input.
- The agent just performed tool calls and is presenting intermediate results.
- The agent is still in the middle of a multi-step workflow.
- Do NOT stop just because the agent completed ONE step — continue until the ENTIRE task is finished.
- If the instructions say to "remind" or "ask" the agent about something, CONTINUE and send the reminder as next_user_input. Do NOT stop.
- If you think the agent skipped a step, check the tool_calls list first — the agent may have called the tool even if the response text doesn't mention it explicitly.

TOOL CALLS:
- Each turn in the conversation history includes a "tool_calls" field — a list of tool names the agent called that turn.
- Use this to verify whether the agent actually performed expected actions before judging that a step was skipped.

Rules:
- You are the user, not the agent.
- Do NOT answer the question yourself.
- Be realistic.
- Output ONLY valid JSON.

JSON format:
{
  "verdict": "continue" | "stop",
  "result": "pass" | "fail",
  "reason": "why",
  "next_user_input": "string or null"
}
"""


def _build_system_prompt(
    controller_instructions: str | None = None,
    steps: list[dict] | None = None,
) -> str:
    prompt = _BASE_SYSTEM_PROMPT

    if controller_instructions:
        prompt += f"\n\nScenario context and instructions (PRIMARY — follow these):\n{controller_instructions.strip()}\n"

    if steps:
        prompt += "\n\nCritical decision points (use these EXACT replies when the situation matches):\n"
        for step in steps:
            prompt += f"- When {step['when']} → reply \"{step['reply']}\"\n"

    return prompt


def decide_next_step(
    history: list[dict],
    last_user_input: str,
    agent_response: str,
    controller_instructions: str | None = None,
    steps: list[dict] | None = None,
) -> tuple[dict, dict | None]:
    """Ask the controller LLM what to do next."""
    system_prompt = _build_system_prompt(controller_instructions, steps)
    user_prompt = f"""
Conversation so far:
{json.dumps(history, indent=2, ensure_ascii=False)}

Last user input:
{last_user_input}

Agent response:
{agent_response}
"""
    decision, usage = generate_json(system_prompt, user_prompt)
    _sanitize(decision)
    return decision, usage


def _sanitize(decision: dict) -> None:
    for key in ("verdict", "result", "reason", "next_user_input"):
        val = decision.get(key)
        if isinstance(val, str):
            decision[key] = " ".join(val.split())
