from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from vibe_loop.config import TaskSourceConfig, find_forbidden_generated_command_keys
from vibe_loop.eval_examples import EXAMPLE_SUITE_ID
from vibe_loop.evals import (
    has_symlink_component,
    is_secret_like_eval_path,
    path_diagnostics,
    validate_skill_eval_run_record,
)
from vibe_loop.tasks import build_task_source


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument(
        "--grader-repo",
        type=Path,
        help="Fixture repository containing grader-only eval metadata",
    )
    args = parser.parse_args(argv)
    result = grade_repository(
        args.repo,
        artifact_root=args.artifacts,
        grader_repo=args.grader_repo,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


def grade_repository(
    repo: Path,
    *,
    artifact_root: Path | None = None,
    grader_repo: Path | None = None,
) -> dict[str, object]:
    repo = repo.resolve()
    spec_repo = (grader_repo or repo).resolve()
    spec_path = spec_repo / "eval" / "expected-artifacts.json"
    with spec_path.open(encoding="utf-8") as handle:
        spec = json.load(handle)
    case = load_case_metadata(repo, grader_repo=spec_repo)
    checks = [run_check(repo, check) for check in spec.get("checks", ())]
    if artifact_root is not None:
        checks.extend(run_artifact_checks(repo, artifact_root.resolve(), spec, case))
    return {
        "schema_version": 1,
        "grader": "local-demo-v1",
        "case_id": spec.get("case_id"),
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }


def load_case_metadata(
    repo: Path, *, grader_repo: Path | None = None
) -> dict[str, Any]:
    paths = [repo / "eval" / "case.json", repo.parent / "case.json"]
    if grader_repo is not None:
        paths.extend(
            [
                grader_repo / "eval" / "case.json",
                grader_repo.parent / "case.json",
            ]
        )
    for path in paths:
        if path.is_file():
            with path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                return payload
    return {}


def run_check(repo: Path, check: Mapping[str, Any]) -> dict[str, object]:
    kind = check.get("kind")
    check_id = str(check.get("id", kind))
    try:
        if kind == "files_exist":
            return files_exist(repo, check_id, check)
        if kind == "files_absent":
            return files_absent(repo, check_id, check)
        if kind == "file_contains":
            return file_contains(repo, check_id, check)
        if kind == "file_not_contains":
            return file_not_contains(repo, check_id, check)
        if kind == "json_field_equals":
            return json_field_equals(repo, check_id, check)
        if kind == "jsonl_record_matches":
            return jsonl_record_matches(repo, check_id, check)
        if kind == "plan_status":
            return plan_status(repo, check_id, check)
        if kind == "task_source_parse":
            return task_source_parse(repo, check_id, check)
        if kind == "generated_cache":
            return generated_cache(repo, check_id, check)
        if kind == "unittest":
            return unittest_check(repo, check_id, check)
        return failed(check_id, f"unsupported check kind: {kind}")
    # Check dispatch deliberately isolates fixture-authored validation failures
    # so one failed check can be reported beside the rest of the grader output.
    except Exception as exc:
        return failed(check_id, f"{type(exc).__name__}: {exc}")


def files_exist(
    repo: Path,
    check_id: str,
    check: Mapping[str, Any],
) -> dict[str, object]:
    missing = [
        path for path in string_list(check.get("paths")) if not (repo / path).exists()
    ]
    if missing:
        return failed(check_id, "missing paths: " + ", ".join(missing))
    return passed(check_id)


def files_absent(
    repo: Path,
    check_id: str,
    check: Mapping[str, Any],
) -> dict[str, object]:
    present = [
        path for path in string_list(check.get("paths")) if (repo / path).exists()
    ]
    if present:
        return failed(check_id, "unexpected paths: " + ", ".join(present))
    return passed(check_id)


def file_contains(
    repo: Path,
    check_id: str,
    check: Mapping[str, Any],
) -> dict[str, object]:
    path = required_string(check, "path")
    text = required_string(check, "text")
    content = (repo / path).read_text(encoding="utf-8")
    if text not in content:
        return failed(check_id, f"{path} does not contain expected text")
    return passed(check_id)


def file_not_contains(
    repo: Path,
    check_id: str,
    check: Mapping[str, Any],
) -> dict[str, object]:
    path = required_string(check, "path")
    text = required_string(check, "text")
    content = (repo / path).read_text(encoding="utf-8")
    if text in content:
        return failed(check_id, f"{path} contains forbidden text")
    return passed(check_id)


def json_field_equals(
    repo: Path,
    check_id: str,
    check: Mapping[str, Any],
) -> dict[str, object]:
    path = required_string(check, "path")
    field_path = required_string(check, "field")
    expected = check.get("equals")
    with (repo / path).open(encoding="utf-8") as handle:
        value = json.load(handle)
    actual = nested_value(value, field_path)
    if actual != expected:
        return failed(check_id, f"{field_path} is {actual!r}, expected {expected!r}")
    return passed(check_id)


def jsonl_record_matches(
    repo: Path,
    check_id: str,
    check: Mapping[str, Any],
) -> dict[str, object]:
    path = required_string(check, "path")
    expected = check.get("fields")
    if not isinstance(expected, Mapping) or not expected:
        return failed(check_id, "fields must be a non-empty object")
    records = read_jsonl_records(repo / path)
    for record in records:
        if all(
            nested_value(record, field) == value for field, value in expected.items()
        ):
            return passed(check_id)
    return failed(check_id, f"no JSONL record matched fields in {path}")


def plan_status(
    repo: Path,
    check_id: str,
    check: Mapping[str, Any],
) -> dict[str, object]:
    task_id = required_string(check, "task_id")
    expected = required_string(check, "status")
    config = TaskSourceConfig(plan_path=str(check.get("plan_path", "PLAN.md")))
    task = build_task_source(repo, config).probe(task_id)
    if task is None:
        return failed(check_id, f"task not found: {task_id}")
    if task.status != expected:
        return failed(check_id, f"{task_id} is {task.status}, expected {expected}")
    return passed(check_id)


def task_source_parse(
    repo: Path,
    check_id: str,
    check: Mapping[str, Any],
) -> dict[str, object]:
    config_payload = check.get("config")
    if not isinstance(config_payload, Mapping):
        return failed(check_id, "config must be an object")
    runnable_statuses = string_list(config_payload.get("runnable_statuses"))
    config = TaskSourceConfig(
        type=str(config_payload.get("type", "markdown-plan")),
        plan_path=config_payload.get("plan_path"),
        profile=config_payload.get("profile"),
        runnable_statuses=tuple(runnable_statuses)
        if runnable_statuses
        else TaskSourceConfig().runnable_statuses,
    )
    tasks = build_task_source(repo, config).list_tasks()
    expected_ids = string_list(check.get("task_ids"))
    actual_ids = [task.task_id for task in tasks]
    if actual_ids != expected_ids:
        return failed(
            check_id, f"task ids are {actual_ids!r}, expected {expected_ids!r}"
        )
    return passed(check_id)


def generated_cache(
    repo: Path,
    check_id: str,
    check: Mapping[str, Any],
) -> dict[str, object]:
    path = required_string(check, "path")
    with (repo / path).open(encoding="utf-8") as handle:
        cache = json.load(handle)
    if cache.get("status") != "profile":
        return failed(check_id, f"generated cache status is {cache.get('status')!r}")
    profile = cache.get("profile")
    if not isinstance(profile, dict):
        return failed(check_id, "generated cache profile must be an object")
    unsafe_profile_path = first_unsafe_profile_source_path(repo, profile)
    if unsafe_profile_path is not None:
        return failed(
            check_id,
            f"generated profile source path is unsafe: {unsafe_profile_path}",
        )
    forbidden = sorted(find_forbidden_generated_command_keys(profile))
    if forbidden:
        return failed(
            check_id, "generated cache has forbidden fields: " + ", ".join(forbidden)
        )
    fingerprints = cache.get("source_fingerprints")
    if not isinstance(fingerprints, list) or not fingerprints:
        return failed(check_id, "generated cache source_fingerprints must be non-empty")
    for entry in fingerprints:
        if not isinstance(entry, Mapping):
            return failed(
                check_id, "generated cache fingerprint entries must be objects"
            )
        source_path = entry.get("path")
        if not isinstance(source_path, str):
            return failed(check_id, "generated cache fingerprint path is required")
        current = safe_repo_file(repo, source_path)
        if current is None or not current.is_file():
            return failed(check_id, f"generated source missing: {source_path}")
        if entry.get("sha256") != sha256_file(current):
            return failed(
                check_id, f"generated source fingerprint stale: {source_path}"
            )
    task_ids = string_list(check.get("task_ids"))
    if task_ids:
        source = build_task_source(
            repo,
            TaskSourceConfig(
                type="markdown-profile",
                profile=profile,
                runnable_statuses=tuple(string_list(check.get("runnable_statuses")))
                or TaskSourceConfig().runnable_statuses,
            ),
        )
        actual_ids = [task.task_id for task in source.list_tasks()]
        if actual_ids != task_ids:
            return failed(
                check_id, f"task ids are {actual_ids!r}, expected {task_ids!r}"
            )
    return passed(check_id)


def unittest_check(
    repo: Path,
    check_id: str,
    check: Mapping[str, Any],
) -> dict[str, object]:
    start_dir = str(check.get("start_dir", "tests"))
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(repo / "src"),
    }
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", start_dir],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return failed(
            check_id,
            (
                "unittest failed: "
                + truncate(completed.stdout)
                + truncate(completed.stderr)
            ),
        )
    return passed(check_id)


