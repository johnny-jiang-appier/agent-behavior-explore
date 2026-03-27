"""CLI entry point: run test scenarios against orchestrator API."""

import argparse
import asyncio
import json
import logging
import shutil
from pathlib import Path

import yaml
from rich.console import Console
from rich.live import Live

from auth.jwt_manager import get_jwt
from client.a2a import A2AClient
from client.orchestrator import OrchestratorClient
from config import get_config
from dashboard import DashboardState, ScenarioState, ScenarioStatus, make_progress_callback, render_dashboard
from runner import run_scenario
from summary import print_rich_summary

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(asctime)s [%(filename)s:%(lineno)d] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Suppress noisy third-party loggers
for _name in ("litellm", "LiteLLM", "LiteLLM Proxy", "LiteLLM Router", "httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.ERROR)

RESULTS_DIR = Path(__file__).parent / "test_results"

console = Console()


def load_scenarios(path: str, filter_key: str | None = None) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    scenarios = [s for s in data.get("scenarios", []) if s.get("enabled", True)]
    if filter_key:
        scenarios = [s for s in scenarios if filter_key in s["name"]]

    # Validate
    valid = []
    for i, s in enumerate(scenarios):
        missing = {"name", "prompt"} - s.keys()
        if missing:
            logger.error("Scenario #%d missing required fields: %s — skipping", i, missing)
            continue
        if s.get("review_instructions") and not s.get("responses"):
            logger.warning("Scenario '%s' has review_instructions but no responses — reviews will be skipped", s["name"])
        if s.get("responses") and not s.get("review_instructions"):
            logger.warning("Scenario '%s' has responses but no review_instructions — reviews will be skipped", s["name"])
        valid.append(s)

    logger.info("Loaded %d scenario(s)", len(valid))
    return valid


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


def _create_client(cfg, mode: str, jwt_token: str | None = None):
    """Create client based on mode. Returns (client, session_id_coroutine_or_str)."""
    if mode == "a2a":
        return A2AClient(
            base_url=cfg.campaign_agent_url,
            eam_project_id=cfg.eam_project_id,
            user_email=cfg.user_id,
            jwt=jwt_token,
        )
    return OrchestratorClient(
        base_url=cfg.orchestrator_url,
        app_name=cfg.app_name,
        user_id=cfg.user_id,
        eam_project_id=cfg.eam_project_id,
        jwt=jwt_token,
        langfuse_project_id=cfg.langfuse_project_id,
    )


async def run_one(scenario: dict, cfg, jwt_token: str | None, progress_cb=None, mode: str = "orchestrator") -> dict:
    """Run a single scenario end-to-end."""
    name = scenario["name"]
    if not progress_cb:
        logger.info("=== Starting scenario: %s ===", name)

    client = _create_client(cfg, mode, jwt_token)

    try:
        # A2A create_session is sync; orchestrator is async
        if mode == "a2a":
            session_id = client.create_session()
        else:
            session_id = await client.create_session()

        result = await run_scenario(
            client=client,
            session_id=session_id,
            prompt=scenario["prompt"].strip(),
            scenario_name=name,
            controller_instructions=scenario.get("controller_instructions"),
            steps=scenario.get("steps"),
            review_instructions=scenario.get("review_instructions"),
            responses=scenario.get("responses"),
            max_turns=scenario.get("max_turns", 30),
            progress_cb=progress_cb,
        )

        save_result(result)
        if not progress_cb:
            logger.info("=== Scenario %s: %s (%d turns) ===", name, result["status"], result["turns"])
        return result

    except Exception as e:
        if progress_cb:
            progress_cb(status=ScenarioStatus.ERROR, detail=f"Error: {e}")
        raise


async def run_all(scenarios: list[dict], parallel: int, jwt_tokens: list[str], state: DashboardState | None = None, mode: str = "orchestrator") -> list[dict]:
    """Run scenarios with concurrency limit."""
    cfg = get_config()
    sem = asyncio.Semaphore(parallel)

    async def run_with_sem(scenario, jwt_token):
        async with sem:
            cb = make_progress_callback(state, scenario["name"]) if state else None
            return await run_one(scenario, cfg, jwt_token, progress_cb=cb, mode=mode)

    tasks = [run_with_sem(s, jwt_tokens[i % len(jwt_tokens)]) for i, s in enumerate(scenarios)]
    return await asyncio.gather(*tasks, return_exceptions=True)


async def run_with_dashboard(scenarios: list[dict], parallel: int, jwt_tokens: list[str], mode: str = "orchestrator") -> list[dict]:
    """Run scenarios with Rich live dashboard."""
    state = DashboardState(parallel=parallel)
    for s in scenarios:
        state.scenarios[s["name"]] = ScenarioState(
            name=s["name"],
            max_turns=s.get("max_turns", 30),
        )

    # Suppress info logging during live display — dashboard covers it
    prev_level = logging.root.level
    logging.root.setLevel(logging.WARNING)

    try:
        with Live(state, console=console, refresh_per_second=4, transient=True):
            results = await run_all(scenarios, parallel, jwt_tokens, state, mode=mode)
    finally:
        logging.root.setLevel(prev_level)

    return results


def main():
    parser = argparse.ArgumentParser(description="Run campaign agent E2E tests via API")
    parser.add_argument("--scenarios", default="scenarios.yaml", help="Path to scenarios YAML")
    parser.add_argument("--parallel", "-n", type=int, default=1, help="Number of parallel sessions")
    parser.add_argument("-k", type=str, default=None, help="Filter scenarios by name substring")
    parser.add_argument("--clean", action="store_true", help="Delete test_results/ before running")
    parser.add_argument("--no-dashboard", action="store_true", help="Disable live dashboard")
    parser.add_argument("--mode", choices=["orchestrator", "a2a"], default="orchestrator",
                        help="orchestrator (via /run_sse) or a2a (direct to campaign-agent)")
    args = parser.parse_args()

    # Clean old results
    if args.clean and RESULTS_DIR.exists():
        shutil.rmtree(RESULTS_DIR)
        logger.info("Cleaned %s", RESULTS_DIR)

    scenarios = load_scenarios(args.scenarios, args.k)
    if not scenarios:
        logger.error("No scenarios found")
        return

    # Get JWT(s) BEFORE entering asyncio loop (Playwright sync API conflicts with asyncio)
    cfg = get_config()
    num_tokens = min(args.parallel, len(scenarios))
    logger.info("Mode: %s | Fetching %d JWT token(s) (use_real=%s)...",
                args.mode, num_tokens, cfg.use_real_jwt)
    jwt_tokens = [get_jwt(cfg.use_real_jwt, cfg.user_id) for _ in range(num_tokens)]
    logger.info("JWT tokens ready")

    # Run with or without dashboard
    use_dashboard = not args.no_dashboard and console.is_terminal
    if use_dashboard:
        results = asyncio.run(run_with_dashboard(scenarios, args.parallel, jwt_tokens, mode=args.mode))
    else:
        results = asyncio.run(run_all(scenarios, args.parallel, jwt_tokens, mode=args.mode))

    # Final summary
    clean_results = [r for r in results if not isinstance(r, Exception)]
    errors = [r for r in results if isinstance(r, Exception)]

    print_rich_summary(clean_results, console=console)

    if errors:
        console.print(f"\n[red bold]{len(errors)} scenario(s) raised exceptions:[/]")
        for e in errors:
            console.print(f"  [red]{e}[/]")


if __name__ == "__main__":
    main()
