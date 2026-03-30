# agent-behavior-explore

Pure API-based E2E testing tool for AI agents. Uses an LLM controller to simulate multi-turn user conversations and evaluate agent behavior. Supports two communication modes:
- **Orchestrator mode** — SSE streaming via orchestrator (server manages conversation history)
- **A2A mode** — JSON-RPC directly to agent (client manages conversation history)

## Setup

```bash
# Install dependencies
uv venv --python 3.12
uv pip install -r requirements.txt --python .venv/bin/python

# Configure
cp .env.example .env
# Edit .env — fill in required values
```

### .env Configuration

```env
# Orchestrator (for orchestrator mode)
ORCHESTRATOR_URL=http://localhost:8888    # Orchestrator API base URL
APP_NAME=multi_agent                      # ADK app name

# Agent (for a2a mode)
CAMPAIGN_AGENT_URL=http://localhost:8777  # Agent API base URL

# User
USER_ID=your-email@example.com           # User email
EAM_PROJECT_ID=your-project-id           # EAM project ID

# JWT
USE_REAL_JWT=false                        # false=fake JWT, true=real JWT via Chrome

# LLM Controller
LITELLM_MODEL=your-model                  # LLM model for controller
LITELLM_REVIEW_MODEL=your-review-model    # LLM model for reviewer (can be different)
LITELLM_API_KEY=your-key-here             # API key
LITELLM_API_BASE=https://your-api-base    # API base URL

# Langfuse (optional)
LANGFUSE_PROJECT_ID=your-project-id       # For trace URL correction
```

### JWT Setup

**Fake JWT (`USE_REAL_JWT=false`)** — Auto-generated, works when orchestrator/agent skip signature verification. Good for local dev.

**Real JWT (`USE_REAL_JWT=true`)** — Each session opens headless Chrome to intercept a real JWT from the frontend. Requires a saved Chrome auth profile:

```bash
# First time: manual login to save profile
.venv/bin/python -m auth.refresh_jwt
# Opens Chrome → log in → close the tab → profile saved

# After this, real JWT is captured automatically (headless)
```

## Usage

### Two Modes

#### Orchestrator Mode (default)

Routes through the orchestrator → agent. The orchestrator manages session state and conversation history server-side.

```bash
.venv/bin/python run.py                # run all scenarios
.venv/bin/python run.py -n 3           # parallel (3 per mode)
.venv/bin/python run.py -k "sms"       # filter by scenario name
```

#### A2A Mode

Calls agent directly via A2A JSON-RPC protocol. Client manages conversation history per turn.

```bash
.venv/bin/python run.py --mode a2a
.venv/bin/python run.py --mode a2a -n 3 -k "sms"
```

#### Per-Scenario Mode

Each scenario can specify its own mode in `scenarios.yaml`:

```yaml
- name: run_01_edm_rec
  mode: orchestrator    # override CLI --mode
  prompt: ...

- name: run_02_line_rec
  mode: a2a
  prompt: ...
```

`-n N` creates **separate concurrency pools per mode** (N orchestrator + N a2a simultaneously).

#### When to Use Which

| | Orchestrator | A2A |
|---|---|---|
| Protocol | SSE streaming | JSON-RPC |
| History | Server-side | Client-side |
| Langfuse URLs | Extracted from SSE events | Not available |
| Use when | Testing full stack | Testing agent in isolation |

### Common Options

```bash
.venv/bin/python run.py --clean             # delete old results before running
.venv/bin/python run.py --retry 1           # auto-retry failed/error scenarios
.venv/bin/python run.py --resume            # skip already-completed scenarios
.venv/bin/python run.py --resume --retry 1  # typical re-run
.venv/bin/python run.py --no-dashboard      # disable live dashboard (for CI)
.venv/bin/python run.py --scenarios my.yaml # use different scenarios file
```

Note: `--resume` and `--clean` are mutually exclusive.

### Live Dashboard

When running in a terminal, a Rich live dashboard shows real-time progress:
- Overall progress (completed / errors / running / pending)
- Per-scenario status, current turn, elapsed time, and detail
- Retry attempts shown as `(retry 1)` in scenario name

### View Results

```bash
.venv/bin/python summary.py                # Rich summary table
.venv/bin/python summary.py --csv out.csv  # export to CSV
```

## Scenario Format

Scenarios are defined in `scenarios.yaml`:

