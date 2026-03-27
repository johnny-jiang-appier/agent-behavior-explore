"""CLI entry point: run test scenarios against orchestrator API."""

import argparse
import asyncio
import json
import logging
from pathlib import Path

import yaml

from auth.jwt_manager import get_jwt
from client.orchestrator import OrchestratorClient
from config import get_config
from runner import run_scenario

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(asctime)s [%(filename)s:%(lineno)d] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).parent / "test_results"


def load_scenarios(path: str, filter_key: str | None = None) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    scenarios = [s for s in data.get("scenarios", []) if s.get("enabled", True)]
    if filter_key:
        scenarios = [s for s in scenarios if filter_key in s["name"]]
    logger.info("Loaded %d scenario(s)", len(scenarios))
    return scenarios


def save_result(result: dict) -> None:
    session_id = result.get("session_id")
    if not session_id:
        logger.warning("No session_id, skipping save")
        return
    out_dir = RESULTS_DIR / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "result.json"
    out_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Result saved: %s", out_file)


async def run_one(scenario: dict, cfg) -> dict:
    """Run a single scenario end-to-end."""
    name = scenario["name"]
    logger.info("=== Starting scenario: %s ===", name)

    jwt = get_jwt(cfg.use_real_jwt, cfg.user_id)

    client = OrchestratorClient(
        base_url=cfg.orchestrator_url,
        app_name=cfg.app_name,
        user_id=cfg.user_id,
        eam_project_id=cfg.eam_project_id,
        jwt=jwt,
        langfuse_project_id=cfg.langfuse_project_id,
    )

    session_id = await client.create_session()

    result = await run_scenario(
        client=client,
        session_id=session_id,
        prompt=scenario["prompt"].strip(),
        scenario_name=name,
        controller_instructions=scenario.get("controller_instructions"),
        steps=scenario.get("steps"),
        max_turns=scenario.get("max_turns", 30),
    )

    save_result(result)
    logger.info("=== Scenario %s: %s (%d turns) ===", name, result["status"], result["turns"])
    return result


async def run_all(scenarios: list[dict], parallel: int) -> list[dict]:
    """Run scenarios with concurrency limit."""
    cfg = get_config()
    sem = asyncio.Semaphore(parallel)

    async def run_with_sem(scenario):
        async with sem:
            return await run_one(scenario, cfg)

    tasks = [run_with_sem(s) for s in scenarios]
    return await asyncio.gather(*tasks, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description="Run campaign agent E2E tests via API")
    parser.add_argument("--scenarios", default="scenarios.yaml", help="Path to scenarios YAML")
    parser.add_argument("--parallel", "-n", type=int, default=1, help="Number of parallel sessions")
    parser.add_argument("-k", type=str, default=None, help="Filter scenarios by name substring")
    args = parser.parse_args()

    scenarios = load_scenarios(args.scenarios, args.k)
    if not scenarios:
        logger.error("No scenarios found")
        return

    results = asyncio.run(run_all(scenarios, args.parallel))

    # Summary
    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    for r in results:
        if isinstance(r, Exception):
            print(f"  ERROR: {r}")
        else:
            print(f"  {r['scenario_name']:40s} {r['status']:20s} turns={r['turns']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
