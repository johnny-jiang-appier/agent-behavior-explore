# agent-test-api

Pure API-based E2E testing tool for campaign agent. Supports two modes:
- **Orchestrator mode** — sends HTTP requests to the orchestrator's `/run_sse` SSE endpoint (server manages conversation history)
- **A2A mode** — sends JSON-RPC requests directly to campaign-agent's `/a2a` endpoint (client manages conversation history)

Both modes use an LLM controller to simulate multi-turn user conversations, and store results with full tool call content.

## Setup

```bash
# Install dependencies
uv venv --python 3.12
uv pip install -r requirements.txt --python .venv/bin/python

# Configure
cp .env.example .env
# Edit .env — fill in LITELLM_API_KEY
```

### .env Configuration

```env
# Orchestrator (for orchestrator mode)
ORCHESTRATOR_URL=http://localhost:8888    # Orchestrator API base URL
APP_NAME=multi_agent                      # ADK app name

# Campaign Agent (for a2a mode)
CAMPAIGN_AGENT_URL=http://localhost:8777  # Campaign agent API base URL

# User
USER_ID=johnny.jiang@appier.com           # User email
EAM_PROJECT_ID=project-aIgu7x4r9         # EAM project ID

# JWT
USE_REAL_JWT=false                        # false=fake JWT, true=real JWT via Chrome

# LLM Controller
LITELLM_MODEL=anthropic/glm-4.7          # LLM model for controller
LITELLM_REVIEW_MODEL=anthropic/glm-5     # LLM model for reviewer (can be different)
LITELLM_API_KEY=your-key-here             # API key
LITELLM_API_BASE=https://api.z.ai/api/anthropic  # API base URL

# Langfuse
LANGFUSE_PROJECT_ID=cmcvpwakl003bnu07yhh4p0bb    # For trace URL correction
```

### JWT Setup

**Fake JWT (`USE_REAL_JWT=false`)** — Auto-generated, works when orchestrator/campaign-agent skip signature verification. Good for local dev.

**Real JWT (`USE_REAL_JWT=true`)** — Each session opens headless Chrome to intercept a real JWT from the frontend. Requires a saved Chrome auth profile:

```bash
# First time: manual login to save profile
.venv/bin/python -m auth.refresh_jwt
# Opens Chrome → log in via Google → close the tab → profile saved

# After this, real JWT is captured automatically (headless)
```

## Usage

### Two Modes

#### Orchestrator Mode (default)

Routes through the orchestrator → campaign-agent. The orchestrator manages session state and conversation history server-side.

```bash
# Run all scenarios via orchestrator
.venv/bin/python run.py

# Run in parallel (3 sessions)
.venv/bin/python run.py -n 3

# Filter by scenario name
.venv/bin/python run.py -k "sms"
```

**Requires:** Orchestrator running at `ORCHESTRATOR_URL`, campaign-agent behind it.

#### A2A Mode

Calls campaign-agent directly via A2A JSON-RPC protocol. Client manages conversation history (including tool call history) per turn. Bypasses the orchestrator entirely.

```bash
# Run all scenarios via A2A
.venv/bin/python run.py --mode a2a

# A2A + parallel + filter
.venv/bin/python run.py --mode a2a -n 3 -k "sms"
```

**Requires:** Campaign-agent running at `CAMPAIGN_AGENT_URL`. No orchestrator needed.

#### When to Use Which

| | Orchestrator | A2A |
|---|---|---|
| Target | `localhost:8888` (orchestrator) | `localhost:8777` (campaign-agent) |
| Protocol | SSE streaming | JSON-RPC |
| History | Server-side (orchestrator manages) | Client-side (sent each turn) |
| Langfuse URLs | Extracted from SSE events | Not available |
| Use when | Testing full stack (orchestrator + agent) | Testing campaign-agent in isolation |

### Common Options

```bash
# Delete old results before running
.venv/bin/python run.py --clean

# Disable live dashboard (for CI or logging)
.venv/bin/python run.py --no-dashboard

# Use a different scenarios file
.venv/bin/python run.py --scenarios my_tests.yaml

# Combine options
.venv/bin/python run.py --mode a2a --clean -n 3 -k "sms"
```

### Live Dashboard

When running in a terminal, a Rich live dashboard shows real-time progress:
- Overall progress (completed / errors / running / pending)
- Per-scenario status, current turn, elapsed time, and detail
- Use `--no-dashboard` to disable

### View Results

```bash
# Summary table (Rich formatted)
.venv/bin/python summary.py

# Export to CSV
.venv/bin/python summary.py --csv results.csv

# View specific result
cat test_results/{session-uuid}/result.json | python -m json.tool
```

## Scenario Format

Scenarios are defined in `scenarios.yaml`:

