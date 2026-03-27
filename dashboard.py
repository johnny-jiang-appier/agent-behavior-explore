"""Rich live dashboard for monitoring parallel test execution."""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from time import monotonic

from rich.table import Table
from rich.text import Text


class ScenarioStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    REVIEWING = "reviewing"
    DONE = "done"
    ERROR = "error"


_STATUS_DISPLAY: dict[ScenarioStatus, tuple[str, str]] = {
    ScenarioStatus.PENDING: ("\u25cb", "dim"),          # ○
    ScenarioStatus.RUNNING: ("\u25cf", "cyan"),          # ●
    ScenarioStatus.REVIEWING: ("\u27f3", "yellow"),      # ⟳
    ScenarioStatus.DONE: ("\u2713", "green"),            # ✓
    ScenarioStatus.ERROR: ("\u2717", "red"),             # ✗
}


@dataclass
class ScenarioState:
    name: str
    status: ScenarioStatus = ScenarioStatus.PENDING
    turn: int = 0
    max_turns: int = 30
    start_time: float | None = None
    end_time: float | None = None
    detail: str = ""
    review_done: int = 0
    review_total: int = 0
    review_parts: list[str] = field(default_factory=list)

    @property
    def elapsed(self) -> float | None:
        if self.start_time is None:
            return None
        end = self.end_time if self.end_time is not None else monotonic()
        return end - self.start_time


@dataclass
class DashboardState:
    scenarios: dict[str, ScenarioState] = field(default_factory=dict)
    parallel: int = 1

    def __rich__(self) -> "Table":
        """Called by Rich Live on every refresh to get the latest table."""
        return render_dashboard(self)


def make_progress_callback(state: DashboardState, scenario_name: str) -> Callable[..., None]:
    """Create a callback that updates a specific scenario's state."""

    def callback(
        *,
        status: ScenarioStatus | None = None,
        turn: int | None = None,
        max_turns: int | None = None,
        detail: str | None = None,
        review_done: int | None = None,
        review_total: int | None = None,
        review_part: str | None = None,
    ) -> None:
        s = state.scenarios[scenario_name]
        if status is not None:
            s.status = status
            if status == ScenarioStatus.RUNNING and s.start_time is None:
                s.start_time = monotonic()
            if status in (ScenarioStatus.DONE, ScenarioStatus.ERROR):
                s.end_time = monotonic()
        if turn is not None:
            s.turn = turn
        if max_turns is not None:
            s.max_turns = max_turns
        if detail is not None:
            s.detail = detail
        if review_done is not None:
            s.review_done = review_done
        if review_total is not None:
            s.review_total = review_total
        if review_part is not None:
            s.review_parts.append(review_part)

    return callback


def render_dashboard(state: DashboardState) -> Table:
    """Build a Rich Table representing current dashboard state."""
    scenarios = list(state.scenarios.values())
    done = sum(1 for s in scenarios if s.status == ScenarioStatus.DONE)
    errors = sum(1 for s in scenarios if s.status == ScenarioStatus.ERROR)
    running = sum(1 for s in scenarios if s.status in (ScenarioStatus.RUNNING, ScenarioStatus.REVIEWING))
    pending = sum(1 for s in scenarios if s.status == ScenarioStatus.PENDING)
    total = len(scenarios)

    title = (
        f" Campaign Agent E2E Tests  "
        f"[bold]{done + errors}[/bold]/{total} completed "
        f"\u00b7 [red]{errors}[/red] errors "
        f"\u00b7 [cyan]{running}[/cyan] running "
        f"\u00b7 [dim]{pending}[/dim] pending "
    )

    table = Table(title=title, expand=True, show_edge=True, pad_edge=True)
    table.add_column("Scenario", width=28, no_wrap=True)
    table.add_column("Status", width=14, no_wrap=True)
    table.add_column("Turn", width=8, justify="right", no_wrap=True)
    table.add_column("Elapsed", width=8, justify="right", no_wrap=True)
    table.add_column("Detail", ratio=1, overflow="ellipsis")

    for s in scenarios:
        icon, style = _STATUS_DISPLAY[s.status]
        status_text = Text(f"{icon} {s.status.value}", style=style)

        if s.status == ScenarioStatus.PENDING:
            turn_text = "-"
            elapsed_text = "-"
        else:
            turn_text = f"{s.turn}/{s.max_turns}"
            elapsed_text = f"{s.elapsed:.0f}s" if s.elapsed is not None else "-"

        detail = _build_detail(s)
        table.add_row(s.name, status_text, turn_text, elapsed_text, detail)

    return table


def _build_detail(s: ScenarioState) -> str:
    """Build the detail string based on scenario status."""
    if s.status == ScenarioStatus.PENDING:
        return f"waiting for slot ({s.name})"
    if s.status == ScenarioStatus.REVIEWING:
        parts = " ".join(s.review_parts) if s.review_parts else ""
        return f"reviewing {s.review_done}/{s.review_total} metrics \u2014 {parts}".strip()
    if s.status == ScenarioStatus.ERROR:
        return s.detail
    # RUNNING or DONE — use the detail field directly
    return s.detail
