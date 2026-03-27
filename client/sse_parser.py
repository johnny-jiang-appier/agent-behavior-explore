"""Parse SSE stream from orchestrator /run_sse into structured data."""

import json
import logging
import re

logger = logging.getLogger(__name__)

_LANGFUSE_PROJECT_RE = re.compile(r"/project/[^/]+/")


def parse_sse_line(line: str) -> dict | None:
    """Parse a single SSE line ('data: {...}') into a dict."""
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if line.startswith("data:"):
        payload = line[5:].strip()
        if payload in ("null", "[DONE]", ""):
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("Failed to parse SSE payload: %s", payload[:100])
            return None
    return None


def fix_langfuse_url(url: str, project_id: str) -> str:
    """Replace the project ID in a Langfuse URL."""
    return _LANGFUSE_PROJECT_RE.sub(f"/project/{project_id}/", url)


def extract_turn_data(events: list[dict], langfuse_project_id: str) -> dict:
    """
    Extract structured data from a list of SSE events for one turn.

    Returns:
        {
            "agent": str,              # Final agent text response
            "tool_calls": list[dict],  # [{name, args, response}, ...]
            "langfuse_trace_url": str | None,
            "raw_events": list[dict],  # All events as-is
        }
    """
    agent_text_parts = []
    tool_calls = {}  # keyed by call_id
    langfuse_trace_url = None

    for event in events:
        content = event.get("content") or {}
        parts = content.get("parts") or []
        custom_meta = event.get("customMetadata") or {}

        # Langfuse trace URL (usually in final event)
        trace_url = custom_meta.get("langfuse_trace_url")
        if trace_url:
            langfuse_trace_url = fix_langfuse_url(trace_url, langfuse_project_id)

        for part in parts:
            # Agent text (non-partial, model role)
            if "text" in part and content.get("role") == "model" and not event.get("partial"):
                agent_text_parts.append(part["text"])

            # Tool call
            fc = part.get("functionCall")
            if fc:
                call_id = fc.get("id", "")
                tool_calls[call_id] = {
                    "name": fc.get("name"),
                    "args": fc.get("args", {}),
                    "response": None,
                }

            # Tool response
            fr = part.get("functionResponse")
            if fr:
                call_id = fr.get("id", "")
                resp = fr.get("response", {})
                if call_id in tool_calls:
                    tool_calls[call_id]["response"] = resp
                else:
                    tool_calls[call_id] = {
                        "name": fr.get("name"),
                        "args": {},
                        "response": resp,
                    }

    # Also collect text from a2a artifacts
    for event in events:
        custom_meta = event.get("customMetadata") or {}
        a2a_resp = custom_meta.get("a2a:response") or {}
        for artifact in a2a_resp.get("artifacts") or []:
            for p in artifact.get("parts") or []:
                if isinstance(p, dict) and p.get("text"):
                    if p["text"] not in agent_text_parts:
                        agent_text_parts.append(p["text"])

    return {
        "agent": "\n".join(agent_text_parts) if agent_text_parts else "",
        "tool_calls": list(tool_calls.values()),
        "langfuse_trace_url": langfuse_trace_url,
        "raw_events": events,
    }
