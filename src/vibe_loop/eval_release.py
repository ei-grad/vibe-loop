from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from vibe_loop.eval_examples import EXAMPLE_SUITE_ID, list_eval_example_cases


RELEASE_READINESS_SCHEMA_VERSION = 1
RELEASE_READINESS_RECORD_TYPE = "skill_release_readiness"
WORKFLOW_REGRESSION_FLAG = "workflow_contract_regression"
DEFAULT_RELEASE_GATE_TRIALS = 1
RELEASE_GATE_CASE_CONDITIONS: Mapping[str, tuple[str, ...]] = {
    "command-hooks-task-source": ("vibe_loop_cli",),
    "dirty-main-worktree": ("vibe_loop",),
    "explicit-list-profile": ("vibe_loop",),
    "finite-py-plan-table": ("vibe_loop", "orchestrated_vibe_loop"),
    "generated-roadmap-profile": ("vibe_loop",),
    "integration-lock-unavailable": ("vibe_loop_cli",),
    "kiro-user-story": ("vibe_loop",),
    "locked-task-selection": ("vibe_loop",),
    "main-advanced-before-merge": ("vibe_loop",),
    "main-integration-lock": ("vibe_loop_cli",),
    "negative-trigger-set": ("vibe_loop",),
    "openspec-user-story": ("vibe_loop",),
    "review-remediation": ("vibe_loop", "orchestrated_vibe_loop"),
    "supervised-worker-report": ("vibe_loop_cli",),
    "spec-kit-user-story": ("vibe_loop",),
    "workspace-duplicate-worktree": ("vibe_loop_cli",),
    "workspace-foreign-dirty": ("vibe_loop_cli",),
    "workspace-merged-branch": ("vibe_loop_cli",),
    "workspace-missing-worktree": ("vibe_loop_cli",),
}
EXTERNAL_SUMMARY_STRING_LIMIT = 240
EXTERNAL_SUMMARY_ITEM_LIMIT = 20
SENSITIVE_EXTERNAL_SUMMARY_KEY_FRAGMENTS = (
    "command",
    "log",
    "message",
    "output",
    "prompt",
    "secret",
    "stderr",
    "stdout",
    "token",
    "transcript",
)


def load_json_mapping(path: Path) -> Mapping[str, object]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"cannot read JSON mapping from {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"expected JSON object in {path}")
    return payload


def release_gate_case_conditions(
    *,
    cases: Sequence[str] = (),
    conditions: Sequence[str] = (),
) -> dict[str, tuple[str, ...]]:
    declared_by_case = {
        case.case_id: case.conditions for case in list_eval_example_cases()
    }
    selected_cases = tuple(cases) or tuple(RELEASE_GATE_CASE_CONDITIONS)
    unknown_cases = sorted(set(selected_cases) - set(declared_by_case))
    if unknown_cases:
        raise ValueError("unknown eval case(s): " + ", ".join(unknown_cases))

    matrix: dict[str, tuple[str, ...]] = {}
    for case_id in selected_cases:
        declared = declared_by_case[case_id]
        selected_conditions = (
            tuple(conditions)
            if conditions
            else RELEASE_GATE_CASE_CONDITIONS.get(case_id, ("vibe_loop",))
        )
        unknown_conditions = sorted(set(selected_conditions) - set(declared))
        if unknown_conditions:
            raise ValueError(
                f"{case_id} does not declare condition(s): "
                + ", ".join(unknown_conditions)
            )
        matrix[case_id] = tuple(
            condition for condition in declared if condition in set(selected_conditions)
        )
    return matrix


def normalized_required_case_conditions(
    required_case_conditions: Mapping[str, Sequence[str]] | None,
) -> dict[str, tuple[str, ...]]:
    if required_case_conditions is None:
        return release_gate_case_conditions()
    return {
        str(case_id): tuple(str(condition) for condition in conditions)
        for case_id, conditions in required_case_conditions.items()
    }


