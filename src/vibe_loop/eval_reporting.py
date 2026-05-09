from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence


TASK_OUTCOME_LABELS = frozenset({"task_outcome"})
WORKFLOW_CONTRACT_LABELS = frozenset(
    {
        "workflow_contract",
        "trigger_false_negative",
        "trigger_false_positive",
        "unsafe_git",
        "secret_access",
        "state_contamination",
        "review_missing",
        "integration_missing",
        "unnecessary_user_prompt",
    }
)
TRIGGER_MISS_LABELS = frozenset({"trigger_false_negative", "trigger_false_positive"})
INFRASTRUCTURE_LABELS = frozenset({"harness_error", "grader_error"})

FAILURE_CATEGORIES: tuple[
    tuple[str, frozenset[str], str],
    ...,
] = (
    ("task_outcome_failures", TASK_OUTCOME_LABELS, "Task outcome failures"),
    ("workflow_contract_failures", WORKFLOW_CONTRACT_LABELS, "Workflow contract failures"),
    ("skill_trigger_misses", TRIGGER_MISS_LABELS, "Skill trigger misses"),
    ("review_discipline_failures", frozenset({"review_missing"}), "Review discipline failures"),
    (
        "integration_discipline_failures",
        frozenset({"integration_missing"}),
        "Integration discipline failures",
    ),
    ("unsafe_git_behavior", frozenset({"unsafe_git"}), "Unsafe git behavior"),
    (
        "unnecessary_user_prompts",
        frozenset({"unnecessary_user_prompt"}),
        "Unnecessary user prompts",
    ),
    ("secret_or_state_leaks", frozenset({"secret_access", "state_contamination"}), "Secret/state leaks"),
    ("infrastructure_failures", INFRASTRUCTURE_LABELS, "Infrastructure failures"),
    ("flaky_trials", frozenset({"flaky"}), "Flaky trials"),
)


def build_skill_quality_report(
    records: Sequence[Mapping[str, object]],
    *,
    baseline_condition: str = "no_skill",
    previous_aggregate: Mapping[str, object] | None = None,
) -> dict[str, object]:
    by_condition = group_records(records, lambda record: string_value(record.get("condition")))
    baseline_records = tuple(by_condition.get(baseline_condition, ()))
    quality_by_condition = {
        condition: condition_quality_summary(condition_records)
        for condition, condition_records in sorted(by_condition.items())
    }
    return {
        "schema_version": 1,
        "baseline_condition": baseline_condition,
        "conditions": quality_by_condition,
        "condition_comparisons": condition_comparisons(
            by_condition,
            baseline_condition=baseline_condition,
        ),
        "failure_categories": failure_category_summaries(records),
        "overlong_trajectories": overlong_summary(records),
        "cost_regressions": cost_regressions(
            by_condition,
            baseline_condition=baseline_condition,
        ),
        "prior_run_regressions": prior_run_regressions(
            quality_by_condition,
            previous_aggregate=previous_aggregate,
        ),
        "per_task_uplift": grouped_uplift(
            records,
            lambda record: string_value(record.get("case_id")),
            baseline_records=baseline_records,
            baseline_condition=baseline_condition,
        ),
        "per_domain_uplift": grouped_uplift(
            records,
            record_domain,
            baseline_records=baseline_records,
            baseline_condition=baseline_condition,
        ),
    }