def run_artifact_checks(
    repo: Path,
    artifact_root: Path,
    spec: Mapping[str, Any],
    case: Mapping[str, Any],
) -> list[dict[str, object]]:
    record = safe_load_json(artifact_root / "run.json")
    if not isinstance(record, Mapping):
        return [failed("artifact-run-record-schema", "run.json must contain an object")]
    required_roles = frozenset(string_list(case.get("expected_artifact_roles")))
    schema_diagnostics = validate_skill_eval_run_record(
        record,
        artifact_root,
        required_artifact_roles=required_roles,
    )
    if schema_diagnostics:
        return [
            failed(
                "artifact-run-record-schema",
                "; ".join(schema_diagnostics),
            )
        ]
    checks = [
        passed("artifact-run-record-schema"),
        artifact_identity(record, spec, case),
        artifact_required_roles(record, case),
        artifact_budget(record, case),
        artifact_workflow_trace(artifact_root, record, spec),
        artifact_orchestrated_delegation(artifact_root, record, spec),
        artifact_case_contract(repo, artifact_root, spec),
    ]
    if spec.get("negative_prompts") is not None:
        checks.append(artifact_negative_prompt_results(artifact_root, record, spec))
    return checks


def artifact_identity(
    record: Mapping[str, Any],
    spec: Mapping[str, Any],
    case: Mapping[str, Any],
) -> dict[str, object]:
    expected_case_id = spec.get("case_id")
    expected_conditions = set(string_list(case.get("conditions")))
    if record.get("suite_id") != EXAMPLE_SUITE_ID:
        return failed(
            "artifact-identity", "run.json suite_id does not match local-demo-v1"
        )
    if record.get("case_id") != expected_case_id:
        return failed("artifact-identity", "run.json case_id does not match fixture")
    if expected_conditions and record.get("condition") not in expected_conditions:
        return failed(
            "artifact-identity", "run.json condition is not declared for case"
        )
    task = record.get("task")
    if isinstance(task, Mapping) and case.get("task_id") is not None:
        if task.get("id") != case.get("task_id"):
            return failed("artifact-identity", "run.json task.id does not match case")
    return passed("artifact-identity")


