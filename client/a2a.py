"""Async HTTP client for campaign-agent A2A endpoint (JSON-RPC)."""

import json
import logging
import uuid

import httpx

from client.retry import retry_with_backoff

logger = logging.getLogger(__name__)

A2A_TIMEOUT = 180.0  # seconds


class A2AClient:
    """
    Client for campaign-agent A2A JSON-RPC endpoint.

    Caller manages conversation history. Each send_message() packs full history
    into message.parts with role metadata, matching the orchestrator's format:
    - Text:             metadata={"role": "user"|"model", "text": "..."}
    - FunctionCall:     metadata={"role": "model", "text": "FunctionCall(name=..., args=...)"}
    - FunctionResponse: metadata={"role": "model", "text": "FunctionResponse(name=..., response=...)"}
    """

    def __init__(
        self,
        base_url: str,
        eam_project_id: str,
        user_email: str,
        jwt: str | None = None,
        session_id: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.eam_project_id = eam_project_id
        self.user_email = user_email
        self.session_id = session_id or str(uuid.uuid4())
        # Each entry: {"role": str, "text": str, "author": str|None}
        # Full conversation chain including tool calls, in orchestrator-compatible format
        self._history_parts: list[dict] = []
        self._agent_name = "campaign_agent"
        self._headers = {
            "Content-Type": "application/json",
            "x-adk-session-eam-project-id": eam_project_id,
            "x-adk-session-user-email": user_email,
            "x-adk-session-id": self.session_id,
        }
        if jwt:
            self._headers["Authorization"] = f"Bearer {jwt}"

    def create_session(self) -> str:
        """Return session_id (no server call needed for A2A)."""
        logger.info("A2A session: %s", self.session_id)
        return self.session_id

    async def send_message(self, session_id: str, text: str) -> dict:
        """
        Send a message via A2A JSON-RPC and return parsed turn data.

        Returns same format as OrchestratorClient:
            {agent, tool_calls, langfuse_trace_url}
        """
        self._history_parts.append({"role": "user", "text": text})

        parts = []
        for h in self._history_parts:
            meta = {"role": h["role"], "text": h["text"]}
            if h.get("author"):
                meta["author"] = h["author"]
            parts.append({"kind": "text", "text": "", "metadata": meta})

        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": parts,
                    "messageId": str(uuid.uuid4()),
                }
            },
        }

        url = f"{self.base_url}/a2a"

        async def _do():
            async with httpx.AsyncClient(timeout=httpx.Timeout(A2A_TIMEOUT, connect=10.0)) as client:
                resp = await client.post(url, headers=self._headers, json=body)
                resp.raise_for_status()
                data = resp.json()

            if "error" in data:
                raise RuntimeError(f"A2A error: {data['error'].get('message', data['error'])}")

            result = data.get("result", {})
            turn_data = self._extract_turn_data(result)
            self._append_agent_history(result)
            return turn_data

        return await retry_with_backoff(_do, label="a2a_send_message")

    def _append_agent_history(self, result: dict) -> None:
        """
        Extract agent-side messages from A2A response history and append to _history_parts.

        Converts to orchestrator-compatible format:
        - Text:             {"role": "model", "text": "agent response"}
        - FunctionCall:     {"role": "model", "text": "FunctionCall(name=X, args=Y)"}
        - FunctionResponse: {"role": "model", "text": "FunctionResponse(name=X, response=Y)"}
        """
        added = 0
        for msg in result.get("history", []):
            for part in msg.get("parts", []):
                kind = part.get("kind", "")
                metadata = part.get("metadata", {})
                adk_type = metadata.get("adk_type", "")
                data = part.get("data", {})

                if adk_type == "function_call" and data:
                    self._history_parts.append({
                        "role": "model",
                        "author": self._agent_name,
                        "text": f"FunctionCall(name={data.get('name', '')}, args={data.get('args', {})})",
                    })
                    added += 1
                elif adk_type == "function_response" and data:
                    self._history_parts.append({
                        "role": "model",
                        "author": self._agent_name,
                        "text": f"FunctionResponse(name={data.get('name', '')}, response={data.get('response', {})})",
                    })
                    added += 1
                elif kind == "text" and part.get("text"):
                    self._history_parts.append({
                        "role": "model",
                        "author": self._agent_name,
                        "text": part["text"],
                    })
                    added += 1
        logger.info("History: +%d parts from %d msgs (total: %d)", added, len(result.get("history", [])), len(self._history_parts))

    def _extract_turn_data(self, result: dict) -> dict:
        """Extract agent text and tool calls from A2A result for the current turn."""
        # Agent text from artifacts
        agent_text = ""
        for artifact in result.get("artifacts", []):
            for part in artifact.get("parts", []):
                if part.get("kind") == "text" and part.get("text"):
                    agent_text += part["text"]

        # Tool calls from history
        tool_calls = []
        for msg in result.get("history", []):
            for part in msg.get("parts", []):
                metadata = part.get("metadata", {})
                adk_type = metadata.get("adk_type", "")
                data = part.get("data", {})

                if adk_type == "function_call" and data:
                    tool_calls.append({
                        "name": data.get("name", ""),
                        "args": data.get("args", {}),
                        "id": data.get("id", ""),
                    })
                elif adk_type == "function_response" and data:
                    call_id = data.get("id", "")
                    call_name = data.get("name", "")
                    response_data = data.get("response", {})
                    if isinstance(response_data, str):
                        try:
                            response_data = json.loads(response_data)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    for tc in tool_calls:
                        if (tc.get("id") == call_id or tc.get("name") == call_name) and "response" not in tc:
                            tc["response"] = response_data
                            break
                    else:
                        tool_calls.append({
                            "name": call_name,
                            "args": {},
                            "id": call_id,
                            "response": response_data,
                        })

        return {
            "agent": agent_text,
            "tool_calls": tool_calls,
            "langfuse_trace_url": None,
        }
