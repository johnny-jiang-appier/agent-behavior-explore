"""Async HTTP client for orchestrator API."""

import json
import logging
from urllib.parse import quote

import httpx

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
        # Use encoded user_id format: eam_project_id::email
        encoded_user_id = f"{self.eam_project_id}::{self.user_id}" if self.eam_project_id else self.user_id
        url = f"{self.base_url}/api/adk/apps/{self.app_name}/users/{quote(encoded_user_id)}/sessions"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=self._headers,
                json={"user_id": encoded_user_id, "eam_project_id": self.eam_project_id},
                timeout=30.0,
            )
            resp.raise_for_status()
            session_id = resp.json()["id"]
            logger.info("Session created: %s", session_id)
            return session_id

    async def send_message(self, session_id: str, text: str, max_retries: int = 3) -> dict:
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

        for attempt in range(1, max_retries + 1):
            try:
                events = []
                async with httpx.AsyncClient(timeout=httpx.Timeout(SSE_TIMEOUT, connect=10.0)) as client:
                    async with client.stream("POST", url, headers=self._headers, json=body) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            event = parse_sse_line(line)
                            if event:
                                events.append(event)

                # Check if we got a final response
                has_final = any(
                    (e.get("customMetadata") or {}).get("is_final_response")
                    for e in events
                )
                if not has_final and events:
                    logger.warning("SSE stream incomplete (attempt %d/%d), no is_final_response", attempt, max_retries)
                    if attempt < max_retries:
                        continue

                return extract_turn_data(events, self.langfuse_project_id)

            except (httpx.HTTPStatusError, httpx.ReadTimeout, httpx.ConnectError) as e:
                logger.error("send_message failed (attempt %d/%d): %s", attempt, max_retries, e)
                if attempt >= max_retries:
                    raise

        return extract_turn_data([], self.langfuse_project_id)
