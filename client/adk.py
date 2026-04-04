"""Async HTTP client for campaign-agent ADK A2A endpoint (JSON-RPC).

Uses orchestrator as artifact storage backend for journey map operations.
"""

import json
import logging
import uuid
from urllib.parse import quote

import httpx

from client.retry import retry_with_backoff

logger = logging.getLogger(__name__)

ADK_TIMEOUT = 180.0  # seconds


class ADKClient:
    """
    Client for campaign-agent ADK A2A JSON-RPC endpoint.

    Caller manages conversation history. Each send_message() packs full history
    into message.parts with role metadata, matching the orchestrator's format:
    - Text:             metadata={"role": "user"|"model", "text": "..."}
    - FunctionCall:     metadata={"role": "model", "text": "FunctionCall(name=..., args=...)"}
    - FunctionResponse: metadata={"role": "model", "text": "FunctionResponse(name=..., response=...)"}

    Orchestrator is used as artifact storage backend — the client creates a
    session on the orchestrator and passes artifact headers so campaign-agent
    tools (journey map, EDM creative) can read/write artifacts.
    """

    def __init__(
        self,
        base_url: str,
        eam_project_id: str,
        user_email: str,
        orchestrator_url: str,
        app_name: str = "multi_agent",
        jwt: str | None = None,
        session_id: str | None = None,
        artifact_origin: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.orchestrator_url = orchestrator_url.rstrip("/")
        # artifact_origin is the URL campaign-agent containers use to reach
        # orchestrator (e.g. http://host.docker.internal:8888)
        self.artifact_origin = (artifact_origin or orchestrator_url).rstrip("/")
        self.app_name = app_name
        self.eam_project_id = eam_project_id
        self.user_email = user_email
        self.session_id = session_id or str(uuid.uuid4())
        self._jwt = jwt
        # Each entry: {"role": str, "text": str, "author": str|None}
        self._history_parts: list[dict] = []
        self._agent_name = "campaign_agent"
        self._headers: dict[str, str] = {}

    async def create_session(self) -> str:
        """Create a session on orchestrator (for artifact storage) and return session_id."""
        encoded_user_id = f"{self.eam_project_id}::{self.user_email}"
        url = f"{self.orchestrator_url}/api/adk/apps/{self.app_name}/users/{quote(encoded_user_id)}/sessions"
        headers = {"Content-Type": "application/json"}
        if self._jwt:
            headers["Authorization"] = f"Bearer {self._jwt}"

        async def _do():
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    url, headers=headers,
                    json={"user_id": encoded_user_id, "eam_project_id": self.eam_project_id},
                    timeout=30.0,
                )
                resp.raise_for_status()
                self.session_id = resp.json()["id"]
                logger.info("ADK session created (via orchestrator): %s", self.session_id)
                return self.session_id

        session_id = await retry_with_backoff(_do, label="adk_create_session")

        # Build headers for all subsequent A2A requests
        user_id = f"{self.eam_project_id}::{self.user_email}"
        api_path = f"/api/adk/apps/{self.app_name}/users/{user_id}/sessions/{self.session_id}"
        self._headers = {
            "Content-Type": "application/json",
            "x-adk-session-eam-project-id": self.eam_project_id,
            "x-adk-session-user-email": self.user_email,
            "x-adk-session-id": self.session_id,
            # Artifact storage headers — point to orchestrator (as seen from campaign-agent container)
            "x-adk-session-api-origin": self.artifact_origin,
            "x-adk-session-api-path": api_path,
        }
        if self._jwt:
            self._headers["Authorization"] = f"Bearer {self._jwt}"

        return session_id

    async def send_message(self, session_id: str, text: str) -> dict:
        """
        Send a message via ADK A2A JSON-RPC and return parsed turn data.

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

        url = f"{self.base_url}/api/adk/a2a/multi_agent"

        async def _do():
            async with httpx.AsyncClient(timeout=httpx.Timeout(ADK_TIMEOUT, connect=10.0)) as client:
                resp = await client.post(url, headers=self._headers, json=body)
                resp.raise_for_status()
                data = resp.json()

            if "error" in data:
                raise RuntimeError(f"ADK A2A error: {data['error'].get('message', data['error'])}")

            result = data.get("result", {})
            turn_data = self._extract_turn_data(result)
            self._append_agent_history(result)
            return turn_data

        return await retry_with_backoff(_do, label="adk_send_message")

    def _append_agent_history(self, result: dict) -> None:
        """
        Extract agent-side messages from ADK A2A response history and append to _history_parts.

        Converts to orchestrator-compatible format:
        - Text:             {"role": "model", "text": "agent response"}
        - FunctionCall:     {"role": "model", "text": "FunctionCall(name=X, args=Y)"}
        - FunctionResponse: {"role": "model", "text": "FunctionResponse(name=X, response=Y)"}
        """
        added = 0
        for msg in result.get("history", []):
            # Skip user messages (only collect agent-side)
            if msg.get("role") == "user":
                continue
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
        """Extract agent text and tool calls from ADK A2A result for the current turn."""
        # Agent text from artifacts
        agent_text = ""
        for artifact in result.get("artifacts", []):
            for part in artifact.get("parts", []):
                if part.get("kind") == "text" and part.get("text"):
                    agent_text += part["text"]

        # Tool calls from history
        tool_calls = []
        for msg in result.get("history", []):
            if msg.get("role") == "user":
                continue
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