def artifact_required_roles(
    record: Mapping[str, Any],
    case: Mapping[str, Any],
) -> dict[str, object]:
    expected_roles = set(string_list(case.get("expected_artifact_roles")))
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, Sequence) or isinstance(artifacts, (str, bytes)):
        return failed("artifact-required-roles", "run.json artifacts must be a list")
    actual_roles = {
        artifact.get("role")
        for artifact in artifacts
        if isinstance(artifact, Mapping)
        and isinstance(artifact.get("role"), str)
        and artifact.get("required") is True
    }
    missing = sorted(expected_roles - actual_roles)
    if missing:
        return failed(
            "artifact-required-roles", "missing artifact roles: " + ", ".join(missing)
        )
    return passed("artifact-required-roles")


def artifact_budget(
    record: Mapping[str, Any],
    case: Mapping[str, Any],
) -> dict[str, object]:
    budget = record.get("budget")
    expected = case.get("budget")
    if not isinstance(budget, Mapping) or not isinstance(expected, Mapping):
        return failed("artifact-budget", "budget metadata is missing")
    for key in ("timeout_seconds", "max_commands", "max_output_bytes"):
        actual_value = budget.get(key)
        expected_value = expected.get(key)
        if isinstance(actual_value, int) and isinstance(expected_value, int):
            if actual_value > expected_value:
                return failed("artifact-budget", f"{key} exceeds case budget")
    return passed("artifact-budget")


