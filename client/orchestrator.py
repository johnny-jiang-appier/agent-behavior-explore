"""Async HTTP client for orchestrator API."""

import json
import logging
from urllib.parse import quote

import httpx

from client.retry import retry_with_backoff
from client.sse_parser import extract_turn_data, parse_sse_line

logger = logging.getLogger(__name__)

SSE_TIMEOUT = 180.0  # seconds


class OrchestratorClient:
    """Client for orchestrator /run_sse API."""

    def __init__(self, base_url: str, app_name: str, user_id: str, eam_project_id: str, jwt: str, langfuse_project_id: str):
        self.base_url = base_url.rstrip("/")
        self.app_name = app_name
        self.user_id = user_id
        self.eam_project_id = eam_project_id
        self.jwt = jwt
        self.langfuse_project_id = langfuse_project_id
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {jwt}",
        }

    async def create_session(self) -> str:
        """Create a new session and return the session ID."""
        encoded_user_id = f"{self.eam_project_id}::{self.user_id}" if self.eam_project_id else self.user_id
        url = f"{self.base_url}/api/adk/apps/{self.app_name}/users/{quote(encoded_user_id)}/sessions"
        body = {"user_id": encoded_user_id, "eam_project_id": self.eam_project_id}

        async def _do():
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, headers=self._headers, json=body, timeout=30.0)
                resp.raise_for_status()
                session_id = resp.json()["id"]
                logger.info("Session created: %s", session_id)
                return session_id

        return await retry_with_backoff(_do, label="create_session")

    async def send_message(self, session_id: str, text: str) -> dict:
        """
        Send a message via /run_sse and return parsed turn data.

        Returns:
            {agent, tool_calls, langfuse_trace_url, raw_events}
        """
        body = {
            "app_name": self.app_name,
            "user_id": self.user_id,
            "session_id": session_id,
            "new_message": {"parts": [{"text": text}], "role": "user"},
            "streaming": True,
            "eam_project_id": self.eam_project_id,
        }
        url = f"{self.base_url}/api/adk/run_sse"

        async def _do():
            events = []
            async with httpx.AsyncClient(timeout=httpx.Timeout(SSE_TIMEOUT, connect=10.0)) as client:
                async with client.stream("POST", url, headers=self._headers, json=body) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        event = parse_sse_line(line)
                        if event:
                            events.append(event)

            has_final = any(
                (e.get("customMetadata") or {}).get("is_final_response")
                for e in events
            )
            if not has_final and events:
                raise httpx.ReadTimeout("SSE stream incomplete, no is_final_response")

            return extract_turn_data(events, self.langfuse_project_id)

        return await retry_with_backoff(_do, label="send_message")
