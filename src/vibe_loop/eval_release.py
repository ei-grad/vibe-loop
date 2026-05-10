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
    minimum_trials: int = 3,
    local_suite_mode: str = "existing_aggregate",
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
    )
    quality_evidence_gaps = skill_quality_evidence_gaps(aggregate)
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
            "required_suite_id": EXAMPLE_SUITE_ID,
            "blockers": blockers,
        },
        "local_suite": local_suite,
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
) -> dict[str, object]:
    coverage = local_suite_coverage(aggregate, minimum_trials=minimum_trials)
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
        "coverage_status": "passed" if not coverage else "blocked",
        "coverage_gaps": coverage,
    }


def local_suite_coverage(
    aggregate: Mapping[str, object],
    *,
    minimum_trials: int,
) -> list[dict[str, object]]:
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
    for case in list_eval_example_cases():
        case_payload = mapping_value(cases.get(case.case_id))
        for condition in case.conditions:
            condition_payload = mapping_value(case_payload.get(condition))
            trials = integer_value(condition_payload.get("trials"))
            if trials >= minimum_trials:
                continue
            gaps.append(
                {
                    "case_id": case.case_id,
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
                "records": sequence_value(comparison.get("condition_records")),
                "baseline_records": sequence_value(comparison.get("baseline_records")),
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
                "records": sequence_value(regression.get("records")),
                "previous_records": sequence_value(regression.get("previous_records")),
            }
        )
    return regressions


def skill_quality_evidence_gaps(
    aggregate: Mapping[str, object],
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
    baseline_condition = string_value(quality.get("baseline_condition")) or "no_skill"
    comparisons = quality.get("condition_comparisons")
    if not isinstance(comparisons, Mapping):
        gaps.append(
            {
                "id": "missing_condition_comparisons",
                "message": "skill_quality is missing condition comparisons",
            }
        )
        comparisons = {}

    for condition in expected_skill_comparison_conditions(
        aggregate,
        baseline_condition=baseline_condition,
    ):
        comparison = comparisons.get(condition)
        if not isinstance(comparison, Mapping):
            gaps.append(
                {
                    "id": "missing_condition_comparison",
                    "condition": condition,
                    "message": f"skill_quality is missing comparison for {condition}",
                }
            )
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
