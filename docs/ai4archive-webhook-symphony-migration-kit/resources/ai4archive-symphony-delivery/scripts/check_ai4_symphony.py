#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import argparse
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_SKILL_NAME = "ai4archive-symphony-delivery"
DEFAULT_STATE_URL = "http://127.0.0.1:4000/api/v1/state"
DEFAULT_TODO_STALE_MINUTES = 30
DEFAULT_ACTIVE_RUNAWAY_MINUTES = 10
DEFAULT_ACTIVE_RUNAWAY_TOKENS = 2_000_000
DEFAULT_ZERO_TOKEN_STALL_MINUTES = 10
DEFAULT_ZERO_TOKEN_STALL_TURNS = 2


def load_config() -> dict[str, str]:
    candidates: list[Path] = []
    if os.environ.get("AI4_SYMPHONY_CONFIG"):
        candidates.append(Path(os.environ["AI4_SYMPHONY_CONFIG"]).expanduser())

    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    candidates.extend(
        [
            codex_home / "ai4archive-symphony.json",
            codex_home / "skills" / DEFAULT_SKILL_NAME / "config.local.json",
        ]
    )

    for path in candidates:
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid config JSON at {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise SystemExit(f"Config JSON must be an object: {path}")
        return {str(key): str(value) for key, value in raw.items() if value is not None}
    return {}


CONFIG = load_config()


def value(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or CONFIG.get(name) or CONFIG.get(name.lower()) or default


def int_value(name: str, default: int) -> int:
    raw = value(name, str(default))
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default


def path_value(name: str) -> Path | None:
    raw = value(name)
    return Path(raw).expanduser() if raw else None


CODEX_HOME = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
SYMPHONY_DIR = path_value("AI4_SYMPHONY_DIR")
WORKSPACE_ROOT = path_value("AI4_WORKSPACE_ROOT")
MAIN_REPO = path_value("AI4_MAIN_REPO")
STARTUP_SCRIPT_NAME = value("AI4_SYMPHONY_STARTUP_SCRIPT", "run_ai4archive_symphony.sh")
EXPECTED_PROJECT_SLUG = value("AI4_LINEAR_PROJECT_SLUG")
EXPECTED_REPO = value("AI4_GITHUB_REPO")
STATE_URL = value("AI4_SYMPHONY_STATE_URL", DEFAULT_STATE_URL) or DEFAULT_STATE_URL
TODO_STALE_MINUTES = int_value("AI4_TODO_STALE_MINUTES", DEFAULT_TODO_STALE_MINUTES)
ACTIVE_RUNAWAY_MINUTES = int_value("AI4_ACTIVE_RUNAWAY_MINUTES", DEFAULT_ACTIVE_RUNAWAY_MINUTES)
ACTIVE_RUNAWAY_TOKENS = int_value("AI4_ACTIVE_RUNAWAY_TOKENS", DEFAULT_ACTIVE_RUNAWAY_TOKENS)
ZERO_TOKEN_STALL_MINUTES = int_value("AI4_ZERO_TOKEN_STALL_MINUTES", DEFAULT_ZERO_TOKEN_STALL_MINUTES)
ZERO_TOKEN_STALL_TURNS = int_value("AI4_ZERO_TOKEN_STALL_TURNS", DEFAULT_ZERO_TOKEN_STALL_TURNS)
TMUX_SESSION = value("AI4_SYMPHONY_TMUX_SESSION", "ai4archive-symphony") or "ai4archive-symphony"
WEBHOOK_HEALTH_URL = value("AI4_WEBHOOK_HEALTH_URL", "http://127.0.0.1:4010/healthz") or "http://127.0.0.1:4010/healthz"
WEBHOOK_TMUX_SESSION = value("AI4_WEBHOOK_TMUX_SESSION", "ai4-codex-webhook") or "ai4-codex-webhook"
CODEX_THREAD_ID = value("AI4_CODEX_THREAD_ID", "") or ""
CODEX_CWD = value("AI4_CODEX_CWD", str(Path.cwd())) or str(Path.cwd())
WEBHOOK_STATE_DIR = Path(value("AI4_CODEX_WEBHOOK_STATE_DIR", CODEX_CWD) or CODEX_CWD).expanduser() / ".ai4_codex_webhook"
WEBHOOK_TURN_LOCK = WEBHOOK_STATE_DIR / "codex-turn.lock"
WEBHOOK_STATE_FILE = WEBHOOK_STATE_DIR / "state.json"
AUTOMATION_FILE = path_value("AI4_AUTOMATION_FILE")
AUTOMATION_INTERVAL = value("AI4_AUTOMATION_INTERVAL", "FREQ=MINUTELY;INTERVAL=30")
SKILL_NAME = value("AI4_SKILL_NAME", DEFAULT_SKILL_NAME) or DEFAULT_SKILL_NAME
SKILL_ROOT = path_value("AI4_SKILL_ROOT") or CODEX_HOME / "skills" / SKILL_NAME
WEBHOOK_ORCHESTRATOR_SCRIPT = (
    path_value("AI4_WEBHOOK_ORCHESTRATOR_SCRIPT")
    or SKILL_ROOT / "tools" / "ai4_codex_webhook_orchestrator.py"
)
ORCHESTRATION_STATE_FILE = (
    path_value("AI4_ORCHESTRATION_STATE_FILE")
    or SKILL_ROOT / "state" / "orchestration-state.json"
)


def discover_symphony_dir() -> Path | None:
    if SYMPHONY_DIR is not None:
        return SYMPHONY_DIR

    cwd = Path.cwd()
    if (cwd / "WORKFLOW.md").is_file():
        return cwd

    roots = [WORKSPACE_ROOT] if WORKSPACE_ROOT is not None else []
    roots.extend(
        [
            Path.home() / "code" / "ai4archive-symphony-workspaces",
            Path.home() / "ai4archive-symphony-workspaces",
        ]
    )
    for root in roots:
        if root is None or not root.is_dir():
            continue
        matches = [
            path
            for path in root.glob("**/WORKFLOW.md")
            if (path.parent / ".env.symphony.local").is_file()
            or (path.parent / (STARTUP_SCRIPT_NAME or "")).is_file()
        ]
        if matches:
            matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            return matches[0].parent
    return None


def discover_automation_file() -> Path | None:
    if AUTOMATION_FILE is not None:
        return AUTOMATION_FILE
    roots = [CODEX_HOME / "automations"]
    for root in roots:
        if not root.is_dir():
            continue
        matches = sorted(root.glob("*/automation.toml"))
        if matches:
            return matches[0]
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check the ai4archive Symphony orchestration environment."
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="print a one-line status summary for heartbeat probes",
    )
    parser.add_argument(
        "--fail-only",
        action="store_true",
        help="print only failed checks, or a compact success line when none failed",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    checks: list[tuple[str, bool, str, bool]] = []

    symphony_dir = discover_symphony_dir()
    automation_file = discover_automation_file()

    checks.append(
        (
            "symphony_dir_resolved",
            symphony_dir is not None,
            str(symphony_dir) if symphony_dir else "set AI4_SYMPHONY_DIR or run from the Symphony runtime dir",
            True,
        )
    )

    workflow = symphony_dir / "WORKFLOW.md" if symphony_dir else None
    env_file = symphony_dir / ".env.symphony.local" if symphony_dir else None
    start_script = symphony_dir / STARTUP_SCRIPT_NAME if symphony_dir and STARTUP_SCRIPT_NAME else None

    checks.append(("workflow_exists", bool(workflow and workflow.is_file()), str(workflow) if workflow else "unresolved", True))
    checks.append(("env_file_exists", bool(env_file and env_file.is_file()), str(env_file) if env_file else "unresolved", True))
    checks.append(("startup_script_exists", bool(start_script and start_script.is_file()), str(start_script) if start_script else "unresolved", True))

    workflow_text = workflow.read_text(encoding="utf-8", errors="ignore") if workflow and workflow.is_file() else ""
    if EXPECTED_PROJECT_SLUG:
        checks.append(("project_slug_ok", EXPECTED_PROJECT_SLUG in workflow_text, EXPECTED_PROJECT_SLUG, True))
    else:
        checks.append(("project_slug_configured", False, "optional: set AI4_LINEAR_PROJECT_SLUG", False))
    checks.append(("env_reference_ok", "api_key: $LINEAR_API_KEY" in workflow_text, "api_key: $LINEAR_API_KEY", True))
    if WORKSPACE_ROOT:
        checks.append(("workspace_root_exists", WORKSPACE_ROOT.is_dir(), str(WORKSPACE_ROOT), True))
        checks.append(("workspace_root_referenced", str(WORKSPACE_ROOT) in workflow_text or WORKSPACE_ROOT.name in workflow_text, str(WORKSPACE_ROOT), False))
    else:
        checks.append(("workspace_root_configured", False, "optional: set AI4_WORKSPACE_ROOT", False))
    if EXPECTED_REPO:
        checks.append(("repo_ok", repo_expected(EXPECTED_REPO, workflow_text, MAIN_REPO), EXPECTED_REPO, True))
    else:
        checks.append(("repo_configured", False, "optional: set AI4_GITHUB_REPO", False))

    env_text = env_file.read_text(encoding="utf-8", errors="ignore") if env_file and env_file.is_file() else ""
    checks.append(("linear_env_present", "LINEAR_API_KEY=" in env_text, "value redacted", True))

    api_ok, api_detail = check_state_api()
    checks.append(("state_api_available", api_ok, api_detail, True))

    tmux_ok, tmux_detail = check_tmux()
    checks.append(("tmux_session_running", tmux_ok, tmux_detail, False))

    webhook_ok, webhook_detail = check_webhook_bridge()
    checks.append(("webhook_bridge_available", webhook_ok, webhook_detail, False))
    webhook_process_ok, webhook_process_detail = check_webhook_process()
    checks.append(("webhook_process_fresh", webhook_process_ok, webhook_process_detail, True))
    webhook_turn_ok, webhook_turn_detail = check_webhook_turn_state()
    checks.append(("webhook_turn_state", webhook_turn_ok, webhook_turn_detail, True))
    webhook_tmux_ok, webhook_tmux_detail = check_tmux_session(WEBHOOK_TMUX_SESSION)
    checks.append(("webhook_tmux_session_running", webhook_tmux_ok, webhook_tmux_detail, False))
    pending_linear_ok, pending_linear_detail = check_pending_linear_mutations()
    checks.append(("pending_linear_mutations", pending_linear_ok, pending_linear_detail, True))
    post_merge_ok, post_merge_detail = check_post_merge_finalizers()
    checks.append(("post_merge_finalizers", post_merge_ok, post_merge_detail, True))
    active_locks_ok, active_locks_detail = check_idle_active_scope_locks()
    checks.append(("idle_active_scope_locks", active_locks_ok, active_locks_detail, True))
    idle_dirty_ok, idle_dirty_detail = check_idle_dirty_workspaces()
    checks.append(("idle_dirty_workspaces", idle_dirty_ok, idle_dirty_detail, True))

    automation_checks = check_automation(automation_file)
    checks.extend(automation_checks)

    failed = [row for row in checks if row[3] and not row[1]]
    payload = {
        "ok": not failed,
        "symphony_dir": str(symphony_dir) if symphony_dir else None,
        "workspace_root": str(WORKSPACE_ROOT) if WORKSPACE_ROOT else None,
        "main_repo": str(MAIN_REPO) if MAIN_REPO else None,
        "state_url": STATE_URL,
        "automation_file": str(automation_file) if automation_file else None,
        "checks": [
            {"name": name, "ok": ok, "required": required, "detail": detail}
            for name, ok, detail, required in checks
        ],
    }
    if args.compact:
        print(compact_summary(payload, failed, include_optional=False))
    elif args.fail_only:
        print_fail_only(payload, failed)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not failed else 2


def compact_summary(
    payload: dict[str, object],
    failed: list[tuple[str, bool, str, bool]],
    *,
    include_optional: bool,
) -> str:
    checks = payload.get("checks", [])
    check_map = {
        item.get("name"): item
        for item in checks
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    optional_bad: list[str] = []
    if include_optional:
        optional_bad = [
            str(item.get("name"))
            for item in checks
            if isinstance(item, dict)
            and not item.get("required")
            and item.get("ok") is False
        ]

    status = "ok=true" if payload.get("ok") else "ok=false"
    required_failed = ",".join(name for name, _ok, _detail, _required in failed) or "-"
    state = detail_for(check_map, "state_api_available")
    tmux = "yes" if bool_for(check_map, "tmux_session_running") else "no"
    if bool_for(check_map, "webhook_bridge_available") and bool_for(check_map, "webhook_process_fresh"):
        webhook = "yes"
    elif bool_for(check_map, "webhook_bridge_available"):
        webhook = "stale"
    else:
        webhook = "no"
    automation = automation_status(check_map)
    parts = [
        status,
        f"failed={required_failed}",
        f"state={state}",
        f"tmux={tmux}",
        f"webhook={webhook}",
        f"automation={automation}",
    ]
    active_issue = active_issue_details()
    if active_issue:
        parts.append(active_issue)
    idle_dirty = detail_for(check_map, "idle_dirty_workspaces")
    if idle_dirty and not bool_for(check_map, "idle_dirty_workspaces"):
        parts.append(f"idle_dirty={idle_dirty}")
    post_merge = detail_for(check_map, "post_merge_finalizers")
    if post_merge and not bool_for(check_map, "post_merge_finalizers"):
        parts.append(f"post_merge={post_merge}")
    active_locks = detail_for(check_map, "idle_active_scope_locks")
    if active_locks and not bool_for(check_map, "idle_active_scope_locks"):
        parts.append(f"active_locks={active_locks}")
    if optional_bad:
        parts.append(f"optional_bad={','.join(optional_bad)}")
    return " ".join(parts)


def print_fail_only(
    payload: dict[str, object],
    failed: list[tuple[str, bool, str, bool]],
) -> None:
    if not failed:
        print(compact_summary(payload, failed, include_optional=True))
        return
    rows = [
        {"name": name, "required": required, "detail": detail}
        for name, _ok, detail, required in failed
    ]
    print(json.dumps({"ok": False, "failed": rows}, ensure_ascii=False, indent=2))


def detail_for(check_map: dict[str, object], name: str) -> str:
    item = check_map.get(name)
    if not isinstance(item, dict):
        return "-"
    detail = item.get("detail")
    return str(detail).replace("\n", " ") if detail is not None else "-"


def bool_for(check_map: dict[str, object], name: str) -> bool:
    item = check_map.get(name)
    return bool(item.get("ok")) if isinstance(item, dict) else False


def automation_status(check_map: dict[str, object]) -> str:
    if not bool_for(check_map, "automation_file_exists"):
        return "missing"
    active = bool_for(check_map, "automation_is_active")
    heartbeat = bool_for(check_map, "automation_is_heartbeat")
    mentions_skill = bool_for(check_map, "automation_mentions_skill")
    interval_ok = bool_for(check_map, "automation_interval_ok")
    if active and heartbeat and mentions_skill and interval_ok:
        return "active"
    bad = []
    if not active:
        bad.append("inactive")
    if not heartbeat:
        bad.append("not-heartbeat")
    if not mentions_skill:
        bad.append("missing-skill")
    if not interval_ok:
        bad.append("interval")
    return ",".join(bad)


def repo_expected(expected: str, workflow_text: str, main_repo: Path | None) -> bool:
    if expected in workflow_text:
        return True
    if main_repo and main_repo.is_dir():
        try:
            result = subprocess.run(
                ["git", "-C", str(main_repo), "remote", "-v"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return False
        return expected in result.stdout or normalize_repo(expected) in normalize_repo(result.stdout)
    return False


def normalize_repo(value: str) -> str:
    return value.replace("git@github.com:", "https://github.com/").replace(".git", "")


def check_state_api() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(STATE_URL, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return False, f"unavailable: {exc}"

    running = data.get("running")
    if not isinstance(running, list):
        return False, "response missing running list"
    retrying = data.get("retrying")
    if retrying is None:
        retrying = []
    if not isinstance(retrying, list):
        return False, "response retrying field is not a list"
    issue_ids = [item.get("issue_identifier") for item in running if isinstance(item, dict)]
    retrying_ids = [item.get("issue_identifier") for item in retrying if isinstance(item, dict)]
    running_age = running_age_details(data, running)
    age_suffix = f" {running_age}" if running_age else ""
    detail = (
        f"running={len(running)} issues={','.join(filter(None, issue_ids))} "
        f"retrying={len(retrying)} retrying_issues={','.join(filter(None, retrying_ids))}"
        f"{age_suffix}"
    )
    stale_todo = stale_todo_issues(data, running)
    if stale_todo:
        return False, f"{detail} stale_todo={','.join(stale_todo)} threshold={TODO_STALE_MINUTES}m"
    zero_token_stall = zero_token_stalled_issues(data, running)
    if zero_token_stall:
        return False, (
            f"{detail} active_zero_token_stall={','.join(zero_token_stall)} "
            f"threshold={ZERO_TOKEN_STALL_MINUTES}m turns={ZERO_TOKEN_STALL_TURNS}"
        )
    runaway_active = runaway_active_issues(data, running)
    if runaway_active:
        return False, f"{detail} active_runaway={','.join(runaway_active)} threshold={ACTIVE_RUNAWAY_MINUTES}m tokens={ACTIVE_RUNAWAY_TOKENS}"
    unsafe_active = unsafe_active_issues(running)
    if unsafe_active:
        return False, f"{detail} active_unsafe_session={','.join(unsafe_active)}"
    return True, detail


def stale_todo_issues(api_state: dict[str, object], running: list[object]) -> list[str]:
    if TODO_STALE_MINUTES <= 0:
        return []
    now = parse_iso(str(api_state.get("generated_at") or "")) or datetime.now(timezone.utc)
    stale: list[str] = []
    for item in running:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "").strip().lower()
        if state != "todo":
            continue
        started = parse_iso(str(item.get("started_at") or ""))
        if not started:
            continue
        if (now - started).total_seconds() >= TODO_STALE_MINUTES * 60:
            stale.append(str(item.get("issue_identifier") or item.get("issue_id") or "unknown"))
    return stale


def zero_token_stalled_issues(api_state: dict[str, object], running: list[object]) -> list[str]:
    if ZERO_TOKEN_STALL_MINUTES <= 0 or ZERO_TOKEN_STALL_TURNS <= 0:
        return []
    now = parse_iso(str(api_state.get("generated_at") or "")) or datetime.now(timezone.utc)
    orchestration = load_orchestration_state()
    stalled: list[str] = []
    for item in running:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "").strip().lower()
        if state not in {"in progress", "todo", "rework"}:
            continue
        issue = str(item.get("issue_identifier") or item.get("issue_id") or "unknown")
        started = issue_started_at(orchestration, issue) or parse_iso(str(item.get("started_at") or ""))
        if not started:
            continue
        age_seconds = (now - started).total_seconds()
        if age_seconds < ZERO_TOKEN_STALL_MINUTES * 60:
            continue
        tokens = item.get("tokens")
        total_tokens = 0
        if isinstance(tokens, dict):
            try:
                total_tokens = int(tokens.get("total_tokens") or 0)
            except (TypeError, ValueError):
                total_tokens = 0
        if total_tokens != 0:
            continue
        try:
            turn_count = int(item.get("turn_count") or 0)
        except (TypeError, ValueError):
            turn_count = 0
        if turn_count < ZERO_TOKEN_STALL_TURNS:
            continue
        last_event = str(item.get("last_event") or "").strip().lower()
        last_message = str(item.get("last_message") or "").strip().lower()
        empty_or_error = (
            last_event in {"notification", "turn_completed"}
            or not last_message
            or last_message == "error"
            or "error" in last_message
        )
        if empty_or_error:
            stalled.append(f"{issue}:turns={turn_count},age={format_duration(age_seconds)}")
    return stalled


def runaway_active_issues(api_state: dict[str, object], running: list[object]) -> list[str]:
    if ACTIVE_RUNAWAY_MINUTES <= 0 or ACTIVE_RUNAWAY_TOKENS <= 0:
        return []
    now = parse_iso(str(api_state.get("generated_at") or "")) or datetime.now(timezone.utc)
    runaway: list[str] = []
    for item in running:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "").strip().lower()
        if state not in {"in progress", "todo", "rework"}:
            continue
        started = parse_iso(str(item.get("started_at") or ""))
        if not started:
            continue
        age_seconds = (now - started).total_seconds()
        if age_seconds < ACTIVE_RUNAWAY_MINUTES * 60:
            continue
        tokens = item.get("tokens")
        total_tokens = 0
        if isinstance(tokens, dict):
            try:
                total_tokens = int(tokens.get("total_tokens") or 0)
            except (TypeError, ValueError):
                total_tokens = 0
        if total_tokens < ACTIVE_RUNAWAY_TOKENS:
            continue
        last_event = str(item.get("last_event") or "").strip().lower()
        last_message = str(item.get("last_message") or "").strip().lower()
        notification_only = last_event == "notification" or "rate limits updated" in last_message
        workspace_path = str(item.get("workspace_path") or "").strip()
        dirty = bool(workspace_path and meaningful_git_status(Path(workspace_path)))
        if notification_only or dirty or age_seconds >= ACTIVE_RUNAWAY_MINUTES * 120:
            issue = str(item.get("issue_identifier") or item.get("issue_id") or "unknown")
            runaway.append(f"{issue}:{total_tokens}")
    return runaway


def unsafe_active_issues(running: list[object]) -> list[str]:
    unsafe: list[str] = []
    for item in running:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "").strip().lower()
        if state not in {"in progress", "todo", "rework"}:
            continue
        session_id = str(item.get("session_id") or "").strip()
        marker = active_session_marker(session_id)
        if marker:
            issue = str(item.get("issue_identifier") or item.get("issue_id") or "unknown")
            unsafe.append(f"{issue}:{marker}")
    return unsafe


def active_session_marker(session_id: str) -> str:
    session_file = find_codex_session_file(session_id)
    if session_file is None:
        return ""
    try:
        text = session_file.read_text(encoding="utf-8", errors="ignore")[-200_000:]
    except OSError:
        return ""
    markers = (
        ("safe_git_blocked", "AI4 safe git blocked destructive git"),
        ("tool_args_parse_failed", "failed to parse function arguments"),
        ("unicode_encode_error", "UnicodeEncodeError"),
    )
    for name, needle in markers:
        if needle in text:
            return name
    return ""


def find_codex_session_file(session_id: str) -> Path | None:
    if not session_id:
        return None
    session_prefix = session_id[:36]
    if not session_prefix:
        return None
    sessions_root = CODEX_HOME / "sessions"
    if not sessions_root.is_dir():
        return None
    matches = sorted(
        sessions_root.glob(f"**/*{session_prefix}.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def check_idle_dirty_workspaces() -> tuple[bool, str]:
    if WORKSPACE_ROOT is None or not WORKSPACE_ROOT.is_dir():
        return True, "workspace_root_unavailable"
    try:
        with urllib.request.urlopen(STATE_URL, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return True, f"state_unavailable:{type(exc).__name__}"

    running = data.get("running")
    retrying = data.get("retrying") or []
    if isinstance(running, list) and running:
        return True, "running"
    if isinstance(retrying, list) and retrying:
        return True, "retrying"

    dirty: list[str] = []
    unpublished: list[str] = []
    active_ids = active_issue_identifiers()
    for workspace in sorted(WORKSPACE_ROOT.iterdir()):
        if not workspace.is_dir() or not (workspace / ".git").exists():
            continue
        if not re.match(r"^[A-Z0-9]+-\d+", workspace.name):
            continue
        lines = meaningful_git_status(workspace)
        if lines:
            dirty.append(f"{workspace.name}:{len(lines)}")
        if workspace.name in active_ids:
            unpublished_detail = unpublished_git_status(workspace)
            if unpublished_detail:
                unpublished.append(f"{workspace.name}:{unpublished_detail}")
    if dirty or unpublished:
        parts: list[str] = []
        if dirty:
            parts.append(f"dirty={','.join(dirty)}")
        if unpublished:
            parts.append(f"unpublished={','.join(unpublished)}")
        return False, " ".join(parts)
    return True, "clean"


def check_idle_active_scope_locks() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(STATE_URL, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return True, f"state_unavailable:{type(exc).__name__}"

    running = data.get("running")
    retrying = data.get("retrying") or []
    if isinstance(running, list) and running:
        return True, "running"
    if isinstance(retrying, list) and retrying:
        return True, "retrying"

    state = load_orchestration_state()
    locks = state.get("issue_scope_locks")
    if not isinstance(locks, dict):
        return True, "none"

    now = parse_iso(str(data.get("generated_at") or "")) or datetime.now(timezone.utc)
    active: list[str] = []
    for identifier, lock in sorted(locks.items()):
        if not isinstance(lock, dict) or str(lock.get("status") or "").lower() != "active":
            continue
        parts = [str(identifier)]
        started = issue_started_at(state, str(identifier))
        if started:
            parts.append(f"age={format_duration((now - started).total_seconds())}")
        pr = lock.get("pr")
        if pr:
            parts.append(f"pr={pr}")
        active.append(":".join([parts[0], ",".join(parts[1:])]) if len(parts) > 1 else parts[0])
    if active:
        return False, ",".join(active)
    return True, "none"


def meaningful_git_status(workspace: Path) -> list[str]:
    try:
        git_meta = workspace / ".git-meta"
        if git_meta.is_dir():
            command = [
                "git",
                f"--git-dir={git_meta}",
                f"--work-tree={workspace}",
                "status",
                "--porcelain",
            ]
        else:
            command = ["git", "-C", str(workspace), "status", "--porcelain"]
        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return []
    return [line for line in result.stdout.splitlines() if line and not low_value_status_line(line)]


def active_issue_identifiers() -> set[str]:
    state = load_orchestration_state()
    active: set[str] = set()
    direct = str(state.get("active_issue_id") or "").strip()
    if direct:
        active.add(direct)
    locks = state.get("issue_scope_locks")
    if isinstance(locks, dict):
        for identifier, lock in locks.items():
            if isinstance(lock, dict) and str(lock.get("status") or "").lower() == "active":
                active.add(str(identifier))
    return active


def unpublished_git_status(workspace: Path) -> str:
    branch = git_stdout(workspace, ["branch", "--show-current"]).strip()
    if not branch or branch in {"main", "master"}:
        return ""

    remote_branch = f"refs/remotes/origin/{branch}"
    if git_success(workspace, ["show-ref", "--verify", "--quiet", remote_branch]):
        return ""

    upstream = git_stdout(
        workspace,
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        stderr=subprocess.DEVNULL,
    ).strip()
    if upstream:
        ahead = git_ahead_count(workspace, upstream)
        return f"{branch}:ahead_upstream={ahead}" if ahead > 0 else ""

    base = "origin/main" if git_success(workspace, ["rev-parse", "--verify", "--quiet", "origin/main"]) else "main"
    ahead = git_ahead_count(workspace, base)
    return f"{branch}:no_upstream,ahead_{base}={ahead}" if ahead > 0 else ""


def git_ahead_count(workspace: Path, base: str) -> int:
    raw = git_stdout(workspace, ["rev-list", "--count", f"{base}..HEAD"]).strip()
    try:
        return int(raw)
    except ValueError:
        return 0


def git_stdout(
    workspace: Path,
    args: list[str],
    *,
    stderr: int | None = subprocess.DEVNULL,
) -> str:
    result = run_workspace_git(workspace, args, stderr=stderr)
    return result.stdout if result and result.returncode == 0 else ""


def git_success(workspace: Path, args: list[str]) -> bool:
    result = run_workspace_git(workspace, args)
    return bool(result and result.returncode == 0)


def run_workspace_git(
    workspace: Path,
    args: list[str],
    *,
    stderr: int | None = subprocess.DEVNULL,
) -> subprocess.CompletedProcess[str] | None:
    git_meta = workspace / ".git-meta"
    if git_meta.is_dir():
        command = ["git", f"--git-dir={git_meta}", f"--work-tree={workspace}", *args]
    else:
        command = ["git", "-C", str(workspace), *args]
    try:
        return subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=stderr,
        )
    except FileNotFoundError:
        return None


def low_value_status_line(line: str) -> bool:
    path = line[3:].replace("\\", "/") if len(line) > 3 else line
    low_value_parts = (
        "__pycache__/",
        ".pytest_cache/",
        ".mypy_cache/",
        ".ruff_cache/",
        ".hypothesis/",
        ".coverage",
    )
    if any(part in path for part in low_value_parts):
        return True
    return path.endswith(".pyc") or ".egg-info/" in path


def running_age_details(api_state: dict[str, object], running: list[object]) -> str:
    if not running:
        return ""
    now = parse_iso(str(api_state.get("generated_at") or "")) or datetime.now(timezone.utc)
    orchestration = load_orchestration_state()
    details: list[str] = []
    for item in running:
        if not isinstance(item, dict):
            continue
        identifier = str(item.get("issue_identifier") or "")
        if not identifier:
            continue
        attempt_started = parse_iso(str(item.get("started_at") or ""))
        issue_started = issue_started_at(orchestration, identifier)
        rework_count = issue_rework_count(orchestration, identifier)
        parts = [identifier]
        if issue_started:
            parts.append(f"issue_age={format_duration((now - issue_started).total_seconds())}")
        if attempt_started:
            parts.append(f"attempt_age={format_duration((now - attempt_started).total_seconds())}")
        if rework_count:
            parts.append(f"reworks={rework_count}")
        details.append(":".join([parts[0], ",".join(parts[1:])]) if len(parts) > 1 else parts[0])
    return "ages=" + ";".join(details) if details else ""


def load_orchestration_state() -> dict[str, object]:
    try:
        raw = json.loads(ORCHESTRATION_STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def issue_started_at(state: dict[str, object], identifier: str) -> datetime | None:
    issues = state.get("issues")
    issue = issues.get(identifier) if isinstance(issues, dict) else None
    candidates: list[dict[str, object]] = []
    if isinstance(issue, dict):
        candidates.append(issue)
    locks = state.get("issue_scope_locks")
    lock = locks.get(identifier) if isinstance(locks, dict) else None
    if isinstance(lock, dict):
        candidates.append(lock)
    for candidate in candidates:
        for key in ("first_started_at", "created_at", "dispatched_at", "updated_at"):
            parsed = parse_iso(str(candidate.get(key) or ""))
            if parsed:
                return parsed
    return None


def issue_rework_count(state: dict[str, object], identifier: str) -> int:
    prs = state.get("prs")
    if not isinstance(prs, dict):
        return 0
    counts: list[int] = []
    for pr in prs.values():
        if not isinstance(pr, dict) or pr.get("issue_id") != identifier:
            continue
        try:
            counts.append(int(pr.get("rework_count") or 0))
        except (TypeError, ValueError):
            continue
    return max(counts) if counts else 0


def active_issue_details() -> str:
    state = load_orchestration_state()
    identifiers = sorted(active_issue_identifiers())
    if not identifiers:
        return ""
    now = datetime.now(timezone.utc)
    issue_parts: list[str] = []
    for identifier in identifiers:
        issue_started = issue_started_at(state, identifier)
        rework_count = issue_rework_count(state, identifier)
        parts = [identifier]
        if issue_started:
            parts.append(f"issue_age={format_duration((now - issue_started).total_seconds())}")
        if rework_count:
            parts.append(f"reworks={rework_count}")
        issue_parts.append(":".join([parts[0], ",".join(parts[1:])]) if len(parts) > 1 else parts[0])
    return f"active_issue={';'.join(issue_parts)}"


def check_pending_linear_mutations() -> tuple[bool, str]:
    state = load_orchestration_state()
    pending = state.get("pending_linear_mutations")
    if pending is None:
        return True, "none"
    if not isinstance(pending, list):
        return False, "pending_linear_mutations must be a list"
    active = [item for item in pending if isinstance(item, dict) and not item.get("applied_at")]
    if not active:
        return True, "none"
    issue_ids = sorted(
        {
            str(item.get("issue_id"))
            for item in active
            if item.get("issue_id")
        }
    )
    suffix = f" issues={','.join(issue_ids)}" if issue_ids else ""
    return False, f"pending={len(active)}{suffix}"


def check_post_merge_finalizers() -> tuple[bool, str]:
    state = load_orchestration_state()
    locks = state.get("issue_scope_locks")
    if not isinstance(locks, dict):
        return True, "none"
    pending: list[str] = []
    incomplete = {"", "pending", "in_progress", "unobserved_pending_recheck", "unknown"}
    completed = {"success", "not_required", "not_required_validator_only"}
    for identifier, lock in locks.items():
        if not isinstance(lock, dict):
            continue
        status = str(lock.get("status") or "").lower()
        if status != "merged_waiting_main_ci":
            continue
        main_ci = lock.get("main_ci")
        conclusion = ""
        if isinstance(main_ci, dict):
            conclusion = str(main_ci.get("conclusion") or "").lower()
        if conclusion not in completed:
            pending.append(f"{identifier}:main_ci={conclusion if conclusion in incomplete else conclusion or 'pending'}")
    if not pending:
        return True, "none"
    return False, ",".join(pending)


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def check_tmux() -> tuple[bool, str]:
    return check_tmux_session(TMUX_SESSION)


def check_tmux_session(session: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False, "tmux not installed"
    return result.returncode == 0, session


def check_webhook_bridge() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(WEBHOOK_HEALTH_URL, timeout=2) as response:
            body = response.read().decode("utf-8", errors="replace").strip()
    except (OSError, urllib.error.URLError) as exc:
        return False, f"unavailable: {exc}"
    ok = 200 <= getattr(response, "status", 0) < 300 and body == "ok"
    return ok, WEBHOOK_HEALTH_URL if ok else f"unexpected response from {WEBHOOK_HEALTH_URL}: {body}"


def check_webhook_process() -> tuple[bool, str]:
    parsed = urllib.parse.urlparse(WEBHOOK_HEALTH_URL)
    port = parsed.port
    if not port:
        return True, "not checked: webhook health URL has no port"
    if not WEBHOOK_ORCHESTRATOR_SCRIPT.is_file():
        return False, f"expected script missing: {WEBHOOK_ORCHESTRATOR_SCRIPT}"
    if shutil.which("lsof") is None:
        return True, "not checked: lsof unavailable"

    result = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    pids = [line[1:] for line in result.stdout.splitlines() if line.startswith("p")]
    if not pids:
        return False, f"no listener process found on webhook port {port}"

    expected = WEBHOOK_ORCHESTRATOR_SCRIPT.resolve()
    details: list[str] = []
    for pid in pids:
        command = subprocess.run(
            ["ps", "-p", pid, "-o", "command="],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout.strip()
        cwd = process_cwd(pid)
        script_path = command_script_path(command, cwd)
        if script_path is None:
            details.append(f"pid={pid} script=unknown command={command[:120]}")
            continue
        try:
            actual = script_path.resolve()
        except OSError:
            actual = script_path
        if paths_same(actual, expected):
            return True, f"pid={pid} script={expected}"
        details.append(f"pid={pid} script={actual} expected={expected}")
    return False, "; ".join(details)


def check_webhook_turn_state() -> tuple[bool, str]:
    pending_count = 0
    try:
        state = json.loads(WEBHOOK_STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    pending = state.get("pending_triggers")
    if isinstance(pending, list):
        pending_count = len([item for item in pending if isinstance(item, dict)])
    elif isinstance(state.get("pending_trigger"), dict):
        pending_count = 1

    lock_pid: str | None = None
    if WEBHOOK_TURN_LOCK.exists():
        try:
            lock_pid = WEBHOOK_TURN_LOCK.read_text(encoding="utf-8").strip()
        except OSError:
            lock_pid = ""
        if not lock_pid or not pid_alive(lock_pid):
            detail = f"stale lock pid={lock_pid or 'unknown'}"
            if pending_count:
                detail += f" pending={pending_count}"
            return False, detail
        detail = f"active lock pid={lock_pid}"
        if pending_count:
            detail += f" pending={pending_count}"
        return True, detail

    if active_codex_process_running():
        detail = "active codex process without webhook lock"
        if pending_count:
            detail += f" pending={pending_count}"
        return True, detail
    if pending_count:
        return False, f"pending triggers without active codex process: {pending_count}"
    return True, str(WEBHOOK_STATE_DIR)


def active_codex_process_running() -> bool:
    if not CODEX_THREAD_ID:
        return False
    needle = f"codex exec resume --all --dangerously-bypass-approvals-and-sandbox {CODEX_THREAD_ID}"
    try:
        result = subprocess.run(
            ["ps", "ax", "-o", "pid=,command="],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        if os.name == "nt":
            env = dict(os.environ)
            env["AI4_CODEX_PROCESS_NEEDLE"] = needle
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        "$needle = $env:AI4_CODEX_PROCESS_NEEDLE; "
                        "Get-CimInstance Win32_Process | "
                        "Where-Object { $_.CommandLine -like \"*$needle*\" } | "
                        "Select-Object -First 1 -ExpandProperty ProcessId"
                    ),
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            return bool(result.stdout.strip())
        try:
            result = subprocess.run(
                ["pgrep", "-f", needle],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return result.returncode == 0
        except FileNotFoundError:
            return False
    current_pid = os.getpid()
    expected_suffix = f"{CODEX_THREAD_ID} -"
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        command = parts[1]
        if pid != current_pid and needle in command and command.rstrip().endswith(expected_suffix):
            return True
    return False


def pid_alive(pid: str) -> bool:
    if not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def process_cwd(pid: str) -> Path | None:
    result = subprocess.run(
        ["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            return Path(line[1:])
    return None


def command_script_path(command: str, cwd: Path | None) -> Path | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    for part in parts:
        if Path(part).name != "ai4_codex_webhook_orchestrator.py":
            continue
        path = Path(part)
        if not path.is_absolute() and cwd is not None:
            path = cwd / path
        return path
    return None


def paths_same(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return left == right


def check_automation(automation_file: Path | None) -> list[tuple[str, bool, str, bool]]:
    checks: list[tuple[str, bool, str, bool]] = []
    detail = str(automation_file) if automation_file else f"optional: {CODEX_HOME}/automations/*/automation.toml"
    checks.append(("automation_file_exists", bool(automation_file and automation_file.is_file()), detail, False))
    if not automation_file or not automation_file.is_file():
        return checks

    text = automation_file.read_text(encoding="utf-8", errors="ignore")
    checks.append(("automation_is_heartbeat", has_toml_value(text, "kind", "heartbeat"), "kind=heartbeat", False))
    checks.append(("automation_is_active", has_toml_value(text, "status", "ACTIVE"), "status=ACTIVE", False))
    checks.append(("automation_mentions_skill", f"${SKILL_NAME}" in text, f"${SKILL_NAME}", False))
    if AUTOMATION_INTERVAL:
        checks.append(("automation_interval_ok", AUTOMATION_INTERVAL in text, AUTOMATION_INTERVAL, False))
    return checks


def has_toml_value(text: str, key: str, expected: str) -> bool:
    pattern = rf'(?m)^\s*{re.escape(key)}\s*=\s*"{re.escape(expected)}"\s*$'
    return re.search(pattern, text) is not None


if __name__ == "__main__":
    raise SystemExit(main())