def parse_parked_regression_specs(specs: Sequence[str]) -> dict[str, list[str]]:
    parked: dict[str, list[str]] = {}
    for spec in specs:
        regression_id, separator, task_id = spec.partition("=")
        regression_id = regression_id.strip()
        task_id = task_id.strip()
        if not separator or not regression_id or not task_id:
            raise ValueError("parked regression must use REGRESSION_ID=TASK_ID format")
        parked.setdefault(regression_id, [])
        if task_id not in parked[regression_id]:
            parked[regression_id].append(task_id)
    return parked


def load_external_benchmark_evidence(paths: Sequence[Path]) -> list[dict[str, object]]:
    return [external_benchmark_evidence(path) for path in paths]


def external_benchmark_evidence(path: Path) -> dict[str, object]:
    payload = load_json_mapping(path)
    return {
        "path": str(path),
        "sha256": file_sha256(path),
        "size": path.stat().st_size,
        "benchmark": string_value(
            payload.get("benchmark")
            or payload.get("suite_id")
            or payload.get("dataset")
            or path.stem
        ),
        "status": string_value(payload.get("status") or payload.get("outcome"))
        or "recorded",
        "summary": external_summary(payload),
    }


def build_release_readiness_record(
    aggregate: Mapping[str, object],
    *,
    aggregate_path: Path,
    dry_run: bool,
    minimum_trials: int = DEFAULT_RELEASE_GATE_TRIALS,
    local_suite_mode: str = "existing_aggregate",
    required_case_conditions: Mapping[str, Sequence[str]] | None = None,
    parked_regressions: Mapping[str, Sequence[str]] | None = None,
    parked_workflow_regression_task_ids: Sequence[str] = (),
    external_benchmarks: Sequence[Mapping[str, object]] = (),
    generated_at: str | None = None,
) -> dict[str, object]:
    if minimum_trials < 1:
        raise ValueError("minimum trials must be at least 1")

    parked_regressions = parked_regressions or {}
    local_suite = local_suite_evidence(
        aggregate,
        aggregate_path=aggregate_path,
        minimum_trials=minimum_trials,
        mode=local_suite_mode,
        required_case_conditions=required_case_conditions,
    )
    quality_evidence_gaps = skill_quality_evidence_gaps(
        aggregate,
        required_case_conditions=required_case_conditions,
    )
    trial_failures = release_trial_failures(
        aggregate,
        required_case_conditions=required_case_conditions,
    )
    regressions = workflow_contract_regressions(aggregate)
    regression_ids = {string_value(regression.get("id")) for regression in regressions}
    invalid_parked_ids = sorted(set(parked_regressions) - regression_ids)
    annotated_regressions = annotate_regressions(
        regressions,
        parked_regressions=parked_regressions,
        parked_workflow_regression_task_ids=parked_workflow_regression_task_ids,
    )
    unresolved_regressions = [
        regression
        for regression in annotated_regressions
        if not regression.get("parked_task_ids")
    ]
    blockers = release_blockers(
        local_suite=local_suite,
        quality_evidence_gaps=quality_evidence_gaps,
        trial_failures=trial_failures,
        unresolved_regressions=unresolved_regressions,
        invalid_parked_ids=invalid_parked_ids,
    )
    status = "passed" if not blockers else "blocked"
    external_benchmarks = [dict(item) for item in external_benchmarks]
    record = {
        "schema_version": RELEASE_READINESS_SCHEMA_VERSION,
        "record_type": RELEASE_READINESS_RECORD_TYPE,
        "generated_at": generated_at or utc_now(),
        "dry_run": dry_run,
        "status": status,
        "gate": {
            "name": "bundled_skill_release_readiness",
            "minimum_trials_per_case_condition": minimum_trials,
            "required_case_conditions": {
                case_id: list(conditions)
                for case_id, conditions in sorted(
                    normalized_required_case_conditions(
                        required_case_conditions
                    ).items()
                )
            },
            "required_suite_id": EXAMPLE_SUITE_ID,
            "blockers": blockers,
        },
        "local_suite": local_suite,
        "trial_failures": {
            "status": "passed" if not trial_failures else "blocked",
            "total": len(trial_failures),
            "records": trial_failures,
        },
        "workflow_contract_regressions": {
            "evidence_status": "passed" if not quality_evidence_gaps else "blocked",
            "evidence_gaps": quality_evidence_gaps,
            "total": len(annotated_regressions),
            "parked": [
                regression
                for regression in annotated_regressions
                if regression.get("parked_task_ids")
            ],
            "unresolved": unresolved_regressions,
            "invalid_parked_ids": invalid_parked_ids,
        },
        "external_benchmarks": {
            "required": False,
            "status": "recorded" if external_benchmarks else "optional_not_provided",
            "records": external_benchmarks,
        },
    }
    record["checklist"] = release_checklist(record)
    return record


