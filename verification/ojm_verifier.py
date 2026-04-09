"""OJM journey map verification via direct API calls.

After the agent calls finalizeJourneyMap or updateExistingJourneyMap,
this module walks the backend state via BFS and checks for:
- Dangling transition references (node GET returns 500/404)
- Wrapped creative issues on SMS/WebPush nodes (aq_sms/aq_webpush keys)
"""

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15.0
BFS_TIMEOUT = 60.0


def _extract_transition_targets(node: dict[str, Any]) -> list[str]:
    """Extract all transition target node IDs from an OJM node payload."""
    targets = []
    for a in node.get("actions", []) or []:
        if a.get("type") == "transition":
            targets.append(a["value"])
    for t in node.get("trigger", []) or []:
        for a in t.get("actions", []) or []:
            if a.get("type") == "transition":
                targets.append(a["value"])
    for a in (node.get("expire", {}) or {}).get("actions", []) or []:
        if a.get("type") == "transition":
            targets.append(a["value"])
    for s in node.get("scenario", []) or []:
        for a in s.get("actions", []) or []:
            if a.get("type") == "transition":
                targets.append(a["value"])
    return [t for t in targets if t.upper() != "EXIT"]


def _check_creative(node: dict[str, Any]) -> str | None:
    """Check if a send_message node has incorrectly wrapped creative."""
    if node.get("type") != "send_message":
        return None
    creative = node.get("creative", {})
    if not creative:
        return None
    platform = node.get("platform", "")
    # AppPush always uses aq_apppush_common wrapper — that's correct
    if platform == "aq_apppush":
        return None
    # SMS/WebPush should have FLAT creative (message, title, body, etc.)
    # If wrapped under aq_sms or aq_webpush, it's Bug B
    if "aq_sms" in creative:
        return f"SMS node {node.get('id', '?')}: creative wrapped under aq_sms"
    if "aq_webpush" in creative:
        return f"WebPush node {node.get('id', '?')}: creative wrapped under aq_webpush"
    return None


async def verify_journey_map(
    api_host: str, token: str, journey_map_id: str
) -> dict[str, Any]:
    """Verify a journey map's backend state via BFS walk.

    Args:
        api_host: OJM API base URL (e.g. https://api-console-dev.appier.info)
        token: Bearer JWT token
        journey_map_id: Journey map ID to verify

    Returns:
        Dict with keys:
            - status: "pass" or "fail"
            - nodes_checked: number of nodes successfully retrieved
            - errors: list of error strings
            - dangling_refs: list of {parent_id, target_id, http_code}
            - creative_issues: list of issue description strings
    """

    async def _bfs(client: httpx.AsyncClient) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        errors: list[str] = []
        dangling_refs: list[dict[str, Any]] = []
        creative_issues: list[str] = []
        nodes_checked = 0

        # GET map metadata
        map_resp = await client.get(
            f"{api_host}/api/journeyMap/{journey_map_id}/LATEST",
            headers=headers,
        )
        if map_resp.status_code != 200:
            errors.append(
                f"GET map {journey_map_id} returned {map_resp.status_code}"
            )
            return {
                "status": "fail",
                "nodes_checked": 0,
                "errors": errors,
                "dangling_refs": dangling_refs,
                "creative_issues": creative_issues,
            }

        trigger_id = map_resp.json().get("triggerNodeId", "")
        if not trigger_id:
            errors.append("Map has no triggerNodeId")
            return {
                "status": "fail",
                "nodes_checked": 0,
                "errors": errors,
                "dangling_refs": dangling_refs,
                "creative_issues": creative_issues,
            }

        # BFS walk
        queue: list[tuple[str, str | None]] = [(trigger_id, None)]
        visited: set[str] = set()

        while queue:
            nid, parent_id = queue.pop(0)
            if nid in visited or nid.upper() == "EXIT":
                continue
            visited.add(nid)

            resp = await client.get(
                f"{api_host}/api/journeyNode/{nid}/LATEST",
                headers=headers,
            )
            if resp.status_code != 200:
                dangling_refs.append({
                    "parent_id": parent_id,
                    "target_id": nid,
                    "http_code": resp.status_code,
                })
                logger.warning(
                    "Dangling ref: %s -> %s (HTTP %d)",
                    parent_id, nid, resp.status_code,
                )
                continue

            node = resp.json()
            nodes_checked += 1

            issue = _check_creative(node)
            if issue:
                creative_issues.append(issue)

            for target in _extract_transition_targets(node):
                if target not in visited:
                    queue.append((target, nid))

        status = "fail" if dangling_refs or creative_issues else "pass"
        return {
            "status": status,
            "nodes_checked": nodes_checked,
            "errors": errors,
            "dangling_refs": dangling_refs,
            "creative_issues": creative_issues,
        }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            return await asyncio.wait_for(_bfs(client), timeout=BFS_TIMEOUT)
    except asyncio.TimeoutError:
        return {
            "status": "fail",
            "nodes_checked": 0,
            "errors": [f"BFS timed out after {BFS_TIMEOUT}s"],
            "dangling_refs": [],
            "creative_issues": [],
        }
    except Exception as e:
        return {
            "status": "fail",
            "nodes_checked": 0,
            "errors": [f"Verification error: {e}"],
            "dangling_refs": [],
            "creative_issues": [],
        }