def condition_quality_summary(
    records: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    primary = [record for record in records if not excluded_from_primary(record)]
    passed = sum(1 for record in primary if record_passed(record))
    primary_trials = len(primary)
    workflow_failures = count_records_with_labels(records, WORKFLOW_CONTRACT_LABELS)
    trigger_misses = count_records_with_labels(records, TRIGGER_MISS_LABELS)
    return {
        "trials": len(records),
        "primary_trials": primary_trials,
        "pass_count": passed,
        "pass_rate": round(passed / primary_trials, 6) if primary_trials else 0.0,
        "task_score_mean": mean_metric(records, "task_score"),
        "workflow_score_mean": mean_metric(records, "workflow_score"),
        "trigger_score_mean": mean_metric(records, "trigger_score"),
        "workflow_violation_rate": round(workflow_failures / len(records), 6)
        if records
        else 0.0,
        "trigger_miss_rate": round(trigger_misses / len(records), 6)
        if records
        else 0.0,
        "latency_seconds_mean": mean_structured(records, "latency_seconds"),
        "command_count_mean": mean_structured(records, "command_count"),
        "token_per_trial": per_trial_usage(records, "tokens"),
        "cost_per_trial": per_trial_usage(records, "cost_usd"),
        "failure_taxonomy": dict(sorted(failure_counts(records).items())),
        "records": [record_ref(record) for record in records],
    }


def condition_comparisons(
    by_condition: Mapping[str, Sequence[Mapping[str, object]]],
    *,
    baseline_condition: str,
) -> dict[str, object]:
    baseline_records = tuple(by_condition.get(baseline_condition, ()))
    if not baseline_records:
        return {}
    baseline = condition_quality_summary(baseline_records)
    comparisons: dict[str, object] = {}
    for condition, records in sorted(by_condition.items()):
        if condition == baseline_condition:
            continue
        current = condition_quality_summary(records)
        deltas = {
            key: numeric_delta(current.get(key), baseline.get(key))
            for key in (
                "pass_rate",
                "task_score_mean",
                "workflow_score_mean",
                "trigger_score_mean",
                "workflow_violation_rate",
                "trigger_miss_rate",
                "latency_seconds_mean",
                "command_count_mean",
                "token_per_trial",
                "cost_per_trial",
            )
        }
        flags = regression_flags(deltas)
        comparisons[condition] = {
            "baseline_condition": baseline_condition,
            "deltas": deltas,
            "regression_flags": flags,
            "baseline_records": [record_ref(record) for record in baseline_records],
            "condition_records": [record_ref(record) for record in records],
        }
    return comparisons


def failure_category_summaries(
    records: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    summaries: dict[str, object] = {}
    for key, labels, title in FAILURE_CATEGORIES:
        matches = records_with_labels(records, labels)
        summaries[key] = category_summary(title, labels, records, matches)
    return summaries


def category_summary(
    title: str,
    labels: frozenset[str],
    all_records: Sequence[Mapping[str, object]],
    matches: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "title": title,
        "labels": sorted(labels),
        "count": len(matches),
        "rate": round(len(matches) / len(all_records), 6) if all_records else 0.0,
        "by_condition": dict(sorted(counter_for(matches, "condition").items())),
        "by_case": dict(sorted(counter_for(matches, "case_id").items())),
        "records": [record_ref(record) for record in matches],
    }


def overlong_summary(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    matches = [record for record in records if overlong_trajectory(record)]
    return category_summary(
        "Overlong trajectories",
        frozenset({"timeout", "max_commands", "timeout_seconds", "max_output_bytes"}),
        records,
        matches,
    )


def cost_regressions(
    by_condition: Mapping[str, Sequence[Mapping[str, object]]],
    *,
    baseline_condition: str,
) -> list[dict[str, object]]:
    baseline_records = tuple(by_condition.get(baseline_condition, ()))
    baseline = condition_quality_summary(baseline_records)
    baseline_cost = numeric_value(baseline.get("cost_per_trial"))
    if baseline_cost is None:
        return []
    regressions = []
    for condition, records in sorted(by_condition.items()):
        if condition == baseline_condition:
            continue
        current = condition_quality_summary(records)
        current_cost = numeric_value(current.get("cost_per_trial"))
        if current_cost is None or current_cost <= baseline_cost:
            continue
        regressions.append(
            {
                "condition": condition,
                "baseline_condition": baseline_condition,
                "baseline_cost_per_trial": baseline_cost,
                "cost_per_trial": current_cost,
                "delta": round(current_cost - baseline_cost, 6),
                "baseline_records": [record_ref(record) for record in baseline_records],
                "records": [record_ref(record) for record in records],
            }
        )
    return regressions


def prior_run_regressions(
    current_conditions: Mapping[str, Mapping[str, object]],
    *,
    previous_aggregate: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    if previous_aggregate is None:
        return []
    previous_conditions = previous_condition_summaries(previous_aggregate)
    regressions = []
    for condition, current in sorted(current_conditions.items()):
        previous = previous_conditions.get(condition)
        if previous is None:
            continue
        deltas = {
            key: numeric_delta(current.get(key), previous.get(key))
            for key in (
                "pass_rate",
                "task_score_mean",
                "workflow_score_mean",
                "trigger_score_mean",
                "workflow_violation_rate",
                "trigger_miss_rate",
                "latency_seconds_mean",
                "command_count_mean",
                "token_per_trial",
                "cost_per_trial",
            )
        }
        flags = regression_flags(deltas)
        if not flags:
            continue
        regressions.append(
            {
                "condition": condition,
                "previous_generated_at": previous_aggregate.get("generated_at", ""),
                "deltas": deltas,
                "regression_flags": flags,
                "previous_records": list(previous.get("records", []))
                if isinstance(previous.get("records"), list)
                else [],
                "records": list(current.get("records", []))
                if isinstance(current.get("records"), list)
                else [],
            }
        )
    return regressions


def previous_condition_summaries(
    previous_aggregate: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    quality = previous_aggregate.get("skill_quality")
    if isinstance(quality, Mapping):
        conditions = quality.get("conditions")
        if isinstance(conditions, Mapping):
            return {
                condition: dict(payload)
                for condition, payload in conditions.items()
                if isinstance(condition, str) and isinstance(payload, Mapping)
            }
    conditions = previous_aggregate.get("conditions")
    if not isinstance(conditions, Mapping):
        return {}
    records_by_condition = group_records(
        mapping_list(previous_aggregate.get("records")),
        lambda record: string_value(record.get("condition")),
    )
    return {
        condition: normalize_legacy_condition_summary(
            payload,
            records_by_condition.get(condition, ()),
        )
        for condition, payload in conditions.items()
        if isinstance(condition, str) and isinstance(payload, Mapping)
    }


def normalize_legacy_condition_summary(
    payload: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    trials = numeric_value(payload.get("trials"))
    normalized = dict(payload)
    normalized["latency_seconds_mean"] = nested_mean(payload.get("latency_seconds"))
    normalized["command_count_mean"] = nested_mean(payload.get("command_count"))
    normalized["token_per_trial"] = total_per_trial(payload.get("token_total"), trials)
    normalized["cost_per_trial"] = total_per_trial(payload.get("cost_total"), trials)
    normalized["records"] = [record_ref(record) for record in records]
    return normalized


def nested_mean(value: object) -> float | None:
    if not isinstance(value, Mapping):
        return None
    return numeric_value(value.get("mean"))


def total_per_trial(total: object, trials: float | None) -> float | None:
    total_value = numeric_value(total)
    if total_value is None or trials is None or trials <= 0:
        return None
    return round(total_value / trials, 6)


def grouped_uplift(
    records: Sequence[Mapping[str, object]],
    key_fn: Callable[[Mapping[str, object]], str],
    *,
    baseline_records: Sequence[Mapping[str, object]],
    baseline_condition: str,
) -> dict[str, object]:
    by_group_condition: dict[tuple[str, str], list[Mapping[str, object]]] = defaultdict(list)
    groups = sorted({key_fn(record) for record in records if key_fn(record)})
    for record in records:
        group = key_fn(record)
        condition = string_value(record.get("condition"))
        if group and condition:
            by_group_condition[(group, condition)].append(record)

    result: dict[str, object] = {}
    for group in groups:
        baseline_group_records = tuple(
            record for record in baseline_records if key_fn(record) == group
        )
        baseline_summary = (
            condition_quality_summary(baseline_group_records)
            if baseline_group_records
            else {}
        )
        group_payload: dict[str, object] = {}
        conditions = sorted(
            condition
            for item_group, condition in by_group_condition
            if item_group == group and condition != baseline_condition
        )
        for condition in conditions:
            current_records = tuple(by_group_condition[(group, condition)])
            current_summary = condition_quality_summary(current_records)
            current_pass = numeric_value(current_summary.get("pass_rate"))
            baseline_pass = numeric_value(baseline_summary.get("pass_rate"))
            group_payload[condition] = {
                "baseline_condition": baseline_condition,
                "baseline_pass_rate": baseline_pass,
                "pass_rate": current_pass,
                "absolute_uplift": numeric_delta(current_pass, baseline_pass),
                "normalized_gain": normalized_gain(current_pass, baseline_pass),
                "task_score_delta": numeric_delta(
                    current_summary.get("task_score_mean"),
                    baseline_summary.get("task_score_mean"),
                ),
                "workflow_score_delta": numeric_delta(
                    current_summary.get("workflow_score_mean"),
                    baseline_summary.get("workflow_score_mean"),
                ),
                "trigger_score_delta": numeric_delta(
                    current_summary.get("trigger_score_mean"),
                    baseline_summary.get("trigger_score_mean"),
                ),
                "baseline_records": [record_ref(record) for record in baseline_group_records],
                "condition_records": [record_ref(record) for record in current_records],
            }
        if group_payload:
            result[group] = group_payload
    return result


def render_skill_quality_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "## Skill Quality",
        "",
        f"Baseline condition: `{report.get('baseline_condition', '')}`",
        "",
        "| Condition | Pass delta | Task delta | Workflow delta | Trigger delta | Cost delta | Flags | Baseline records | Current records |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    comparisons = report.get("condition_comparisons")
    if isinstance(comparisons, Mapping):
        for condition, payload in comparisons.items():
            if not isinstance(payload, Mapping):
                continue
            deltas = payload.get("deltas")
            flags = payload.get("regression_flags")
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_cell(condition),
                        format_delta(mapping_get_number(deltas, "pass_rate")),
                        format_delta(mapping_get_number(deltas, "task_score_mean")),
                        format_delta(mapping_get_number(deltas, "workflow_score_mean")),
                        format_delta(mapping_get_number(deltas, "trigger_score_mean")),
                        format_delta(mapping_get_number(deltas, "cost_per_trial")),
                        markdown_cell(", ".join(string_list(flags)) if flags else ""),
                        markdown_cell(render_record_refs(payload.get("baseline_records"))),
                        markdown_cell(render_record_refs(payload.get("condition_records"))),
                    ]
                )
                + " |"
            )
    prior_regressions = report.get("prior_run_regressions")
    if isinstance(prior_regressions, Sequence) and not isinstance(
        prior_regressions,
        (str, bytes),
    ):
        nonempty_regressions = [
            regression
            for regression in prior_regressions
            if isinstance(regression, Mapping)
        ]
        if nonempty_regressions:
            lines.extend(["", "### Prior Run Regressions", ""])
            lines.extend(
                [
                    "| Condition | Previous generated at | Flags | Previous records | Current records |",
                    "| --- | --- | --- | --- | --- |",
                ]
            )
            for regression in nonempty_regressions:
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            markdown_cell(regression.get("condition", "")),
                            markdown_cell(regression.get("previous_generated_at", "")),
                            markdown_cell(
                                ", ".join(
                                    string_list(regression.get("regression_flags"))
                                )
                            ),
                            markdown_cell(
                                render_record_refs(regression.get("previous_records"))
                            ),
                            markdown_cell(render_record_refs(regression.get("records"))),
                        ]
                    )
                    + " |"
                )
    lines.extend(["", "### Failure Categories", ""])
    lines.extend(
        [
            "| Category | Count | Conditions | Records |",
            "| --- | ---: | --- | --- |",
        ]
    )
    categories = report.get("failure_categories")
    if isinstance(categories, Mapping):
        for category, payload in categories.items():
            if not isinstance(payload, Mapping):
                continue
            count = payload.get("count", 0)
            if count == 0:
                continue
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_cell(str(category)),
                        str(count),
                        markdown_cell(render_count_mapping(payload.get("by_condition"))),
                        markdown_cell(render_record_refs(payload.get("records"))),
                    ]
                )
                + " |"
            )
    overlong = report.get("overlong_trajectories")
    if isinstance(overlong, Mapping) and overlong.get("count"):
        lines.append(
            "| "
            + " | ".join(
                [
                    "overlong_trajectories",
                    str(overlong.get("count", 0)),
                    markdown_cell(render_count_mapping(overlong.get("by_condition"))),
                    markdown_cell(render_record_refs(overlong.get("records"))),
                ]
            )
            + " |"
        )
    lines.extend(["", "### Per Task Uplift", ""])
    lines.extend(render_uplift_table(report.get("per_task_uplift"), "Task"))
    lines.extend(["", "### Per Domain Uplift", ""])
    lines.extend(render_uplift_table(report.get("per_domain_uplift"), "Domain"))
    return "\n".join(lines) + "\n"