def write_release_readiness_record(path: Path, record: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def render_release_readiness_summary(record: Mapping[str, object]) -> str:
    local_suite = mapping_value(record.get("local_suite"))
    trial_failures = mapping_value(record.get("trial_failures"))
    regressions = mapping_value(record.get("workflow_contract_regressions"))
    external = mapping_value(record.get("external_benchmarks"))
    blockers = sequence_value(mapping_value(record.get("gate")).get("blockers"))
    lines = [
        f"release gate: {record.get('status')}",
        f"aggregate: {local_suite.get('aggregate_path', '')}",
        (
            "local suite: "
            f"{local_suite.get('suite_id', '')} "
            f"trials={local_suite.get('total_trials', 0)} "
            f"coverage={local_suite.get('coverage_status', '')}"
        ),
        (
            "workflow regressions: "
            f"total={regressions.get('total', 0)} "
            f"parked={len(sequence_value(regressions.get('parked')))} "
            f"unresolved={len(sequence_value(regressions.get('unresolved')))}"
        ),
        f"trial failures: {trial_failures.get('total', 0)}",
        (
            "external benchmarks: "
            f"{len(sequence_value(external.get('records')))} "
            f"({external.get('status', '')})"
        ),
    ]
    if blockers:
        lines.append("blockers:")
        for blocker in blockers:
            if isinstance(blocker, Mapping):
                count = blocker.get("count")
                suffix = f" count={count}" if isinstance(count, int) else ""
                lines.append(f"- {blocker.get('id', 'unknown')}{suffix}")
    lines.extend(render_coverage_gap_summary(local_suite.get("coverage_gaps")))
    lines.extend(render_trial_failure_summary(trial_failures.get("records")))
    lines.extend(render_evidence_gap_summary(regressions.get("evidence_gaps")))
    lines.extend(render_unresolved_regression_summary(regressions.get("unresolved")))
    invalid_parked_ids = [
        item
        for item in sequence_value(regressions.get("invalid_parked_ids"))
        if isinstance(item, str)
    ]
    if invalid_parked_ids:
        lines.append("invalid parked regression ids: " + ", ".join(invalid_parked_ids))
    return "\n".join(lines) + "\n"


def local_suite_evidence(
    aggregate: Mapping[str, object],
    *,
    aggregate_path: Path,
    minimum_trials: int,
    mode: str,
    required_case_conditions: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, object]:
    required = normalized_required_case_conditions(required_case_conditions)
    coverage = local_suite_coverage(
        aggregate,
        minimum_trials=minimum_trials,
        required_case_conditions=required,
    )
    return {
        "mode": mode,
        "suite_id": string_value(aggregate.get("suite_id")),
        "generated_at": string_value(aggregate.get("generated_at")),
        "aggregate_path": str(aggregate_path),
        "aggregate_sha256": file_sha256(aggregate_path)
        if aggregate_path.exists()
        else "",
        "artifact_root": string_value(aggregate.get("artifact_root")),
        "total_trials": integer_value(aggregate.get("total_trials")),
        "conditions": condition_trial_counts(aggregate),
        "required_case_conditions": {
            case_id: list(conditions)
            for case_id, conditions in sorted(required.items())
        },
        "coverage_status": "passed" if not coverage else "blocked",
        "coverage_gaps": coverage,
    }


def local_suite_coverage(
    aggregate: Mapping[str, object],
    *,
    minimum_trials: int,
    required_case_conditions: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, object]]:
    required = normalized_required_case_conditions(required_case_conditions)
    gaps: list[dict[str, object]] = []
    if aggregate.get("suite_id") != EXAMPLE_SUITE_ID:
        gaps.append(
            {
                "case_id": "",
                "condition": "",
                "trials": 0,
                "required_trials": minimum_trials,
                "reason": "wrong_suite",
            }
        )
    cases = mapping_value(aggregate.get("cases"))
    for case_id, conditions in sorted(required.items()):
        case_payload = mapping_value(cases.get(case_id))
        for condition in conditions:
            condition_payload = mapping_value(case_payload.get(condition))
            trials = integer_value(condition_payload.get("trials"))
            if trials >= minimum_trials:
                continue
            gaps.append(
                {
                    "case_id": case_id,
                    "condition": condition,
                    "trials": trials,
                    "required_trials": minimum_trials,
                    "reason": "missing" if trials == 0 else "insufficient_trials",
                }
            )
    return gaps