```yaml
scenarios:
  - name: "test_sms_rec_happy_path"
    prompt: >
      請幫我建立一個 SMS 行銷活動，使用推薦商品模型。
    controller_instructions: >
      對話風格簡潔合作。不要讓 agent 建立 segment。
    steps:
      - when: "agent 問受眾"
        reply: "用 All Users"
      - when: "agent 問發送時間"
        reply: "明天早上 10 點"
      - when: "agent 問商品資訊"
        reply: "幫我搜尋推薦的商品"
    review_instructions: >
      驗證 agent 是否正確完成 SMS + REC 流程。
    responses:
      - name: "scenario_id_binding"
        description: "scenario_id 在所有下游 tool call 一致"
      - name: "flow_completion"
        description: "Agent 完成完整流程到達 presentCampaign"
    max_turns: 30
    enabled: true        # set false to skip
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Scenario identifier, shown in results |
| `prompt` | Yes | First message sent to the agent |
| `controller_instructions` | No | General direction for the LLM controller |
| `steps` | No | Key decision points — controller prioritizes these over free-form |
| `review_instructions` | No | Instructions for session-level reviewer |
| `responses` | No | Response metrics to score (requires `review_instructions`) |
| `max_turns` | No | Max conversation turns (default: 30) |
| `enabled` | No | Set `false` to skip (default: true) |

### How `steps` Work

Steps define **when → reply** pairs for critical decision points. The LLM controller checks steps first; if none match the current situation, it follows `controller_instructions` freely.

```yaml
steps:
  - when: "agent 問受眾"           # Condition (natural language)
    reply: "用 All Users"          # Exact reply to use
```

### How Review Works

After the conversation ends, each response metric is **independently and concurrently** evaluated by the LLM reviewer (max 3 concurrent, using `LITELLM_REVIEW_MODEL`). Each metric gets its own LLM call.

Scores: `1` = pass, `0` = fail. Results are saved in `result.json` under `review`.

## Result Structure

Each test produces `test_results/{session_id}/result.json`:

```json
{
  "session_id": "72bee0aa-...",
  "scenario_name": "test_sms_rec_happy_path",
  "status": "completed",
  "reason": "...",
  "prompt": "...",
  "controller_instructions": "...",
  "steps": [...],
  "timestamp": "2026-03-27T12:16:44",
  "turns": 13,
  "history": [
    {
      "user": "user message",
      "agent": "agent reply",
      "langfuse_trace_url": "https://langfuse.appier.net/project/.../traces/...",
      "tool_calls": [
        {
          "name": "getClientSettings",
          "args": {},
          "response": {"channels": ["push", "sms", ...]}
        }
      ]
    }
  ],
  "review": {
    "scores": {"scenario_id_binding": 1, "flow_completion": 1},
    "details": {"scenario_id_binding": "...", "flow_completion": "..."},
    "review_detail": "- scenario_id_binding: PASS — ...\n- flow_completion: PASS — ..."
  },
  "token_usage": {
    "by_turn": [...],
    "total": {"prompt_token_count": ..., "candidates_token_count": ..., "total_token_count": ...}
  }
}
```

## Architecture

```
run.py                    CLI entry point (--mode, --parallel, -k, --clean)
  │
  ├─ auth/jwt_manager.py  Get JWT (fake or real via headless Chrome)
  │
  ├─ client/
  │   ├─ orchestrator.py  Orchestrator mode: async SSE client (server-side history)
  │   ├─ a2a.py           A2A mode: JSON-RPC client (client-side history)
  │   └─ sse_parser.py    Parse SSE stream → agent text + tool calls + langfuse URL
  │
  ├─ controller/
  │   ├─ llm.py           LiteLLM provider (JSON parsing + retry + model override)
  │   ├─ decide.py        Controller decision logic (with steps support)
  │   └─ reviewer.py      Session-level reviewer (semaphore-limited, per-metric scoring)
  │
  ├─ dashboard.py          Rich live dashboard (real-time progress monitoring)
  ├─ runner.py             Conversation loop (multi-turn send/receive → result)
  │
  └─ summary.py            Results aggregation (Rich table + CSV export)
```

## API Flow

### Orchestrator Mode
```
Per scenario:
  1. Get JWT (fake or real)
  2. POST /api/adk/apps/multi_agent/users/{user_id}/sessions → session_id
  3. Loop:
     a. POST /api/adk/run_sse → SSE stream
     b. Parse SSE → agent text + tool calls + langfuse trace URL
     c. LLM controller decides next step (history + steps + instructions)
     d. verdict=stop → exit loop
  4. Session-level review (async, max 3 concurrent, per response metric)
  5. Save test_results/{session_id}/result.json
```

### A2A Mode
```
Per scenario:
  1. Get JWT (for campaign-agent tool auth)
  2. Generate UUID session_id (no server call)
  3. Loop:
     a. Build message.parts from full conversation history
        (user text + agent text + FunctionCall/FunctionResponse as text with author metadata)
     b. POST /a2a (JSON-RPC message/send)
     c. Parse response → agent text + tool calls
     d. Append agent response parts to client-side history
     e. LLM controller decides next step
     f. verdict=stop → exit loop
  4. Session-level review (async, max 3 concurrent, per response metric)
  5. Save test_results/{session_id}/result.json
```

## Prerequisites

- **Python 3.12+**
- **Campaign agent** running at `http://localhost:8777` (required for both modes)
- **Orchestrator** running at `http://localhost:8888` (orchestrator mode only)
- **Agent UI** at `http://localhost:8778` (only needed for `USE_REAL_JWT=true`)
