"""Generate a complete 2-run integration test in behavior_tuning_workspace.

This simulates the full closed loop:
  e2e-testing explore → d-optimal design → agent-test-api execute → e2e-testing verify → d-optimal analyze

Feature: login-flow-tuning
  run-01: baseline (some metrics fail)
  run-02: improved (review-rules applied, metrics improve)
"""

import csv
import json
import uuid
import yaml
from datetime import datetime
from pathlib import Path

WORKSPACE = Path("/Users/johnny.jiang/Documents/behavior_tuning_workspace")
FEATURE = "login-flow-tuning"
FEATURE_DIR = WORKSPACE / FEATURE

# ---------- Shared definitions ----------

FACTORS = {
    "auth_method": {"type": "categorical", "levels": ["password", "sso"]},
    "language": {"type": "categorical", "levels": ["en", "zh"]},
}

RESPONSES = [
    {"name": "login_success", "description": "User successfully logs in and sees dashboard", "type": "binary"},
    {"name": "error_handling", "description": "Error messages are clear and actionable", "type": "binary"},
    {"name": "session_persistence", "description": "Session persists after page refresh", "type": "binary"},
    {"name": "i18n_correctness", "description": "All UI text matches selected language", "type": "binary"},
]

RESPONSE_NAMES = [r["name"] for r in RESPONSES]

# Design matrix: full factorial 2x2 = 4 runs
DESIGN_MATRIX = [
    {"run_id": 1, "auth_method": "password", "language": "en"},
    {"run_id": 2, "auth_method": "password", "language": "zh"},
    {"run_id": 3, "auth_method": "sso", "language": "en"},
    {"run_id": 4, "auth_method": "sso", "language": "zh"},
]

# ---------- Run 1 scores (baseline - some failures) ----------
RUN1_SCORES = [
    # run_id 1: password+en - login works, error handling bad, session ok, i18n ok
    {"login_success": 1, "error_handling": 0, "session_persistence": 1, "i18n_correctness": 1},
    # run_id 2: password+zh - login works, error handling bad, session ok, i18n FAILS (mixed lang)
    {"login_success": 1, "error_handling": 0, "session_persistence": 1, "i18n_correctness": 0},
    # run_id 3: sso+en - login FAILS (redirect bug), error ok, session n/a, i18n ok
    {"login_success": 0, "error_handling": 1, "session_persistence": 0, "i18n_correctness": 1},
    # run_id 4: sso+zh - login FAILS, error bad, session n/a, i18n fails
    {"login_success": 0, "error_handling": 0, "session_persistence": 0, "i18n_correctness": 0},
]

# ---------- Run 2 scores (improved after review-rules) ----------
RUN2_SCORES = [
    # password+en: all pass now
    {"login_success": 1, "error_handling": 1, "session_persistence": 1, "i18n_correctness": 1},
    # password+zh: error handling fixed, i18n fixed
    {"login_success": 1, "error_handling": 1, "session_persistence": 1, "i18n_correctness": 1},
    # sso+en: login fixed, all pass
    {"login_success": 1, "error_handling": 1, "session_persistence": 1, "i18n_correctness": 1},
    # sso+zh: login fixed, error handling still fails, i18n fixed
    {"login_success": 1, "error_handling": 0, "session_persistence": 1, "i18n_correctness": 1},
]


def make_session_id():
    return str(uuid.uuid4())