def condition_trial_counts(aggregate: Mapping[str, object]) -> dict[str, int]:
    conditions = mapping_value(aggregate.get("conditions"))
    return {
        str(condition): integer_value(mapping_value(payload).get("trials"))
        for condition, payload in sorted(conditions.items())
    }


def workflow_contract_regressions(
    aggregate: Mapping[str, object],
) -> list[dict[str, object]]:
    quality = mapping_value(aggregate.get("skill_quality"))
    regressions: list[dict[str, object]] = []
    comparisons = mapping_value(quality.get("condition_comparisons"))
    for condition, payload in sorted(comparisons.items()):
        comparison = mapping_value(payload)
        flags = string_list(comparison.get("regression_flags"))
        if WORKFLOW_REGRESSION_FLAG not in flags:
            continue
        regressions.append(
            {
                "id": f"condition_comparison:{condition}",
                "source": "condition_comparison",
                "condition": str(condition),
                "flag": WORKFLOW_REGRESSION_FLAG,
                "deltas": workflow_delta_summary(comparison.get("deltas")),
                "records": release_record_refs(comparison.get("condition_records")),
                "baseline_records": release_record_refs(
                    comparison.get("baseline_records")
                ),
            }
        )

    prior = sequence_value(quality.get("prior_run_regressions"))
    for index, payload in enumerate(prior, start=1):
        regression = mapping_value(payload)
        flags = string_list(regression.get("regression_flags"))
        if WORKFLOW_REGRESSION_FLAG not in flags:
            continue
        condition = string_value(regression.get("condition")) or "unknown"
        regressions.append(
            {
                "id": f"prior_run:{condition}:{index}",
                "source": "prior_run_regression",
                "condition": condition,
                "flag": WORKFLOW_REGRESSION_FLAG,
                "previous_generated_at": string_value(
                    regression.get("previous_generated_at")
                ),
                "deltas": workflow_delta_summary(regression.get("deltas")),
                "records": release_record_refs(regression.get("records")),
                "previous_records": release_record_refs(
                    regression.get("previous_records")
                ),
            }
        )
    return regressions


