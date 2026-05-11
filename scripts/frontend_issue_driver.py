#!/usr/bin/env python3
"""Drive frontend development issues from a local plan into Linear.

The driver is intentionally small and dependency-free. It reads a JSON issue
plan, creates exactly one Linear issue at a time, records local progress, and
can mark the active issue done only after its configured validation commands
pass.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from urllib import error, request


LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
DEFAULT_STATE_FILE = ".frontend_issue_driver_state.json"


@dataclass(frozen=True)
class PlannedIssue:
    key: str
    title: str
    description: str
    validation: tuple[str, ...] = field(default_factory=tuple)
    labels: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DriverState:
    next_index: int = 0
    active_key: str | None = None
    active_linear_id: str | None = None
    completed_keys: tuple[str, ...] = field(default_factory=tuple)

    def as_json(self) -> dict[str, Any]:
        return {
            "next_index": self.next_index,
            "active_key": self.active_key,
            "active_linear_id": self.active_linear_id,
            "completed_keys": list(self.completed_keys),
        }


def build_status_report(plan: list[PlannedIssue], state: DriverState) -> dict[str, Any]:
    completed_count = len(state.completed_keys)
    total_planned = len(plan)
    remaining_count = max(total_planned - completed_count - (1 if state.active_key is not None else 0), 0)
    if state.active_key is not None:
        driver_status = "active"
    elif completed_count >= total_planned and state.next_index >= total_planned:
        driver_status = "complete"
    else:
        driver_status = "idle"
    return {
        **state.as_json(),
        "total_planned": total_planned,
        "completed_count": completed_count,
        "remaining_count": remaining_count,
        "driver_status": driver_status,
    }


class LinearClient:
    def __init__(self, api_key: str, endpoint: str = LINEAR_GRAPHQL_URL) -> None:
        self.api_key = api_key
        self.endpoint = endpoint

    def query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        req = request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Authorization": self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Linear API returned HTTP {exc.code}: {details}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Linear API request failed: {exc.reason}") from exc

        parsed = json.loads(body)
        if parsed.get("errors"):
            raise RuntimeError(f"Linear API errors: {parsed['errors']}")
        data = parsed.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("Linear API response did not include a data object.")
        return data

    def team_id(self, team_key: str) -> str:
        data = self.query(
            """
            query Team($key: String!) {
              team(id: $key) { id }
            }
            """,
            {"key": team_key},
        )
        team = data.get("team")
        if not isinstance(team, dict) or not team.get("id"):
            raise RuntimeError(f"Linear team not found: {team_key}")
        return str(team["id"])

    def state_id(self, team_id: str, state_name: str) -> str:
        data = self.query(
            """
            query WorkflowStates($teamId: String!) {
              workflowStates(filter: { team: { id: { eq: $teamId } } }) {
                nodes { id name }
              }
            }
            """,
            {"teamId": team_id},
        )
        nodes = data.get("workflowStates", {}).get("nodes", [])
        for node in nodes:
            if isinstance(node, dict) and str(node.get("name", "")).casefold() == state_name.casefold():
                return str(node["id"])
        raise RuntimeError(f"Linear workflow state not found: {state_name}")

    def create_issue(self, *, team_id: str, state_id: str, issue: PlannedIssue) -> str:
        data = self.query(
            """
            mutation CreateIssue($input: IssueCreateInput!) {
              issueCreate(input: $input) {
                success
                issue { id identifier url }
              }
            }
            """,
            {
                "input": {
                    "teamId": team_id,
                    "stateId": state_id,
                    "title": issue.title,
                    "description": issue.description,
                    "labelIds": [],
                }
            },
        )
        result = data.get("issueCreate")
        created = result.get("issue") if isinstance(result, dict) else None
        if not isinstance(result, dict) or not result.get("success") or not isinstance(created, dict):
            raise RuntimeError(f"Linear issue creation failed: {result}")
        return str(created["id"])

    def update_issue_state(self, *, issue_id: str, state_id: str) -> None:
        data = self.query(
            """
            mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) {
              issueUpdate(id: $id, input: $input) { success }
            }
            """,
            {"id": issue_id, "input": {"stateId": state_id}},
        )
        result = data.get("issueUpdate")
        if not isinstance(result, dict) or not result.get("success"):
            raise RuntimeError(f"Linear issue update failed: {result}")


def load_plan(path: Path) -> list[PlannedIssue]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("issues") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("plan must be a JSON array or an object with an 'issues' array.")

    issues: list[PlannedIssue] = []
    keys: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"issue at index {index} must be an object.")
        key = _required_text(row, "key", index)
        if key in keys:
            raise ValueError(f"duplicate issue key in plan: {key}")
        keys.add(key)
        validation = tuple(_string_list(row.get("validation", []), f"issues[{index}].validation"))
        labels = tuple(_string_list(row.get("labels", []), f"issues[{index}].labels"))
        issues.append(
            PlannedIssue(
                key=key,
                title=_required_text(row, "title", index),
                description=str(row.get("description", "")).strip(),
                validation=validation,
                labels=labels,
            )
        )
    return issues


def load_state(path: Path) -> DriverState:
    if not path.exists():
        return DriverState()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("state file must contain a JSON object.")
    return DriverState(
        next_index=int(payload.get("next_index", 0)),
        active_key=payload.get("active_key"),
        active_linear_id=payload.get("active_linear_id"),
        completed_keys=tuple(_string_list(payload.get("completed_keys", []), "completed_keys")),
    )


def save_state(path: Path, state: DriverState) -> None:
    path.write_text(json.dumps(state.as_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def create_next_issue(
    *,
    plan: list[PlannedIssue],
    state: DriverState,
    linear: LinearClient,
    team_id: str,
    todo_state_id: str,
) -> DriverState:
    if state.active_key is not None:
        return state
    if state.next_index >= len(plan):
        return state
    issue = plan[state.next_index]
    linear_id = linear.create_issue(team_id=team_id, state_id=todo_state_id, issue=issue)
    return DriverState(
        next_index=state.next_index + 1,
        active_key=issue.key,
        active_linear_id=linear_id,
        completed_keys=state.completed_keys,
    )


def complete_active_issue(
    *,
    plan: list[PlannedIssue],
    state: DriverState,
    linear: LinearClient,
    done_state_id: str,
    cwd: Path,
    dry_run: bool = False,
) -> DriverState:
    if state.active_key is None or state.active_linear_id is None:
        raise ValueError("there is no active Linear issue to complete.")
    active = _issue_by_key(plan, state.active_key)
    run_validation(active.validation, cwd=cwd, dry_run=dry_run)
    if not dry_run:
        linear.update_issue_state(issue_id=state.active_linear_id, state_id=done_state_id)
    return DriverState(
        next_index=state.next_index,
        active_key=None,
        active_linear_id=None,
        completed_keys=state.completed_keys + (state.active_key,),
    )


def run_validation(commands: tuple[str, ...], *, cwd: Path, dry_run: bool = False) -> None:
    for command in commands:
        print(f"+ {command}", flush=True)
        if dry_run:
            continue
        subprocess.run(command, cwd=cwd, shell=True, check=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Drive one Linear frontend-development issue at a time from a JSON plan.")
    parser.add_argument("action", choices=["next", "complete", "run", "status"])
    parser.add_argument("--plan", required=True, type=Path, help="JSON plan with an issues array.")
    parser.add_argument("--state-file", default=Path(DEFAULT_STATE_FILE), type=Path)
    parser.add_argument("--team-key", help="Linear team key, for example AI4.")
    parser.add_argument("--todo-state", default="Todo")
    parser.add_argument("--done-state", default="Done")
    parser.add_argument("--repo-root", default=Path.cwd(), type=Path, help="Working directory for validation commands.")
    parser.add_argument("--dry-run", action="store_true", help="Validate local flow without changing Linear or running commands.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.plan)
        state = load_state(args.state_file)
        if args.action == "status":
            print(json.dumps(build_status_report(plan, state), ensure_ascii=False, indent=2))
            return 0

        if not args.team_key:
            raise ValueError("--team-key is required unless action is status.")
        linear = _linear_client(args.dry_run)
        team_id = "dry-run-team" if args.dry_run else linear.team_id(args.team_key)
        todo_state_id = "dry-run-todo" if args.dry_run else linear.state_id(team_id, args.todo_state)
        done_state_id = "dry-run-done" if args.dry_run else linear.state_id(team_id, args.done_state)

        if args.action == "next":
            state = create_next_issue(plan=plan, state=state, linear=linear, team_id=team_id, todo_state_id=todo_state_id)
        elif args.action == "complete":
            state = complete_active_issue(
                plan=plan,
                state=state,
                linear=linear,
                done_state_id=done_state_id,
                cwd=args.repo_root,
                dry_run=args.dry_run,
            )
        elif args.action == "run":
            state = create_next_issue(plan=plan, state=state, linear=linear, team_id=team_id, todo_state_id=todo_state_id)
            while state.active_key is not None:
                state = complete_active_issue(
                    plan=plan,
                    state=state,
                    linear=linear,
                    done_state_id=done_state_id,
                    cwd=args.repo_root,
                    dry_run=args.dry_run,
                )
                state = create_next_issue(plan=plan, state=state, linear=linear, team_id=team_id, todo_state_id=todo_state_id)
        save_state(args.state_file, state)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        parser.exit(2, f"frontend_issue_driver.py: error: {exc}\n")
    print(json.dumps(state.as_json(), ensure_ascii=False, indent=2))
    return 0


def _linear_client(dry_run: bool) -> LinearClient:
    if dry_run:
        return DryRunLinearClient()
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        raise RuntimeError("LINEAR_API_KEY is required unless --dry-run is used.")
    return LinearClient(api_key)


class DryRunLinearClient(LinearClient):
    def __init__(self) -> None:
        super().__init__("dry-run")
        self._counter = 0

    def query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("dry-run client does not execute GraphQL queries.")

    def create_issue(self, *, team_id: str, state_id: str, issue: PlannedIssue) -> str:
        self._counter += 1
        print(f"dry-run: create Linear issue key={issue.key} title={issue.title!r} state={state_id}")
        return f"dry-run-issue-{self._counter}"

    def update_issue_state(self, *, issue_id: str, state_id: str) -> None:
        print(f"dry-run: update Linear issue id={issue_id} state={state_id}")


def _required_text(row: dict[str, Any], field_name: str, index: int) -> str:
    value = row.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"issues[{index}].{field_name} must be a non-empty string.")
    return value.strip()


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be an array of strings.")
    return [item for item in value if item.strip()]


def _issue_by_key(plan: list[PlannedIssue], key: str) -> PlannedIssue:
    for issue in plan:
        if issue.key == key:
            return issue
    raise ValueError(f"active issue key is not present in plan: {key}")


if __name__ == "__main__":
    raise SystemExit(main())