def generate_result_json(session_id: str, scenario_name: str, scores: dict, design_row: dict) -> dict:
    """Generate a realistic result.json matching agent-test-api output format."""
    return {
        "session_id": session_id,
        "scenario_name": scenario_name,
        "status": "completed",
        "reason": "All steps completed successfully",
        "prompt": f"我要用 {design_row['auth_method']} 方式登入系統，語言設為 {design_row['language']}",
        "controller_instructions": (
            "你是一個測試工程師，模擬使用者登入流程。\n"
            f"認證方式：{design_row['auth_method']}\n"
            f"語言設定：{design_row['language']}\n"
            "請按照步驟操作並觀察 Agent 回應是否正確。"
        ),
        "steps": [
            {"when": "Agent 顯示登入頁面", "reply": f"使用 {design_row['auth_method']} 登入"},
            {"when": "Agent 要求認證資訊", "reply": "提供測試帳號 test@example.com"},
            {"when": "Agent 完成登入", "reply": "檢查 dashboard 是否正確顯示"},
        ],
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f"),
        "turns": 8,
        "history": [
            {
                "user": f"我要用 {design_row['auth_method']} 方式登入，語言 {design_row['language']}",
                "agent": "好的，我來幫你設定登入流程。首先開啟登入頁面...",
                "tool_calls": [
                    {
                        "name": "navigate_to_login",
                        "args": {"url": "https://app.example.com/login"},
                        "response": {"result": "Login page loaded successfully"},
                    }
                ],
            },
            {
                "user": f"使用 {design_row['auth_method']} 登入",
                "agent": "正在處理認證...",
                "tool_calls": [
                    {
                        "name": f"auth_{design_row['auth_method']}",
                        "args": {"email": "test@example.com", "method": design_row["auth_method"]},
                        "response": {
                            "result": "Authentication successful" if scores["login_success"] else "Authentication failed: SSO redirect error"
                        },
                    }
                ],
            },
            {
                "user": "檢查 dashboard",
                "agent": "Dashboard 載入完成，顯示使用者資訊。" if scores["login_success"] else "無法載入 Dashboard，登入可能失敗。",
                "tool_calls": [
                    {
                        "name": "check_dashboard",
                        "args": {},
                        "response": {"result": "Dashboard elements verified" if scores["login_success"] else "Dashboard not accessible"},
                    }
                ],
            },
            {
                "user": "測試 session 持久性",
                "agent": "重新整理頁面後，" + ("session 仍然有效。" if scores["session_persistence"] else "session 已遺失，需要重新登入。"),
                "tool_calls": [
                    {
                        "name": "refresh_page",
                        "args": {},
                        "response": {"result": "Session valid" if scores["session_persistence"] else "Session lost"},
                    }
                ],
            },
        ],
        "review": {
            "scores": scores,
            "review_detail": _make_review_detail(scores, design_row),
        },
    }


def _make_review_detail(scores: dict, design_row: dict) -> str:
    lines = [f"## Review for {design_row['auth_method']}+{design_row['language']}\n"]
    for metric, val in scores.items():
        status = "PASS" if val == 1 else "FAIL"
        lines.append(f"- **{metric}**: {status}")
    if not scores.get("error_handling"):
        lines.append("\n> Error messages were generic (e.g., 'Something went wrong') instead of actionable.")
    if not scores.get("i18n_correctness"):
        lines.append(f"\n> Mixed language detected: some UI elements still in English when language={design_row['language']}.")
    if not scores.get("login_success"):
        lines.append(f"\n> SSO redirect failed with 302 loop when auth_method={design_row['auth_method']}.")
    return "\n".join(lines)