def skill_quality_evidence_gaps(
    aggregate: Mapping[str, object],
    *,
    required_case_conditions: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, object]]:
    quality = aggregate.get("skill_quality")
    if not isinstance(quality, Mapping):
        return [
            {
                "id": "missing_skill_quality",
                "message": "aggregate is missing skill_quality evidence",
            }
        ]

    gaps: list[dict[str, object]] = []
    required_conditions = sorted(
        {
            condition
            for conditions in normalized_required_case_conditions(
                required_case_conditions
            ).values()
            for condition in conditions
        }
    )
    baseline_condition = string_value(quality.get("baseline_condition")) or "no_skill"
    condition_summaries = quality.get("conditions")
    if not isinstance(condition_summaries, Mapping):
        gaps.append(
            {
                "id": "missing_condition_summaries",
                "message": "skill_quality is missing condition summaries",
            }
        )
        condition_summaries = {}
    for condition in required_conditions:
        summary = condition_summaries.get(condition)
        if not isinstance(summary, Mapping):
            gaps.append(
                {
                    "id": "missing_condition_summary",
                    "condition": condition,
                    "message": f"skill_quality is missing summary for {condition}",
                }
            )
            continue
        for field in ("workflow_score_mean", "workflow_violation_rate", "records"):
            if field not in summary:
                gaps.append(
                    {
                        "id": "missing_condition_summary_field",
                        "condition": condition,
                        "field": field,
                        "message": (
                            f"skill_quality summary for {condition} is missing {field}"
                        ),
                    }
                )

    comparisons = quality.get("condition_comparisons")
    if not isinstance(comparisons, Mapping):
        comparisons = {}

    comparison_conditions = expected_skill_comparison_conditions(
        aggregate,
        baseline_condition=baseline_condition,
    )
    for condition in comparison_conditions:
        comparison = comparisons.get(condition)
        if not isinstance(comparison, Mapping):
            continue
        regression_flags = comparison.get("regression_flags")
        if not is_sequence_payload(regression_flags):
            gaps.append(
                {
                    "id": "missing_regression_flags",
                    "condition": condition,
                    "message": f"comparison for {condition} is missing regression flags",
                }
            )
        elif not all(isinstance(flag, str) for flag in regression_flags):
            gaps.append(
                {
                    "id": "invalid_regression_flags",
                    "condition": condition,
                    "message": (
                        f"comparison for {condition} has non-string regression flags"
                    ),
                }
            )
        deltas = comparison.get("deltas")
        if not isinstance(deltas, Mapping):
            gaps.append(
                {
                    "id": "missing_comparison_deltas",
                    "condition": condition,
                    "message": f"comparison for {condition} is missing deltas",
                }
            )
        elif not (
            "workflow_score_mean" in deltas and "workflow_violation_rate" in deltas
        ):
            gaps.append(
                {
                    "id": "missing_workflow_deltas",
                    "condition": condition,
                    "message": f"comparison for {condition} is missing workflow deltas",
                }
            )

    failure_categories = quality.get("failure_categories")
    if not isinstance(failure_categories, Mapping):
        gaps.append(
            {
                "id": "missing_failure_categories",
                "message": "skill_quality is missing failure category evidence",
            }
        )
    elif not isinstance(failure_categories.get("workflow_contract_failures"), Mapping):
        gaps.append(
            {
                "id": "missing_workflow_failure_category",
                "message": "skill_quality is missing workflow-contract failure evidence",
            }
        )

    prior_run_regressions = quality.get("prior_run_regressions")
    if not is_sequence_payload(prior_run_regressions):
        gaps.append(
            {
                "id": "missing_prior_run_regressions",
                "message": "skill_quality is missing prior-run regression evidence",
            }
        )
    else:
        for index, regression in enumerate(prior_run_regressions, start=1):
            if not isinstance(regression, Mapping):
                gaps.append(
                    {
                        "id": "invalid_prior_run_regression",
                        "index": index,
                        "message": "prior-run regression entry is not an object",
                    }
                )
                continue
            regression_flags = regression.get("regression_flags")
            if not is_sequence_payload(regression_flags):
                gaps.append(
                    {
                        "id": "missing_prior_run_regression_flags",
                        "index": index,
                        "message": (
                            "prior-run regression entry is missing regression flags"
                        ),
                    }
                )
            elif not all(isinstance(flag, str) for flag in regression_flags):
                gaps.append(
                    {
                        "id": "invalid_prior_run_regression_flags",
                        "index": index,
                        "message": (
                            "prior-run regression entry has non-string regression flags"
                        ),
                    }
                )
    return gaps


def expected_skill_comparison_conditions(
    aggregate: Mapping[str, object],
    *,
    baseline_condition: str,
) -> list[str]:
    expected: set[str] = set()
    cases = mapping_value(aggregate.get("cases"))
    for case in list_eval_example_cases():
        case_payload = mapping_value(cases.get(case.case_id))
        for condition in case.conditions:
            if condition == baseline_condition:
                continue
            condition_payload = mapping_value(case_payload.get(condition))
            if integer_value(condition_payload.get("trials")) > 0:
                expected.add(condition)
    return sorted(expected)


