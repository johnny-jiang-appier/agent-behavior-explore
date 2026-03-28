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


def _load_completed_scenarios() -> set[str]:
    """Scan test_results/ for scenarios with status=completed."""
    completed = set()
    if not RESULTS_DIR.exists():
        return completed
    for d in RESULTS_DIR.iterdir():
        if not d.is_dir():
            continue
        result_file = d / "result.json"
        if not result_file.exists():
            continue
        try:
            result = json.loads(result_file.read_text(encoding="utf-8"))
            if result.get("status") == "completed":
                completed.add(result.get("scenario_name", ""))
        except (json.JSONDecodeError, OSError):
            continue
    return completed


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


def _identify_retryable(scenarios: list[dict], results: list) -> list[tuple[int, dict]]:
    """Return (index, scenario) pairs for results that should be retried."""
    retryable = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            retryable.append((i, scenarios[i]))
        elif isinstance(result, dict) and result.get("status") == "failed":
            retryable.append((i, scenarios[i]))
    return retryable


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


async def retry_failed(
    scenarios: list[dict],
    results: list,
    max_retries: int,
    parallel: int,
    jwt_tokens: list[str],
    state: DashboardState | None = None,
    mode: str = "orchestrator",
) -> list:
    """Retry failed/error scenarios up to max_retries times."""
    cfg = get_config()
    sem = asyncio.Semaphore(parallel)

    for attempt in range(1, max_retries + 1):
        retryable = _identify_retryable(scenarios, results)
        if not retryable:
            break

        names = [s["name"] for _, s in retryable]
        logger.info("Retry round %d/%d: %d scenario(s) — %s", attempt, max_retries, len(retryable), ", ".join(names))

        async def run_retry(idx, scenario, jwt_token):
            # Reset dashboard state for this scenario
            if state and scenario["name"] in state.scenarios:
                s = state.scenarios[scenario["name"]]
                s.status = ScenarioStatus.PENDING
                s.turn = 0
                s.start_time = None
                s.end_time = None
                s.detail = f"retry {attempt}"
                s.review_done = 0
                s.review_total = 0
                s.review_parts = []
                s.retry_attempt = attempt

            async with sem:
                cb = make_progress_callback(state, scenario["name"]) if state else None
                try:
                    result = await run_one(scenario, cfg, jwt_token, progress_cb=cb, mode=mode)
                    return (idx, result)
                except Exception as e:
                    return (idx, e)

        tasks = [run_retry(idx, scenario, jwt_tokens[idx % len(jwt_tokens)]) for idx, scenario in retryable]
        retry_results = await asyncio.gather(*tasks)

        for idx, result in retry_results:
            results[idx] = result

    return results


async def run_with_dashboard(scenarios: list[dict], parallel: int, jwt_tokens: list[str], mode: str = "orchestrator", max_retries: int = 0) -> list[dict]:
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
        with Live(state, console=console, refresh_per_second=4, screen=True):
            results = await run_all(scenarios, parallel, jwt_tokens, state, mode=mode)
            if max_retries > 0:
                results = await retry_failed(scenarios, results, max_retries, parallel, jwt_tokens, state, mode)
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
    parser.add_argument("--retry", type=int, default=0, help="Max retry attempts per failed scenario (0=no retry)")
    parser.add_argument("--resume", action="store_true", help="Skip scenarios already completed in test_results/")
    args = parser.parse_args()

    if args.resume and args.clean:
        parser.error("--resume and --clean are mutually exclusive")

    # Clean old results
    if args.clean and RESULTS_DIR.exists():
        shutil.rmtree(RESULTS_DIR)
        logger.info("Cleaned %s", RESULTS_DIR)

    scenarios = load_scenarios(args.scenarios, args.k)
    if not scenarios:
        logger.error("No scenarios found")
        return

    # Resume: skip already-completed scenarios
    if args.resume:
        completed = _load_completed_scenarios()
        before = len(scenarios)
        scenarios = [s for s in scenarios if s["name"] not in completed]
        skipped = before - len(scenarios)
        if skipped:
            logger.info("Resume: skipping %d completed, running %d remaining", skipped, len(scenarios))
        if not scenarios:
            logger.info("All scenarios already completed")
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
        results = asyncio.run(run_with_dashboard(scenarios, args.parallel, jwt_tokens, mode=args.mode, max_retries=args.retry))
    else:
        async def _run():
            r = await run_all(scenarios, args.parallel, jwt_tokens, mode=args.mode)
            if args.retry > 0:
                r = await retry_failed(scenarios, r, args.retry, args.parallel, jwt_tokens, mode=args.mode)
            return r
        results = asyncio.run(_run())

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