def artifact_workflow_trace(
    artifact_root: Path,
    record: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> dict[str, object]:
    expectations = spec.get("workflow_trace")
    if not isinstance(expectations, Mapping):
        return passed("artifact-workflow-trace")
    ordered_events = load_ordered_workflow_events(artifact_root, record)
    events = set(ordered_events)
    required = set(string_list(expectations.get("required")))
    required.update(orchestrated_required_events(record, required))
    forbidden = set(string_list(expectations.get("forbidden")))
    missing = sorted(required - events)
    present_forbidden = sorted(forbidden & events)
    messages = []
    if missing:
        messages.append("missing events: " + ", ".join(missing))
    if present_forbidden:
        messages.append("forbidden events: " + ", ".join(present_forbidden))
    messages.extend(orchestrated_order_diagnostics(record, ordered_events, required))
    if messages:
        return failed("artifact-workflow-trace", "; ".join(messages))
    return passed("artifact-workflow-trace")


def orchestrated_required_events(
    record: Mapping[str, Any],
    base_required: set[str],
) -> set[str]:
    task = record.get("task")
    if (
        record.get("condition") != "orchestrated_vibe_loop"
        or not isinstance(task, Mapping)
        or task.get("should_trigger") is not True
    ):
        return set()
    required = {
        "exploration_delegated",
        "implementation_delegated",
        "review_delegated",
    }
    if (
        "review_finding_addressed" in base_required
        or "rereview_requested" in base_required
    ):
        required.add("remediation_delegated")
    return required


def artifact_orchestrated_delegation(
    artifact_root: Path,
    record: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> dict[str, object]:
    expectations = spec.get("workflow_trace")
    base_required = (
        set(string_list(expectations.get("required")))
        if isinstance(expectations, Mapping)
        else set()
    )
    required_roles = orchestrated_required_roles(record, base_required)
    if not required_roles:
        return passed("artifact-orchestrated-delegation")
    path = artifact_path_for_role(artifact_root, record, "delegation_evidence")
    if path is None or not path.is_file():
        return failed(
            "artifact-orchestrated-delegation",
            "delegation_evidence missing",
        )
    payload = safe_load_json(path)
    if not isinstance(payload, Mapping):
        return failed(
            "artifact-orchestrated-delegation",
            "delegation_evidence must be an object",
        )
    agents = list_of_mappings(payload.get("agents"))
    diagnostics: list[str] = []
    for role in sorted(required_roles):
        matches = [agent for agent in agents if agent.get("role") == role]
        if not matches:
            diagnostics.append(f"delegation role missing: {role}")
            continue
        agent = matches[0]
        for field in ("agent_id", "prompt", "result"):
            if not isinstance(agent.get(field), str) or not agent[field]:
                diagnostics.append(f"{role}.{field} is required")
        if role in {"implementer", "remediator"} and not string_list(
            agent.get("changed_paths")
        ):
            diagnostics.append(f"{role}.changed_paths is required")
    if diagnostics:
        return failed("artifact-orchestrated-delegation", "; ".join(diagnostics))
    return passed("artifact-orchestrated-delegation")


def orchestrated_required_roles(
    record: Mapping[str, Any],
    base_required: set[str],
) -> set[str]:
    if not is_positive_orchestrated_record(record):
        return set()
    required = {"explorer", "implementer", "reviewer"}
    if (
        "review_finding_addressed" in base_required
        or "rereview_requested" in base_required
    ):
        required.add("remediator")
    return required


def orchestrated_order_diagnostics(
    record: Mapping[str, Any],
    events: list[str],
    required: set[str],
) -> list[str]:
    if not is_positive_orchestrated_record(record):
        return []
    sequences = [
        ("exploration_delegated", "implementation_delegated"),
        ("implementation_delegated", "verification_ran"),
        ("verification_ran", "review_delegated"),
        ("review_delegated", "commit_created"),
    ]
    if "remediation_delegated" in required:
        sequences.extend(
            [
                ("review_finding_received", "remediation_delegated"),
                ("remediation_delegated", "review_finding_addressed"),
                ("review_finding_addressed", "rereview_requested"),
            ]
        )
    diagnostics = []
    for before, after in sequences:
        if (
            before in events
            and after in events
            and events.index(before) > events.index(after)
        ):
            diagnostics.append(f"workflow event order missing: {before} -> {after}")
    return diagnostics


def is_positive_orchestrated_record(record: Mapping[str, Any]) -> bool:
    task = record.get("task")
    return (
        record.get("condition") == "orchestrated_vibe_loop"
        and isinstance(task, Mapping)
        and task.get("should_trigger") is True
    )


def artifact_case_contract(
    repo: Path,
    artifact_root: Path,
    spec: Mapping[str, Any],
) -> dict[str, object]:
    contract = spec.get("artifact_contract")
    if not isinstance(contract, Mapping):
        return passed("artifact-case-contract")
    diagnostics: list[str] = []
    diagnostics.extend(check_preserved_files(repo, contract))
    record = safe_load_json(artifact_root / "run.json")
    if not isinstance(record, Mapping):
        diagnostics.append("run.json must contain an object")
        return failed("artifact-case-contract", "; ".join(diagnostics))
    diagnostics.extend(check_event_order(artifact_root, record, contract))
    diagnostics.extend(check_artifact_json_fields(artifact_root, record, contract))
    if diagnostics:
        return failed("artifact-case-contract", "; ".join(diagnostics))
    return passed("artifact-case-contract")


def check_preserved_files(repo: Path, contract: Mapping[str, Any]) -> list[str]:
    diagnostics: list[str] = []
    for item in list_of_mappings(contract.get("preserved_files")):
        path = item.get("path")
        contains = item.get("contains")
        if not isinstance(path, str):
            diagnostics.append("preserved file path is required")
            continue
        file_path = repo / path
        if not file_path.is_file():
            diagnostics.append(f"preserved file missing: {path}")
            continue
        if isinstance(contains, str):
            content = file_path.read_text(encoding="utf-8", errors="replace")
            if contains not in content:
                diagnostics.append(f"preserved file content missing: {path}")
        equals = item.get("equals")
        if isinstance(equals, str):
            content = file_path.read_text(encoding="utf-8", errors="replace")
            if content != equals:
                diagnostics.append(f"preserved file content changed: {path}")
    return diagnostics


def check_event_order(
    artifact_root: Path,
    record: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> list[str]:
    sequence = string_list(contract.get("event_order"))
    if not sequence:
        return []
    events = load_ordered_workflow_events(artifact_root, record)
    position = 0
    for expected in sequence:
        try:
            position = events.index(expected, position) + 1
        except ValueError:
            return ["workflow event order missing: " + " -> ".join(sequence)]
    return []


def check_artifact_json_fields(
    artifact_root: Path,
    record: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> list[str]:
    diagnostics: list[str] = []
    for item in list_of_mappings(contract.get("artifact_json_fields")):
        role = item.get("role")
        field = item.get("field")
        expected = item.get("equals")
        if not isinstance(role, str) or not isinstance(field, str):
            diagnostics.append("artifact JSON field check requires role and field")
            continue
        path = artifact_path_for_role(artifact_root, record, role)
        if path is None or not path.is_file():
            diagnostics.append(f"artifact role missing for JSON field check: {role}")
            continue
        payload = safe_load_json(path)
        if not isinstance(payload, Mapping):
            diagnostics.append(f"artifact role is not a JSON object: {role}")
            continue
        actual = nested_value(payload, field)
        contains = item.get("contains")
        if isinstance(contains, str):
            if contains not in str(actual):
                diagnostics.append(
                    f"{role}.{field} is {actual!r}, expected to contain {contains!r}"
                )
            continue
        if actual != expected:
            diagnostics.append(f"{role}.{field} is {actual!r}, expected {expected!r}")
    return diagnostics


def artifact_negative_prompt_results(
    artifact_root: Path,
    record: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> dict[str, object]:
    result_path = artifact_path_for_role(
        artifact_root, record, "negative_prompt_results"
    )
    if result_path is None or not result_path.is_file():
        return failed(
            "artifact-negative-prompts", "negative-prompt-results.json missing"
        )
    payload = safe_load_json(result_path)
    results = payload.get("results") if isinstance(payload, Mapping) else None
    if not isinstance(results, list):
        return failed("artifact-negative-prompts", "results must be a list")
    by_id = {
        item.get("id"): item
        for item in results
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    for prompt in spec.get("negative_prompts", ()):
        if not isinstance(prompt, Mapping):
            continue
        prompt_id = prompt.get("id")
        if not isinstance(prompt_id, str):
            continue
        result = by_id.get(prompt_id)
        if not isinstance(result, Mapping):
            return failed(
                "artifact-negative-prompts", f"missing result for {prompt_id}"
            )
        if result.get("skill_activated") is not False:
            return failed("artifact-negative-prompts", f"{prompt_id} activated a skill")
        if result.get("repository_changed") is not bool(
            prompt.get("allows_repo_change", False)
        ):
            return failed(
                "artifact-negative-prompts", f"{prompt_id} repository_changed mismatch"
            )
        response = result.get("response", "")
        terms = string_list(prompt.get("response_terms"))
        if terms and not any(term in str(response) for term in terms):
            return failed(
                "artifact-negative-prompts", f"{prompt_id} response terms missing"
            )
    return passed("artifact-negative-prompts")


def read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def load_workflow_events(
    artifact_root: Path,
    record: Mapping[str, Any],
) -> set[str]:
    return set(load_ordered_workflow_events(artifact_root, record))


def load_ordered_workflow_events(
    artifact_root: Path,
    record: Mapping[str, Any],
) -> list[str]:
    path = artifact_path_for_role(artifact_root, record, "workflow_events")
    if path is None:
        path = artifact_root / "workflow-events.json"
    if not path.is_file():
        return []
    payload = safe_load_json(path)
    raw_events = payload.get("events") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
        return []
    events: list[str] = []
    for event in raw_events:
        if isinstance(event, str):
            events.append(event)
        elif isinstance(event, Mapping) and isinstance(event.get("event"), str):
            events.append(event["event"])
    return events


def artifact_path_for_role(
    artifact_root: Path,
    record: Mapping[str, Any],
    role: str,
) -> Path | None:
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, Sequence) or isinstance(artifacts, (str, bytes)):
        return None
    for artifact in artifacts:
        if not isinstance(artifact, Mapping) or artifact.get("role") != role:
            continue
        path = artifact.get("path")
        if isinstance(path, str):
            return safe_artifact_path(artifact_root, path)
    return None


def safe_artifact_path(artifact_root: Path, relative_path: str) -> Path | None:
    if path_diagnostics("artifact", relative_path):
        return None
    path = Path(relative_path)
    if has_symlink_component(artifact_root, path):
        return None
    resolved = (artifact_root / path).resolve()
    try:
        resolved_relative = resolved.relative_to(artifact_root.resolve()).as_posix()
    except ValueError:
        return None
    if is_secret_like_eval_path(resolved_relative):
        return None
    return resolved


def safe_repo_file(repo: Path, relative_path: str) -> Path | None:
    if path_diagnostics("source fingerprint", relative_path):
        return None
    path = Path(relative_path)
    if has_symlink_component(repo, path):
        return None
    resolved = (repo / path).resolve()
    try:
        resolved.relative_to(repo.resolve())
    except ValueError:
        return None
    if is_secret_like_eval_path(path.as_posix()):
        return None
    return resolved


def first_unsafe_profile_source_path(
    repo: Path, profile: Mapping[str, Any]
) -> str | None:
    source_paths = profile.get("source_paths")
    if not isinstance(source_paths, Sequence) or isinstance(source_paths, (str, bytes)):
        return "source_paths"
    for source_path in source_paths:
        if not isinstance(source_path, str):
            return str(source_path)
        resolved = safe_repo_file(repo, source_path)
        if resolved is None or not resolved.is_file():
            return source_path
    return None


def safe_load_json(path: Path) -> object:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def nested_value(value: object, field_path: str) -> object:
    current = value
    for part in field_path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def string_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, str)]


def list_of_mappings(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def required_string(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def passed(check_id: str) -> dict[str, object]:
    return {"id": check_id, "passed": True}


def failed(check_id: str, message: str) -> dict[str, object]:
    return {"id": check_id, "passed": False, "message": message}


def truncate(value: str, limit: int = 2000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "...[truncated]"


if __name__ == "__main__":
    raise SystemExit(main())