def render_uplift_table(value: object, label: str) -> list[str]:
    lines = [
        f"| {label} | Condition | Baseline pass | Pass rate | Uplift | Baseline records | Current records |",
        "| --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    if not isinstance(value, Mapping):
        return lines
    for group, conditions in value.items():
        if not isinstance(conditions, Mapping):
            continue
        for condition, payload in conditions.items():
            if not isinstance(payload, Mapping):
                continue
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_cell(str(group)),
                        markdown_cell(str(condition)),
                        format_number(payload.get("baseline_pass_rate")),
                        format_number(payload.get("pass_rate")),
                        format_delta(numeric_value(payload.get("absolute_uplift"))),
                        markdown_cell(render_record_refs(payload.get("baseline_records"))),
                        markdown_cell(render_record_refs(payload.get("condition_records"))),
                    ]
                )
                + " |"
            )
    return lines


def group_records(
    records: Sequence[Mapping[str, object]],
    key_fn: Callable[[Mapping[str, object]], str],
) -> dict[str, list[Mapping[str, object]]]:
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for record in records:
        key = key_fn(record)
        if key:
            grouped[key].append(record)
    return grouped


def records_with_labels(
    records: Sequence[Mapping[str, object]],
    labels: frozenset[str],
) -> list[Mapping[str, object]]:
    return [record for record in records if labels & record_labels(record)]