```yaml
_definitions:
  responses: &all_responses
    - name: "metric_a"
      description: "Detailed description for LLM reviewer..."
    - name: "metric_b"
      description: "..."
  base_context: >
    Shared controller instructions prepended to all scenarios.
  base_review_instructions: >
    Shared review instructions prepended to all scenarios.

scenarios:
  - name: "test_sms_rec_happy_path"
    mode: a2a                        # optional: orchestrator (default) or a2a
    prompt: >
      First message sent to the agent.
    controller_instructions: >
      Instructions for the LLM controller.
    steps:
      - when: "agent asks about audience"
        reply: "Use All Users"
      - when: "agent asks about schedule"
        reply: "Tomorrow 10am"
    review_instructions: >
      Instructions for session-level reviewer.
    responses: *all_responses
    max_turns: 30
    enabled: true
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Scenario identifier, shown in results |
| `prompt` | Yes | First message sent to the agent |
| `mode` | No | `orchestrator` or `a2a` (default: CLI `--mode`) |
| `controller_instructions` | No | General direction for the LLM controller |
| `steps` | No | Key decision points — controller prioritizes these |
| `review_instructions` | No | Instructions for session-level reviewer |
| `responses` | No | Response metrics to score (requires `review_instructions`) |
| `max_turns` | No | Max conversation turns (default: 30) |
| `enabled` | No | Set `false` to skip (default: true) |

### Shared Definitions (`_definitions`)

| Key | Purpose |
|-----|---------|
| `responses` | Shared response metrics (use YAML anchor `&all_responses`) |
| `base_context` | Prepended to every scenario's `controller_instructions` |
| `base_review_instructions` | Prepended to every scenario's `review_instructions` |

### How `steps` Work

Steps define **when → reply** pairs for critical decision points. The controller checks steps first; if none match, it follows `controller_instructions` freely.

### How Review Works

After the conversation ends, each response metric is **independently and concurrently** evaluated by the LLM reviewer (max 3 concurrent). Each metric gets its own LLM call.

Scores: `1` = pass, `0` = fail. Results saved in `result.json` under `review`.

### Retry & Resume

All API calls use exponential backoff (10→30→60s) with a 180s final wait before giving up.

| Level | Scope | Behavior |
|-------|-------|----------|
| HTTP retry | Per turn | 4 attempts + 180s final wait (automatic) |
| Controller LLM | Per turn | 3 attempts + 180s final wait, then fallback |
| `--retry N` | Per scenario | Re-run failed scenarios with new session |
| `--resume` | Across runs | Skip scenarios already completed in `test_results/` |

## Result Structure

Each test produces `test_results/{session_id}/result.json`:

```json
{
  "session_id": "uuid",
  "scenario_name": "test_name",
  "status": "completed|failed|error|max_turns_reached",
  "reason": "...",
  "turns": 13,
  "history": [
    {
      "user": "user message",
      "agent": "agent reply",
      "langfuse_trace_url": "...",
      "tool_calls": [
        {"name": "toolName", "args": {}, "response": {}}
      ]
    }
  ],
  "review": {
    "scores": {"metric_a": 1, "metric_b": 0},
    "details": {"metric_a": "...", "metric_b": "..."}
  },
  "token_usage": {
    "by_turn": [...],
    "total": {"prompt_token_count": 0, "candidates_token_count": 0, "total_token_count": 0}
  }
}
```

## Architecture

```
run.py                    CLI entry point (--mode, -n, -k, --retry, --resume, --clean)
  │
  ├─ auth/jwt_manager.py  Get JWT (fake or real via headless Chrome)
  │
  ├─ client/
  │   ├─ orchestrator.py  Orchestrator mode: async SSE client
  │   ├─ a2a.py           A2A mode: JSON-RPC client
  │   ├─ retry.py         Shared retry-with-backoff helper
  │   └─ sse_parser.py    Parse SSE stream → agent text + tool calls
  │
  ├─ controller/
  │   ├─ llm.py           LiteLLM provider (JSON parsing + retry)
  │   ├─ decide.py        Controller decision logic (with steps support)
  │   └─ reviewer.py      Session-level reviewer (per-metric scoring)
  │
  ├─ dashboard.py          Rich live dashboard
  ├─ runner.py             Conversation loop (multi-turn send/receive → result)
  │
  └─ summary.py            Results aggregation (Rich table + CSV export)
```

## Prerequisites

- **Python 3.12+**
- **Agent** running (required for both modes)
- **Orchestrator** running (orchestrator mode only)