def write_yaml(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ============================================================
# Phase generators
# ============================================================

def generate_explore(run_dir: Path, run_name: str, is_first_run: bool):
    """e2e-testing skill: Explore phase."""
    # brief.md (feature level, only first run)
    if is_first_run:
        write_text(
            FEATURE_DIR / "brief.md",
            "# Login Flow Tuning\n\n"
            "調整 Agent 處理使用者登入流程的行為品質，涵蓋密碼認證、SSO、多語言支援等場景。\n"
            "目標：確保所有認證方式在所有語言設定下都能正確運作。\n",
        )

    # exploration-notes.md
    notes = (
        "# Exploration Notes\n\n"
        "## 觀察到的行為\n"
        "- Agent 可以正確處理 password 登入流程\n"
        "- SSO 認證有 redirect loop 問題，需要進一步測試\n"
        "- 中文語言設定下部分 UI 元素仍顯示英文\n"
        "- 錯誤訊息過於籠統，缺乏可操作性\n\n"
        "## 建議的 Factors\n"
        "1. **auth_method**: password vs sso（主要差異來源）\n"
        "2. **language**: en vs zh（影響 i18n 正確性）\n\n"
        "## 建議的 Responses\n"
        "1. login_success - 是否成功登入\n"
        "2. error_handling - 錯誤訊息是否清晰\n"
        "3. session_persistence - session 是否持久\n"
        "4. i18n_correctness - 語言是否正確\n\n"
        "## 已知問題\n"
        "- SSO redirect 在某些情況下會進入無限迴圈\n"
        "- 中文環境下 error message 有時混雜英文\n"
    )
    if not is_first_run:
        notes = (
            "# Exploration Notes (Run 02)\n\n"
            "## 基於 Run 01 的改進\n"
            "- 已修復 SSO redirect loop 問題\n"
            "- 已改善錯誤訊息的可讀性\n"
            "- i18n 翻譯覆蓋率提升\n\n"
            "## 需驗證的項目\n"
            "- SSO+zh 組合是否仍有問題\n"
            "- error_handling 在邊界情況下是否改善\n\n"
            "## Review Rules 注入\n"
            "- 已載入 2 條 review rules 到 scenarios.yaml\n"
        )
    write_text(run_dir / "exploration-notes.md", notes)

    # run-context.yaml (explore phase)
    write_yaml(run_dir / "run-context.yaml", {
        "feature": FEATURE,
        "run": run_name,
        "workspace": str(WORKSPACE),
        "phase": "explore",
        "created_at": "2026-04-06",
        "updated_at": "2026-04-06",
        "notes": "初始探索階段" if is_first_run else "基於 run-01 review rules 的改進測試",
    })


def generate_design(run_dir: Path, run_name: str, review_rules_text: str = ""):
    """d-optimal-experiment skill: Design phase."""
    # experiment-config.yaml
    write_yaml(run_dir / "experiment-config.yaml", {
        "experiment": {
            "name": run_name,
            "description": f"Login flow tuning {run_name} - 2x2 full factorial",
            "model": "full_enumeration",
            "n_runs": 4,
            "alpha": 0.05,
        },
        "factors": FACTORS,
        "constraints": [],
        "responses": RESPONSES,
    })

    # design-matrix.csv
    fieldnames = ["run_id", "auth_method", "language"]
    write_csv(run_dir / "design-matrix.csv", DESIGN_MATRIX, fieldnames)

    # scenarios.yaml
    base_review = (
        "## 評分標準\n"
        "1. login_success: 使用者是否成功登入並看到 dashboard\n"
        "2. error_handling: 錯誤訊息是否清晰且可操作\n"
        "3. session_persistence: 重新整理後 session 是否保持\n"
        "4. i18n_correctness: 所有 UI 文字是否符合所選語言\n"
    )
    if review_rules_text:
        base_review += f"\n## 來自前次測試的 Review Rules\n{review_rules_text}\n"

    scenarios = []
    for row in DESIGN_MATRIX:
        scenarios.append({
            "name": f"{run_name}_{row['auth_method']}_{row['language']}",
            "mode": "adk",
            "prompt": f"我要用 {row['auth_method']} 方式登入系統，語言設為 {row['language']}",
            "controller_instructions": (
                f"模擬使用者以 {row['auth_method']} 認證、{row['language']} 語言登入。\n"
                "依序測試：登入流程 → 錯誤處理 → session 持久性 → 語言正確性"
            ),
            "steps": [
                {"when": "Agent 顯示登入頁面", "reply": f"使用 {row['auth_method']} 登入"},
                {"when": "Agent 要求認證資訊", "reply": "提供測試帳號 test@example.com"},
                {"when": "Agent 完成登入", "reply": "檢查 dashboard 是否正確顯示"},
            ],
            "review_instructions": base_review,
            "responses": RESPONSES,
            "max_turns": 20,
        })

    write_yaml(run_dir / "scenarios.yaml", {"scenarios": scenarios})

    # test-plan.md
    write_text(run_dir / "test-plan.md", (
        f"# Test Plan: {run_name}\n\n"
        "## Design\n"
        "Full factorial 2×2 design: auth_method × language\n\n"
        "## Runs\n"
        "| # | auth_method | language |\n"
        "|---|-------------|----------|\n"
        "| 1 | password    | en       |\n"
        "| 2 | password    | zh       |\n"
        "| 3 | sso         | en       |\n"
        "| 4 | sso         | zh       |\n\n"
        "## Metrics\n"
        "- login_success (binary)\n"
        "- error_handling (binary)\n"
        "- session_persistence (binary)\n"
        "- i18n_correctness (binary)\n"
    ))

    # design-report.json
    write_json(run_dir / "design-report.json", {
        "model": "full_enumeration",
        "n_runs": 4,
        "n_factors": 2,
        "d_efficiency": 1.0,
        "design_matrix_path": str(run_dir / "design-matrix.csv"),
    })

    # Update run-context
    ctx = yaml.safe_load((run_dir / "run-context.yaml").read_text()) or {}
    ctx["phase"] = "designed"
    ctx["updated_at"] = "2026-04-06"
    write_yaml(run_dir / "run-context.yaml", ctx)


def generate_execute(run_dir: Path, scores_list: list[dict]) -> list[str]:
    """agent-test-api: Execute phase - generate result.json in sessions/."""
    sessions_dir = run_dir / "sessions"
    run_name = run_dir.name
    session_ids = []

    for i, (design_row, scores) in enumerate(zip(DESIGN_MATRIX, scores_list)):
        sid = make_session_id()
        session_ids.append(sid)
        scenario_name = f"{run_name}_{design_row['auth_method']}_{design_row['language']}"
        result = generate_result_json(sid, scenario_name, scores, design_row)
        write_json(sessions_dir / sid / "result.json", result)

    # Update run-context
    ctx = yaml.safe_load((run_dir / "run-context.yaml").read_text()) or {}
    ctx["phase"] = "executed"
    ctx["sessions_count"] = len(scores_list)
    ctx["updated_at"] = "2026-04-06"
    write_yaml(run_dir / "run-context.yaml", ctx)

    return session_ids


def generate_verify(run_dir: Path, scores_list: list[dict], session_ids: list[str]):
    """e2e-testing skill: Verify phase - generate results.csv."""
    fieldnames = ["run_id", "auth_method", "language"] + RESPONSE_NAMES + ["session_id", "notes"]
    rows = []
    for i, (design_row, scores) in enumerate(zip(DESIGN_MATRIX, scores_list)):
        row = {**design_row, **scores, "session_id": session_ids[i], "notes": ""}
        # Add notes for failures
        notes_parts = []
        if not scores.get("error_handling"):
            notes_parts.append("error messages too generic")
        if not scores.get("i18n_correctness"):
            notes_parts.append("mixed language in UI")
        if not scores.get("login_success"):
            notes_parts.append("SSO redirect loop")
        row["notes"] = "; ".join(notes_parts)
        rows.append(row)

    write_csv(run_dir / "results.csv", rows, fieldnames)

    # Update run-context
    ctx = yaml.safe_load((run_dir / "run-context.yaml").read_text()) or {}
    ctx["phase"] = "verified"
    ctx["updated_at"] = "2026-04-06"
    write_yaml(run_dir / "run-context.yaml", ctx)


def generate_analyze(run_dir: Path, scores_list: list[dict]):
    """d-optimal-experiment skill: Analysis phase."""
    # Compute simple stats
    n = len(scores_list)
    metric_stats = {}
    for r in RESPONSE_NAMES:
        vals = [s[r] for s in scores_list]
        metric_stats[r] = {"mean": sum(vals) / n, "pass_rate": sum(vals) / n, "n": n}

    # analysis-report.json
    effects = {}
    for r in RESPONSE_NAMES:
        # Simple main effect: compare levels
        pw_scores = [s[r] for s, d in zip(scores_list, DESIGN_MATRIX) if d["auth_method"] == "password"]
        sso_scores = [s[r] for s, d in zip(scores_list, DESIGN_MATRIX) if d["auth_method"] == "sso"]
        en_scores = [s[r] for s, d in zip(scores_list, DESIGN_MATRIX) if d["language"] == "en"]
        zh_scores = [s[r] for s, d in zip(scores_list, DESIGN_MATRIX) if d["language"] == "zh"]

        effects[r] = {
            "auth_method": {
                "password_mean": sum(pw_scores) / max(len(pw_scores), 1),
                "sso_mean": sum(sso_scores) / max(len(sso_scores), 1),
                "effect": sum(pw_scores) / max(len(pw_scores), 1) - sum(sso_scores) / max(len(sso_scores), 1),
            },
            "language": {
                "en_mean": sum(en_scores) / max(len(en_scores), 1),
                "zh_mean": sum(zh_scores) / max(len(zh_scores), 1),
                "effect": sum(en_scores) / max(len(en_scores), 1) - sum(zh_scores) / max(len(zh_scores), 1),
            },
        }

    write_json(run_dir / "analysis-report.json", {
        "experiment": run_dir.name,
        "model": "full_enumeration",
        "n_observations": n,
        "metric_summary": metric_stats,
        "main_effects": effects,
        "diagnostics": {"convergence": True, "separation": False},
    })

    # analysis.md
    run_name = run_dir.name
    lines = [f"# Analysis Report: {run_name}\n"]
    lines.append("## 整體通過率\n")
    for r in RESPONSE_NAMES:
        pct = int(metric_stats[r]["pass_rate"] * 100)
        lines.append(f"- **{r}**: {pct}% ({int(metric_stats[r]['pass_rate'] * n)}/{n})")
    lines.append("\n## 主要效果\n")
    for r in RESPONSE_NAMES:
        e = effects[r]
        auth_eff = e["auth_method"]["effect"]
        lang_eff = e["language"]["effect"]
        if abs(auth_eff) > 0.01:
            direction = "password 優於 sso" if auth_eff > 0 else "sso 優於 password"
            lines.append(f"- **{r}** × auth_method: 效果 {auth_eff:+.2f}（{direction}）")
        if abs(lang_eff) > 0.01:
            direction = "en 優於 zh" if lang_eff > 0 else "zh 優於 en"
            lines.append(f"- **{r}** × language: 效果 {lang_eff:+.2f}（{direction}）")

    lines.append("\n## 建議\n")
    overall_rate = sum(metric_stats[r]["pass_rate"] for r in RESPONSE_NAMES) / len(RESPONSE_NAMES)
    if overall_rate < 0.8:
        lines.append("- 整體通過率偏低，建議優先修復 SSO 認證和錯誤訊息\n")
        lines.append("- 中文環境下 i18n 問題需要專項修復\n")
    else:
        lines.append("- 整體表現良好，大部分指標已通過\n")
        lines.append("- 仍需關注 sso+zh 組合的 error_handling\n")

    write_text(run_dir / "analysis.md", "\n".join(lines))

    # Update run-context
    ctx = yaml.safe_load((run_dir / "run-context.yaml").read_text()) or {}
    ctx["phase"] = "analyzed"
    ctx["updated_at"] = "2026-04-06"
    write_yaml(run_dir / "run-context.yaml", ctx)


def generate_review_rules(session_ids_run1: list[str]):
    """Generate review-rules.yaml at feature level based on run-01 findings."""
    rules = [
        {
            "id": f"rule-{uuid.uuid4().hex[:8]}",
            "metric": "error_handling",
            "learned_from": "run-01",
            "session": session_ids_run1[0],
            "description": "Agent 的錯誤訊息過於籠統（如 'Something went wrong'），應提供具體的錯誤原因和建議操作",
            "action": "檢查錯誤回應是否包含：1) 具體錯誤原因 2) 建議的下一步操作 3) 如何避免此錯誤",
            "confirmed_by": "human",
            "created_at": "2026-04-06",
        },
        {
            "id": f"rule-{uuid.uuid4().hex[:8]}",
            "metric": "i18n_correctness",
            "learned_from": "run-01",
            "session": session_ids_run1[3],
            "description": "中文語言設定下，部分系統訊息仍顯示英文，特別是 error message 和 loading 狀態",
            "action": "驗證所有 Agent 回應文字是否完全使用所選語言，包括錯誤訊息、狀態提示、按鈕文字",
            "confirmed_by": "human",
            "created_at": "2026-04-06",
        },
    ]
    write_yaml(FEATURE_DIR / "review-rules.yaml", {"rules": rules})
    return rules


# ============================================================
# Main
# ============================================================

def main():
    print(f"Generating integration test in {FEATURE_DIR}")

    # === Run 01 ===
    run1_dir = FEATURE_DIR / "run-01"
    print("\n--- Run 01: Baseline ---")

    print("  [1/5] Explore phase...")
    generate_explore(run1_dir, "run-01", is_first_run=True)

    print("  [2/5] Design phase...")
    generate_design(run1_dir, "run-01")

    print("  [3/5] Execute phase...")
    session_ids_1 = generate_execute(run1_dir, RUN1_SCORES)
    print(f"         Generated {len(session_ids_1)} sessions")

    print("  [4/5] Verify phase...")
    generate_verify(run1_dir, RUN1_SCORES, session_ids_1)

    print("  [5/5] Analyze phase...")
    generate_analyze(run1_dir, RUN1_SCORES)

    # === Generate review rules from run-01 ===
    print("\n--- Review Rules (from Run 01) ---")
    rules = generate_review_rules(session_ids_1)
    print(f"  Generated {len(rules)} rules")

    # === Run 02 ===
    run2_dir = FEATURE_DIR / "run-02"
    print("\n--- Run 02: Improved ---")

    print("  [1/5] Explore phase...")
    generate_explore(run2_dir, "run-02", is_first_run=False)

    print("  [2/5] Design phase (with review rules)...")
    rules_text = "\n".join(
        f"- **{r['metric']}**: {r['description']} → {r['action']}" for r in rules
    )
    generate_design(run2_dir, "run-02", review_rules_text=rules_text)

    print("  [3/5] Execute phase...")
    session_ids_2 = generate_execute(run2_dir, RUN2_SCORES)
    print(f"         Generated {len(session_ids_2)} sessions")

    print("  [4/5] Verify phase...")
    generate_verify(run2_dir, RUN2_SCORES, session_ids_2)

    print("  [5/5] Analyze phase...")
    generate_analyze(run2_dir, RUN2_SCORES)

    # === Summary ===
    print("\n" + "=" * 60)
    print("Integration test data generated successfully!")
    print(f"Feature: {FEATURE}")
    print(f"  run-01: 4 sessions, baseline (50% avg pass rate)")
    print(f"  run-02: 4 sessions, improved (94% avg pass rate)")
    print(f"  review-rules.yaml: 2 rules")
    print("=" * 60)

    # Verify file count
    all_files = list(FEATURE_DIR.rglob("*"))
    files_only = [f for f in all_files if f.is_file()]
    print(f"\nTotal files generated: {len(files_only)}")
    for f in sorted(files_only):
        print(f"  {f.relative_to(WORKSPACE)}")


if __name__ == "__main__":
    main()
