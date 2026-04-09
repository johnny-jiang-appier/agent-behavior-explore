"""Minimal test: create a journey map and finalize it.

Usage:
    uv run scripts/test_finalize_jm.py              # default: adk mode
    uv run scripts/test_finalize_jm.py --mode adk
    uv run scripts/test_finalize_jm.py --mode orchestrator
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auth.jwt_manager import get_jwt
from client.adk import ADKClient
from client.orchestrator import OrchestratorClient
from config import get_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("test_finalize_jm")


MESSAGES = [
    "我想建立一個 journey map，用產品上市的模板，快速建立就好，不需要指定 segment，跳過。",
    "用產品上市那個模板",
    "先用預設的就好，之後再改",
    "確認，直接 finalize",
    "直接 finalize",
    "確認",
]


def print_turn(idx: int, role: str, text: str, tool_calls: list | None = None):
    print(f"\n{'='*60}")
    print(f"Turn {idx} [{role}]")
    print(f"{'='*60}")
    if text:
        print(text[:1000])
    if tool_calls:
        for tc in tool_calls:
            status = ""
            resp = tc.get("response", {})
            if isinstance(resp, dict):
                status = resp.get("status", "")
            print(f"  🔧 {tc['name']}  →  {status}")
            if tc["name"] == "finalizeJourneyMap":
                print(f"     args: {json.dumps(tc.get('args', {}), ensure_ascii=False)[:500]}")
                print(f"     response: {json.dumps(resp, ensure_ascii=False)[:1000]}")


def create_client(cfg, mode: str, jwt_token: str):
    if mode == "adk":
        return ADKClient(
            base_url=cfg.campaign_agent_url,
            eam_project_id=cfg.eam_project_id,
            user_email=cfg.user_id,
            orchestrator_url=cfg.orchestrator_url,
            app_name=cfg.app_name,
            jwt=jwt_token,
            artifact_origin=cfg.artifact_origin,
        )
    return OrchestratorClient(
        base_url=cfg.orchestrator_url,
        app_name=cfg.app_name,
        user_id=cfg.user_id,
        eam_project_id=cfg.eam_project_id,
        jwt=jwt_token,
        langfuse_project_id=cfg.langfuse_project_id,
    )


async def main(jwt_token: str, mode: str):
    cfg = get_config()
    logger.info("Mode: %s | JWT ready (real=%s, len=%d)", mode, cfg.use_real_jwt, len(jwt_token))

    client = create_client(cfg, mode, jwt_token)
    session_id = await client.create_session()
    logger.info("Session: %s", session_id)

    for i, msg in enumerate(MESSAGES):
        print_turn(i + 1, "USER", msg)

        try:
            turn = await client.send_message(session_id, msg)
        except Exception as e:
            logger.error("send_message failed: %s", e)
            break

        agent_text = turn.get("agent", "")
        tool_calls = turn.get("tool_calls", [])
        print_turn(i + 1, "AGENT", agent_text, tool_calls)

        # Check if finalizeJourneyMap was called
        for tc in tool_calls:
            if tc["name"] == "finalizeJourneyMap":
                resp = tc.get("response", {})
                if isinstance(resp, dict) and resp.get("status") == "error":
                    logger.error("❌ finalizeJourneyMap FAILED: %s", resp.get("message", ""))
                else:
                    logger.info("✅ finalizeJourneyMap succeeded")
                print(f"\n\nDone — finalizeJourneyMap reached ({mode} mode).")
                return

    print(f"\n\nDone — finalizeJourneyMap was NOT reached in {len(MESSAGES)} turns ({mode} mode).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["adk", "orchestrator"], default="adk")
    args = parser.parse_args()

    cfg = get_config()
    jwt_token = get_jwt(cfg.use_real_jwt, cfg.user_id)
    asyncio.run(main(jwt_token, args.mode))