def release_trial_failures(
    aggregate: Mapping[str, object],
    *,
    required_case_conditions: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, object]]:
    required = normalized_required_case_conditions(required_case_conditions)
    records = sequence_value(aggregate.get("records"))
    failures: list[dict[str, object]] = []
    failed_pairs: set[tuple[str, str]] = set()
    for record_value in records:
        record = mapping_value(record_value)
        case_id = string_value(record.get("case_id"))
        condition = string_value(record.get("condition"))
        if condition not in required.get(case_id, ()):
            continue
        if record.get("status") == "passed":
            continue
        failed_pairs.add((case_id, condition))
        failures.append(
            {
                "case_id": case_id,
                "condition": condition,
                "trial": integer_value(record.get("trial")),
                "run_id": string_value(record.get("run_id")),
                "status": string_value(record.get("status")) or "unknown",
                "artifact_root": string_value(record.get("artifact_root")),
                "failure_taxonomy": string_list(record.get("failure_taxonomy")),
            }
        )
    cases = mapping_value(aggregate.get("cases"))
    for case_id, conditions in sorted(required.items()):
        case_payload = mapping_value(cases.get(case_id))
        for condition in conditions:
            if (case_id, condition) in failed_pairs:
                continue
            condition_payload = mapping_value(case_payload.get(condition))
            trials = integer_value(condition_payload.get("trials"))
            pass_count = integer_value(condition_payload.get("pass_count"))
            if trials <= 0 or pass_count >= trials:
                continue
            failures.append(
                {
                    "case_id": case_id,
                    "condition": condition,
                    "trial": 0,
                    "run_id": "",
                    "status": "failed",
                    "artifact_root": "",
                    "failure_taxonomy": [
                        key
                        for key in mapping_value(
                            condition_payload.get("failure_taxonomy")
                        )
                        if isinstance(key, str)
                    ],
                    "summary": {
                        "trials": trials,
                        "pass_count": pass_count,
                    },
                }
            )
    return failures


def annotate_regressions(
    regressions: Sequence[Mapping[str, object]],
    *,
    parked_regressions: Mapping[str, Sequence[str]],
    parked_workflow_regression_task_ids: Sequence[str],
) -> list[dict[str, object]]:
    global_task_ids = unique_nonempty(parked_workflow_regression_task_ids)
    annotated: list[dict[str, object]] = []
    for regression in regressions:
        regression_id = string_value(regression.get("id"))
        task_ids = unique_nonempty(
            [*parked_regressions.get(regression_id, ()), *global_task_ids]
        )
        payload = dict(regression)
        payload["parked_task_ids"] = task_ids
        annotated.append(payload)
    return annotated


def release_blockers(
    *,
    local_suite: Mapping[str, object],
    quality_evidence_gaps: Sequence[Mapping[str, object]],
    trial_failures: Sequence[Mapping[str, object]],
    unresolved_regressions: Sequence[Mapping[str, object]],
    invalid_parked_ids: Sequence[str],
) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    coverage_gaps = sequence_value(local_suite.get("coverage_gaps"))
    if coverage_gaps:
        blockers.append(
            {
                "id": "local_demo_coverage",
                "message": "local demo suite coverage is incomplete",
                "count": len(coverage_gaps),
            }
        )
    if trial_failures:
        blockers.append(
            {
                "id": "release_trial_failures",
                "message": "required release-gate trials did not pass",
                "count": len(trial_failures),
            }
        )
    if quality_evidence_gaps:
        blockers.append(
            {
                "id": "skill_quality_evidence",
                "message": "skill-quality workflow-regression evidence is incomplete",
                "count": len(quality_evidence_gaps),
            }
        )
    if unresolved_regressions:
        blockers.append(
            {
                "id": "workflow_contract_regressions",
                "message": "workflow-contract regressions are not parked",
                "count": len(unresolved_regressions),
            }
        )
    if invalid_parked_ids:
        blockers.append(
            {
                "id": "invalid_parked_regressions",
                "message": "parked regression id did not match the aggregate",
                "regression_ids": list(invalid_parked_ids),
            }
        )
    return blockers