def count_records_with_labels(
    records: Sequence[Mapping[str, object]],
    labels: frozenset[str],
) -> int:
    return len(records_with_labels(records, labels))


def failure_counts(records: Sequence[Mapping[str, object]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for record in records:
        counts.update(record_labels(record))
    return counts


def counter_for(records: Sequence[Mapping[str, object]], key: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for record in records:
        value = string_value(record.get(key))
        if value:
            counts[value] += 1
    return counts


def record_ref(record: Mapping[str, object]) -> dict[str, object]:
    artifact_root = string_value(record.get("artifact_root"))
    reproducibility = record.get("reproducibility")
    if isinstance(reproducibility, Mapping):
        artifact_root = string_value(reproducibility.get("artifact_root")) or artifact_root
    return {
        "run_id": record.get("run_id"),
        "case_id": record.get("case_id"),
        "condition": record.get("condition"),
        "trial": record.get("trial"),
        "artifact_root": artifact_root,
        "failure_taxonomy": sorted(record_labels(record)),
    }


def record_domain(record: Mapping[str, object]) -> str:
    task = record.get("task")
    if isinstance(task, Mapping):
        domain = string_value(task.get("domain"))
        if domain:
            return domain
    case_id = string_value(record.get("case_id"))
    return case_id or "unknown"


def record_labels(record: Mapping[str, object]) -> frozenset[str]:
    labels = set(string_list(record.get("failure_taxonomy")))
    scoring = record.get("scoring")
    if isinstance(scoring, Mapping):
        labels.update(string_list(scoring.get("failure_taxonomy")))
    return frozenset(labels)


def overlong_trajectory(record: Mapping[str, object]) -> bool:
    labels = record_labels(record)
    if "timeout" in labels or record.get("status") == "timeout":
        return True
    structured = record.get("structured_result")
    budget = record.get("budget")
    if not isinstance(structured, Mapping) or not isinstance(budget, Mapping):
        return False
    checks = (
        ("command_count", "max_commands"),
        ("latency_seconds", "timeout_seconds"),
        ("duration_seconds", "timeout_seconds"),
        ("output_bytes", "max_output_bytes"),
    )
    for actual_key, budget_key in checks:
        actual = numeric_value(structured.get(actual_key))
        limit = numeric_value(budget.get(budget_key))
        if actual is not None and limit is not None and actual > limit:
            return True
    return False


def regression_flags(deltas: Mapping[str, object]) -> list[str]:
    flags = []
    if negative_delta(deltas.get("pass_rate")):
        flags.append("pass_rate_regression")
    if negative_delta(deltas.get("task_score_mean")):
        flags.append("task_outcome_regression")
    if positive_delta(deltas.get("workflow_violation_rate")) or negative_delta(
        deltas.get("workflow_score_mean")
    ):
        flags.append("workflow_contract_regression")
    if positive_delta(deltas.get("trigger_miss_rate")) or negative_delta(
        deltas.get("trigger_score_mean")
    ):
        flags.append("skill_trigger_regression")
    if positive_delta(deltas.get("command_count_mean")) or positive_delta(
        deltas.get("latency_seconds_mean")
    ):
        flags.append("trajectory_length_regression")
    if positive_delta(deltas.get("cost_per_trial")):
        flags.append("cost_regression")
    return flags


def excluded_from_primary(record: Mapping[str, object]) -> bool:
    scoring = record.get("scoring")
    return bool(isinstance(scoring, Mapping) and scoring.get("excluded_from_primary"))


def record_passed(record: Mapping[str, object]) -> bool:
    scoring = record.get("scoring")
    return bool(isinstance(scoring, Mapping) and scoring.get("passed") is True)


def mean_metric(records: Sequence[Mapping[str, object]], key: str) -> float | None:
    values = []
    for record in records:
        scoring = record.get("scoring")
        if isinstance(scoring, Mapping):
            value = numeric_value(scoring.get(key))
            if value is not None:
                values.append(value)
    return rounded_mean(values)


def mean_structured(records: Sequence[Mapping[str, object]], key: str) -> float | None:
    values = []
    for record in records:
        structured = record.get("structured_result")
        if isinstance(structured, Mapping):
            value = numeric_value(structured.get(key))
            if value is not None:
                values.append(value)
    return rounded_mean(values)


def per_trial_usage(records: Sequence[Mapping[str, object]], key: str) -> float | None:
    values = []
    for record in records:
        structured = record.get("structured_result")
        usage = structured.get("usage") if isinstance(structured, Mapping) else None
        if isinstance(usage, Mapping):
            value = numeric_value(usage.get(key))
            if value is not None:
                values.append(value)
    if not records or not values:
        return None
    return round(sum(values) / len(records), 6)


def numeric_delta(current: object, baseline: object) -> float | None:
    current_value = numeric_value(current)
    baseline_value = numeric_value(baseline)
    if current_value is None or baseline_value is None:
        return None
    return round(current_value - baseline_value, 6)


def normalized_gain(current: object, baseline: object) -> float | None:
    current_value = numeric_value(current)
    baseline_value = numeric_value(baseline)
    if current_value is None or baseline_value is None:
        return None
    if baseline_value >= 1.0:
        return 0.0 if current_value >= baseline_value else -1.0
    return round((current_value - baseline_value) / (1.0 - baseline_value), 6)


def rounded_mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def numeric_value(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def negative_delta(value: object) -> bool:
    number = numeric_value(value)
    return number is not None and number < 0


def positive_delta(value: object) -> bool:
    number = numeric_value(value)
    return number is not None and number > 0


def mapping_get_number(value: object, key: str) -> float | None:
    if isinstance(value, Mapping):
        return numeric_value(value.get(key))
    return None


def string_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def string_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, str)]


def mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def render_count_mapping(value: object) -> str:
    if not isinstance(value, Mapping) or not value:
        return ""
    return ", ".join(f"{key}={value[key]}" for key in sorted(value))


def render_record_refs(value: object) -> str:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ""
    refs = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        run_id = string_value(item.get("run_id"))
        artifact_root = string_value(item.get("artifact_root"))
        refs.append(f"{run_id} ({artifact_root})" if artifact_root else run_id)
    return ", ".join(refs)


def format_number(value: object) -> str:
    number = numeric_value(value)
    if number is None:
        return ""
    return f"{number:.6g}"


def format_delta(value: object) -> str:
    number = numeric_value(value)
    if number is None:
        return ""
    return f"{number:+.6g}"


def markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|")
