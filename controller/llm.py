"""LiteLLM provider for controller decisions."""

import json
import logging
import re

import litellm

from config import get_config

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_MAX_RETRIES = 3


def _parse_json(text: str) -> dict:
    """Parse JSON from LLM output, handling markdown blocks."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = _JSON_OBJECT_RE.search(text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not parse JSON from LLM response: {text[:200]}")


def generate_json(system_prompt: str, user_prompt: str) -> tuple[dict, dict | None]:
    """Send prompt to LLM and return (parsed_json, token_usage)."""
    cfg = get_config()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    last_err = None
    for attempt in range(1, _MAX_RETRIES + 1):
        kwargs = {
            "model": cfg.litellm_model,
            "messages": messages,
            "temperature": 0,
        }
        if cfg.litellm_api_key:
            kwargs["api_key"] = cfg.litellm_api_key
        if cfg.litellm_api_base:
            kwargs["api_base"] = cfg.litellm_api_base

        response = litellm.completion(**kwargs)
        text = response.choices[0].message.content or ""

        try:
            decision = _parse_json(text)
            usage = _extract_usage(response)
            return decision, usage
        except ValueError as e:
            last_err = e
            logger.warning("LLM attempt %d/%d: non-JSON: %s", attempt, _MAX_RETRIES, text[:100])
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": "Respond with ONLY valid JSON."})

    raise RuntimeError(f"LLM failed to return valid JSON after {_MAX_RETRIES} attempts") from last_err


def _extract_usage(response) -> dict | None:
    usage = getattr(response, "usage", None)
    if not usage:
        return None
    return {
        "prompt_token_count": getattr(usage, "prompt_tokens", 0) or 0,
        "candidates_token_count": getattr(usage, "completion_tokens", 0) or 0,
        "total_token_count": getattr(usage, "total_tokens", 0) or 0,
    }