def release_checklist(record: Mapping[str, object]) -> list[dict[str, object]]:
    local_suite = mapping_value(record.get("local_suite"))
    trial_failures = mapping_value(record.get("trial_failures"))
    regressions = mapping_value(record.get("workflow_contract_regressions"))
    external = mapping_value(record.get("external_benchmarks"))
    return [
        {
            "id": "run_local_demo_suite",
            "required": True,
            "status": local_suite.get("coverage_status", "blocked"),
            "evidence": local_suite.get("aggregate_path", ""),
        },
        {
            "id": "required_trials_pass",
            "required": True,
            "status": trial_failures.get("status", "blocked"),
            "failure_count": trial_failures.get("total", 0),
        },
        {
            "id": "resolve_workflow_contract_regressions",
            "required": True,
            "status": "passed"
            if not sequence_value(regressions.get("evidence_gaps"))
            and not sequence_value(regressions.get("unresolved"))
            else "blocked",
            "evidence_status": regressions.get("evidence_status", "blocked"),
            "parked_count": len(sequence_value(regressions.get("parked"))),
            "unresolved_count": len(sequence_value(regressions.get("unresolved"))),
        },
        {
            "id": "record_external_benchmark_smoke",
            "required": False,
            "status": external.get("status", "optional_not_provided"),
            "evidence_count": len(sequence_value(external.get("records"))),
        },
        {
            "id": "include_eval_evidence_in_release_notes",
            "required": True,
            "status": "manual",
            "evidence": "reference this release-readiness record and any parked task IDs",
        },
    ]


def workflow_delta_summary(value: object) -> dict[str, object]:
    deltas = mapping_value(value)
    return {
        key: deltas[key]
        for key in ("workflow_score_mean", "workflow_violation_rate")
        if key in deltas
        and (
            deltas[key] is None
            or (
                isinstance(deltas[key], (int, float))
                and not isinstance(deltas[key], bool)
            )
        )
    }


def release_record_refs(value: object) -> list[dict[str, object]]:
    return [
        release_record_ref(item)
        for item in sequence_value(value)
        if isinstance(item, Mapping)
    ]


def release_record_ref(record: Mapping[str, object]) -> dict[str, object]:
    artifact_root = string_value(record.get("artifact_root"))
    reproducibility = record.get("reproducibility")
    if isinstance(reproducibility, Mapping):
        artifact_root = (
            string_value(reproducibility.get("artifact_root")) or artifact_root
        )
    labels = set(string_list(record.get("failure_taxonomy")))
    scoring = record.get("scoring")
    if isinstance(scoring, Mapping):
        labels.update(string_list(scoring.get("failure_taxonomy")))
    return {
        "run_id": record.get("run_id"),
        "case_id": record.get("case_id"),
        "condition": record.get("condition"),
        "trial": record.get("trial"),
        "artifact_root": artifact_root,
        "failure_taxonomy": sorted(labels),
    }


def external_summary(payload: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "benchmark",
        "suite_id",
        "dataset",
        "split",
        "sample_size",
        "conditions",
        "status",
        "outcome",
        "generated_at",
        "summary",
    )
    summary: dict[str, object] = {}
    for key in keys:
        if key not in payload:
            continue
        sanitized = sanitize_external_summary_value(key, payload[key], depth=0)
        if sanitized is not None:
            summary[key] = sanitized
    if summary:
        return summary
    return {"keys": sorted(str(key) for key in payload)[:20]}


