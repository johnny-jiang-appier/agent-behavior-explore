"""Summarize all test results into a table and optional CSV export."""

import argparse
import csv
import json
import logging
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).parent / "test_results"


def load_results(results_dir: Path = RESULTS_DIR) -> list[dict]:
    results = []
    for d in sorted(results_dir.iterdir()):
        if not d.is_dir():
            continue
        result_file = d / "result.json"
        if result_file.exists():
            results.append(json.loads(result_file.read_text(encoding="utf-8")))
    return results


def print_summary(results: list[dict]) -> None:
    if not results:
        print("No results found.")
        return

    print(f"\n{'='*80}")
    print(f"{'Scenario':<40} {'Status':<15} {'Turns':<6} {'Review Scores'}")
    print(f"{'='*80}")
    for r in results:
        name = r.get("scenario_name", "unknown")[:39]
        status = r.get("status", "?")
        turns = r.get("turns", 0)
        review = r.get("review", {})
        scores = review.get("scores", {})
        if scores:
            score_str = " ".join(f"{k}={v}" for k, v in scores.items())
        else:
            score_str = "-"
        print(f"  {name:<40} {status:<15} {turns:<6} {score_str}")
    print(f"{'='*80}")

    # Summary stats
    total = len(results)
    completed = sum(1 for r in results if r.get("status") == "completed")
    failed = sum(1 for r in results if r.get("status") == "failed")
    errors = sum(1 for r in results if r.get("status") == "error")
    print(f"  Total: {total} | Completed: {completed} | Failed: {failed} | Errors: {errors}")
    print()


def export_csv(results: list[dict], output_path: str) -> None:
    if not results:
        return

    # Collect all review score keys
    all_score_keys = set()
    for r in results:
        scores = r.get("review", {}).get("scores", {})
        all_score_keys.update(scores.keys())
    all_score_keys = sorted(all_score_keys)

    fieldnames = ["scenario_name", "session_id", "status", "turns", "reason"] + all_score_keys + ["review_detail"]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            review = r.get("review", {})
            scores = review.get("scores", {})
            row = {
                "scenario_name": r.get("scenario_name", ""),
                "session_id": r.get("session_id", ""),
                "status": r.get("status", ""),
                "turns": r.get("turns", 0),
                "reason": r.get("reason", ""),
                "review_detail": review.get("review_detail", ""),
            }
            for k in all_score_keys:
                row[k] = scores.get(k, "")
            writer.writerow(row)

    logger.info("CSV exported to: %s", output_path)


_STATUS_STYLES = {
    "completed": "green",
    "failed": "red",
    "error": "red bold",
    "max_turns_reached": "yellow",
}


def print_rich_summary(results: list[dict], console: Console | None = None) -> None:
    """Print a Rich-formatted summary table with review scores."""
    console = console or Console()
    if not results:
        console.print("[dim]No results found.[/]")
        return

    # Collect all score keys
    all_score_keys: list[str] = []
    seen = set()
    for r in results:
        for k in r.get("review", {}).get("scores", {}):
            if k not in seen:
                all_score_keys.append(k)
                seen.add(k)

    # Stats
    total = len(results)
    completed = sum(1 for r in results if r.get("status") == "completed")
    failed = sum(1 for r in results if r.get("status") == "failed")
    errors = sum(1 for r in results if r.get("status") in ("error", "max_turns_reached"))

    title = (
        f"Test Results  "
        f"[bold]{completed}[/]/{total} passed "
        f"\u00b7 [red]{failed}[/] failed "
        f"\u00b7 [yellow]{errors}[/] errors"
    )

    table = Table(title=title, show_edge=True, pad_edge=True)
    table.add_column("Scenario", style="bold", no_wrap=True)
    table.add_column("Session", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Turns", justify="right")
    for key in all_score_keys:
        table.add_column(key, justify="center", no_wrap=True)

    for r in results:
        status = r.get("status", "?")
        style = _STATUS_STYLES.get(status, "")
        sid = r.get("session_id", "-")
        sid_short = sid[:8] if len(sid) > 8 else sid
        scores = r.get("review", {}).get("scores", {})
        score_cells = []
        for k in all_score_keys:
            v = scores.get(k)
            if v == 1:
                score_cells.append(Text("\u2713", style="green"))
            elif v == 0:
                score_cells.append(Text("\u2717", style="red"))
            else:
                score_cells.append(Text("-", style="dim"))

        table.add_row(
            r.get("scenario_name", "?"),
            Text(sid_short, style="dim"),
            Text(status, style=style),
            str(r.get("turns", 0)),
            *score_cells,
        )

    console.print()
    console.print(table)
    console.print()


def main():
    parser = argparse.ArgumentParser(description="Summarize test results")
    parser.add_argument("--csv", type=str, default=None, help="Export to CSV file")
    parser.add_argument("--dir", type=str, default=None, help="Results directory")
    args = parser.parse_args()

    results_dir = Path(args.dir) if args.dir else RESULTS_DIR
    results = load_results(results_dir)
    print_rich_summary(results)

    if args.csv:
        export_csv(results, args.csv)


if __name__ == "__main__":
    main()