def sanitize_external_summary_value(
    key: object,
    value: object,
    *,
    depth: int,
) -> object:
    key_text = str(key).lower()
    if any(
        fragment in key_text for fragment in SENSITIVE_EXTERNAL_SUMMARY_KEY_FRAGMENTS
    ):
        return {"omitted": "sensitive_key"}
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, str):
        if "\n" in value or len(value) > EXTERNAL_SUMMARY_STRING_LIMIT:
            return {"omitted": "long_string", "length": len(value)}
        return value
    if isinstance(value, Mapping):
        if depth >= 1:
            return {"omitted": "nested_mapping"}
        sanitized: dict[str, object] = {}
        for index, (nested_key, nested_value) in enumerate(sorted(value.items())):
            if index >= EXTERNAL_SUMMARY_ITEM_LIMIT:
                sanitized["_omitted_items"] = len(value) - EXTERNAL_SUMMARY_ITEM_LIMIT
                break
            sanitized_value = sanitize_external_summary_value(
                nested_key,
                nested_value,
                depth=depth + 1,
            )
            if sanitized_value is not None:
                sanitized[str(nested_key)] = sanitized_value
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if depth >= 1:
            return {"omitted": "nested_sequence"}
        sanitized_items: list[object] = []
        for index, item in enumerate(value):
            if index >= EXTERNAL_SUMMARY_ITEM_LIMIT:
                sanitized_items.append(
                    {
                        "omitted": "extra_items",
                        "count": len(value) - EXTERNAL_SUMMARY_ITEM_LIMIT,
                    }
                )
                break
            sanitized_items.append(
                sanitize_external_summary_value(index, item, depth=depth + 1)
            )
        return sanitized_items
    return {"omitted": type(value).__name__}


def render_coverage_gap_summary(value: object) -> list[str]:
    gaps = [gap for gap in sequence_value(value) if isinstance(gap, Mapping)]
    if not gaps:
        return []
    lines = ["coverage gaps:"]
    for gap in gaps[:5]:
        lines.append(
            "- "
            + " ".join(
                str(part)
                for part in (
                    gap.get("case_id", ""),
                    gap.get("condition", ""),
                    f"trials={gap.get('trials', 0)}/{gap.get('required_trials', 0)}",
                    gap.get("reason", ""),
                )
                if part
            )
        )
    if len(gaps) > 5:
        lines.append(f"- ... {len(gaps) - 5} more")
    return lines


def render_trial_failure_summary(value: object) -> list[str]:
    failures = [
        failure for failure in sequence_value(value) if isinstance(failure, Mapping)
    ]
    if not failures:
        return []
    lines = ["release trial failures:"]
    for failure in failures[:5]:
        labels = ", ".join(string_list(failure.get("failure_taxonomy")))
        suffix = f" {labels}" if labels else ""
        lines.append(
            "- "
            + " ".join(
                str(part)
                for part in (
                    failure.get("case_id", ""),
                    failure.get("condition", ""),
                    f"trial={failure.get('trial', 0)}",
                    failure.get("status", ""),
                )
                if part
            )
            + suffix
        )
    if len(failures) > 5:
        lines.append(f"- ... {len(failures) - 5} more")
    return lines


def render_evidence_gap_summary(value: object) -> list[str]:
    gaps = [gap for gap in sequence_value(value) if isinstance(gap, Mapping)]
    if not gaps:
        return []
    lines = ["skill-quality evidence gaps:"]
    for gap in gaps[:5]:
        condition = f" condition={gap['condition']}" if "condition" in gap else ""
        lines.append(f"- {gap.get('id', 'unknown')}{condition}")
    if len(gaps) > 5:
        lines.append(f"- ... {len(gaps) - 5} more")
    return lines


def render_unresolved_regression_summary(value: object) -> list[str]:
    regressions = [
        regression
        for regression in sequence_value(value)
        if isinstance(regression, Mapping)
    ]
    if not regressions:
        return []
    lines = ["unresolved workflow regressions:"]
    for regression in regressions[:5]:
        regression_id = regression.get("id", "unknown")
        lines.append(
            f"- {regression_id} (park with --parked-regression {regression_id}=TASK-ID)"
        )
    if len(regressions) > 5:
        lines.append(f"- ... {len(regressions) - 5} more")
    return lines


def unique_nonempty(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mapping_value(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def sequence_value(value: object) -> list[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def is_sequence_payload(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def string_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, str)]


def string_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def integer_value(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def utc_now() -> str:
    return datetime.now(UTC).isoformat()
