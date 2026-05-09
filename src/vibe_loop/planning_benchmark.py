from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from vibe_loop.config import (
    PlanningAnalyticsDurationModelConfig,
    VibeConfig,
    planning_analytics_output_report,
)
from vibe_loop.planning_evidence import (
    DEFAULT_GIT_COMMIT_LIMIT,
    PlanningEvidence,
    collect_planning_evidence,
)
from vibe_loop.planning_timeline import (
    ACTUAL_IDLE_GAP_CLIP_MINUTES,
    ActualSpan,
    DurationBaselineModel,
    FIRST_COMMIT_FLOOR_MINUTES,
    PlanningTimelineBuilder,
    format_time,
    model_parameters,
    parse_time,
    round_minutes,
)


PLANNING_DURATION_BENCHMARK_SCHEMA_VERSION = 1
BENCHMARK_COMMAND = "vibe-loop planning benchmark-duration"
MAX_WORST_MISSES = 5
MAX_FOLDS = 5


@dataclasses.dataclass(frozen=True)
class BenchmarkExample:
    task_id: str
    task: dict[str, object]
    actual: ActualSpan
    commits: frozenset[str]


@dataclasses.dataclass(frozen=True)
class BenchmarkFold:
    fold_id: str
    validation_task_ids: tuple[str, ...]
    validation_commits: tuple[str, ...]
    training_task_ids: tuple[str, ...]
    excluded_shared_commit_task_ids: tuple[str, ...]
    leakage_free: bool

    def to_json(self) -> dict[str, object]:
        return {
            "fold_id": self.fold_id,
            "validation_task_ids": list(self.validation_task_ids),
            "validation_commits": list(self.validation_commits),
            "training_task_ids": list(self.training_task_ids),
            "excluded_shared_commit_task_ids": list(
                self.excluded_shared_commit_task_ids
            ),
            "leakage_free": self.leakage_free,
        }


def build_duration_benchmark(
    config: VibeConfig,
    *,
    evidence: PlanningEvidence | None = None,
    git_commit_limit: int = DEFAULT_GIT_COMMIT_LIMIT,
) -> dict[str, object]:
    if evidence is None:
        evidence = collect_planning_evidence(config, git_commit_limit=git_commit_limit)
    builder = PlanningTimelineBuilder(config, evidence)
    completed_ids = builder.completed_task_ids()
    actual_spans = builder.actual_spans(completed_ids)
    examples = benchmark_examples(evidence.tasks, actual_spans)
    folds = build_benchmark_folds(examples)
    generator_config = config.planning_analytics.duration_model
    candidates = duration_benchmark_candidate_configs(generator_config)
    candidate_reports = [
        evaluate_duration_candidate(candidate, examples, folds)
        for candidate in candidates
    ]
    selected = select_duration_candidate(candidate_reports, generator_config)
    selected["matches_generator_config"] = str(selected["id"]) == duration_model_id(
        generator_config
    )
    warnings = [warning.to_json() for warning in evidence.warnings]
    if len(examples) < 2:
        warnings.append(
            {
                "code": "duration_benchmark_insufficient_history",
                "message": (
                    "duration benchmark has fewer than two completed actual spans"
                ),
                "source": "benchmark-duration",
            }
        )
    exclusion = fold_exclusion_checks(folds)
    return {
        "schema_version": PLANNING_DURATION_BENCHMARK_SCHEMA_VERSION,
        "generated_by": BENCHMARK_COMMAND,
        "source_provenance": {
            "task_source_origin": evidence.task_source_origin,
            "git": {
                "commit_limit": evidence.git_commit_limit,
                "commits_collected": len(evidence.commits),
            },
        },
        "duration_model": {
            "generator_config": duration_model_payload(generator_config),
            "candidate_count": len(candidates),
        },
        "selected_estimator": selected,
        "selection_policy": {
            "primary": "lowest_mae_minutes",
            "tie_breakers": [
                "lowest_mean_log_error",
                "lowest_absolute_bias_minutes",
                "estimator_id",
            ],
        },
        "exclusion_checks": exclusion,
        "folds": [fold.to_json() for fold in folds],
        "candidates": candidate_reports,
        "warnings": dedupe_warning_payloads(warnings),
    }


def benchmark_examples(
    tasks: tuple[dict[str, object], ...],
    actual_spans: dict[str, ActualSpan],
) -> tuple[BenchmarkExample, ...]:
    examples: list[BenchmarkExample] = []
    for task in tasks:
        current_task_id = string_value(task.get("id"))
        actual = actual_spans.get(current_task_id)
        if actual is None or actual.duration_minutes <= 0:
            continue
        commits = frozenset(
            string_value(commit.get("commit"))
            for commit in actual.commits
            if string_value(commit.get("commit"))
        )
        examples.append(
            BenchmarkExample(
                task_id=current_task_id,
                task=task,
                actual=actual,
                commits=commits,
            )
        )
    return tuple(sorted(examples, key=lambda example: example.task_id))


def build_benchmark_folds(
    examples: tuple[BenchmarkExample, ...],
) -> tuple[BenchmarkFold, ...]:
    if not examples:
        return ()
    groups = shared_commit_groups(examples)
    fold_count = min(MAX_FOLDS, len(groups))
    fold_groups: list[list[tuple[BenchmarkExample, ...]]] = [
        [] for _ in range(fold_count)
    ]
    for index, group in enumerate(sorted(groups, key=group_stable_key)):
        fold_groups[index % fold_count].append(group)

    by_task = {example.task_id: example for example in examples}
    folds: list[BenchmarkFold] = []
    for index, groups_for_fold in enumerate(fold_groups):
        validation_ids = tuple(
            sorted(example.task_id for group in groups_for_fold for example in group)
        )
        validation_commits = tuple(
            sorted(
                {
                    commit
                    for task_id in validation_ids
                    for commit in by_task[task_id].commits
                }
            )
        )
        validation_commit_set = set(validation_commits)
        training_ids: list[str] = []
        excluded_shared: list[str] = []
        for example in examples:
            if example.task_id in validation_ids:
                continue
            if example.commits & validation_commit_set:
                excluded_shared.append(example.task_id)
                continue
            training_ids.append(example.task_id)
        training_commit_set = {
            commit for task_id in training_ids for commit in by_task[task_id].commits
        }
        folds.append(
            BenchmarkFold(
                fold_id=f"fold-{index + 1}",
                validation_task_ids=validation_ids,
                validation_commits=validation_commits,
                training_task_ids=tuple(sorted(training_ids)),
                excluded_shared_commit_task_ids=tuple(sorted(excluded_shared)),
                leakage_free=(
                    not set(validation_ids) & set(training_ids)
                    and not validation_commit_set & training_commit_set
                ),
            )
        )
    return tuple(folds)


def shared_commit_groups(
    examples: tuple[BenchmarkExample, ...],
) -> tuple[tuple[BenchmarkExample, ...], ...]:
    by_task = {example.task_id: example for example in examples}
    adjacent: dict[str, set[str]] = {example.task_id: set() for example in examples}
    by_commit: dict[str, list[str]] = {}
    for example in examples:
        for commit in example.commits:
            by_commit.setdefault(commit, []).append(example.task_id)
    for task_ids in by_commit.values():
        for task_id in task_ids:
            adjacent[task_id].update(other for other in task_ids if other != task_id)
    groups: list[tuple[BenchmarkExample, ...]] = []
    seen: set[str] = set()
    for task_id in sorted(by_task):
        if task_id in seen:
            continue
        stack = [task_id]
        component: list[str] = []
        seen.add(task_id)
        while stack:
            current = stack.pop()
            component.append(current)
            for other in sorted(adjacent[current]):
                if other in seen:
                    continue
                seen.add(other)
                stack.append(other)
        groups.append(tuple(by_task[current] for current in sorted(component)))
    return tuple(groups)


def group_stable_key(group: tuple[BenchmarkExample, ...]) -> str:
    parts = [
        "tasks",
        *sorted(example.task_id for example in group),
        "commits",
        *sorted({commit for example in group for commit in example.commits}),
    ]
    return stable_digest(parts)


def duration_benchmark_candidate_configs(
    generator_config: PlanningAnalyticsDurationModelConfig,
) -> tuple[PlanningAnalyticsDurationModelConfig, ...]:
    default = PlanningAnalyticsDurationModelConfig()
    variants = (
        generator_config,
        default,
        dataclasses.replace(
            default,
            similarity_blend_weight=0.0,
            similarity_max_examples=0,
        ),
        dataclasses.replace(default, group_min_sample_count=3),
        dataclasses.replace(default, similarity_min_score=0.5),
    )
    by_id = {duration_model_id(variant): variant for variant in variants}
    return tuple(by_id[key] for key in sorted(by_id))


def evaluate_duration_candidate(
    candidate: PlanningAnalyticsDurationModelConfig,
    examples: tuple[BenchmarkExample, ...],
    folds: tuple[BenchmarkFold, ...],
) -> dict[str, object]:
    predictions: list[dict[str, object]] = []
    by_task = {example.task_id: example for example in examples}
    for fold in folds:
        training_examples = [by_task[task_id] for task_id in fold.training_task_ids]
        training_actuals = fold_training_actuals(fold, by_task)
        model = DurationBaselineModel(
            [example.task for example in training_examples],
            training_actuals,
            candidate,
        )
        for task_id in fold.validation_task_ids:
            example = by_task[task_id]
            estimate = model.estimate(example.task).to_json()
            predictions.append(
                prediction_payload(
                    fold.fold_id,
                    example,
                    estimate,
                )
            )
    metrics = duration_metrics(predictions)
    return {
        "id": duration_model_id(candidate),
        "name": candidate.name,
        "parameters": model_parameters(candidate),
        "metrics": metrics,
        "worst_misses": worst_misses(predictions),
    }


def fold_training_actuals(
    fold: BenchmarkFold,
    by_task: dict[str, BenchmarkExample],
) -> dict[str, ActualSpan]:
    training_examples = [by_task[task_id] for task_id in fold.training_task_ids]
    previous_time_by_commit = previous_time_by_training_commit(training_examples)
    return {
        example.task_id: fold_local_actual_span(example, previous_time_by_commit)
        for example in training_examples
    }


def previous_time_by_training_commit(
    training_examples: list[BenchmarkExample],
) -> dict[str, datetime | None]:
    ordered = sorted(
        {
            (commit_hash, parse_time(author_time))
            for example in training_examples
            for commit_hash, author_time in commit_times(example)
        },
        key=lambda item: (item[1], item[0]),
    )
    previous: dict[str, datetime | None] = {}
    last_time: datetime | None = None
    for commit_hash, author_time in ordered:
        previous[commit_hash] = last_time
        last_time = author_time
    return previous


def fold_local_actual_span(
    example: BenchmarkExample,
    previous_time_by_commit: dict[str, datetime | None],
) -> ActualSpan:
    commit_payloads = sorted(
        (payload for payload in example.actual.commits if commit_payload_hash(payload)),
        key=lambda payload: (
            parse_time(string_value(payload.get("author_time"))),
            commit_payload_hash(payload),
        ),
    )
    if not commit_payloads:
        return example.actual

    start_candidates: list[datetime] = []
    raw_duration = 0
    duration = 0
    clipped_total = 0
    rebuilt_payloads: list[dict[str, object]] = []
    mapping_sources: set[str] = set()
    for payload in commit_payloads:
        commit_hash = commit_payload_hash(payload)
        author_time = parse_time(string_value(payload.get("author_time")))
        previous = previous_time_by_commit.get(commit_hash)
        if previous is None:
            raw_gap = FIRST_COMMIT_FLOOR_MINUTES
            clipped_gap = FIRST_COMMIT_FLOOR_MINUTES
        else:
            raw_gap = max(0, round_minutes(author_time - previous))
            clipped_gap = min(raw_gap, ACTUAL_IDLE_GAP_CLIP_MINUTES)
        raw_duration += raw_gap
        duration += clipped_gap
        clipped_total += raw_gap - clipped_gap
        start_candidates.append(author_time - timedelta(minutes=clipped_gap))
        sources = [string_value(source) for source in payload_sources(payload)]
        mapping_sources.update(source for source in sources if source)
        rebuilt_payloads.append(
            {
                "commit": commit_hash,
                "author_time": format_time(author_time),
                "sources": sources,
            }
        )

    return ActualSpan(
        task_id=example.task_id,
        start=min(start_candidates),
        end=max(
            parse_time(string_value(payload.get("author_time")))
            for payload in commit_payloads
        ),
        duration_minutes=duration,
        raw_duration_minutes=raw_duration,
        idle_gap_clipped_minutes=clipped_total,
        commits=tuple(rebuilt_payloads),
        mapping_sources=tuple(sorted(mapping_sources)),
    )


def commit_times(example: BenchmarkExample) -> tuple[tuple[str, str], ...]:
    return tuple(
        (commit_payload_hash(payload), string_value(payload.get("author_time")))
        for payload in example.actual.commits
        if commit_payload_hash(payload) and string_value(payload.get("author_time"))
    )


def commit_payload_hash(payload: dict[str, object]) -> str:
    return string_value(payload.get("commit"))


def payload_sources(payload: dict[str, object]) -> tuple[object, ...]:
    sources = payload.get("sources")
    if isinstance(sources, list):
        return tuple(sources)
    return ()


def prediction_payload(
    fold_id: str,
    example: BenchmarkExample,
    estimate: dict[str, object],
) -> dict[str, object]:
    actual = example.actual.duration_minutes
    predicted = int(estimate["minutes"])
    absolute_error = abs(predicted - actual)
    low = int(estimate["low_minutes"])
    high = int(estimate["high_minutes"])
    return {
        "fold_id": fold_id,
        "task_id": example.task_id,
        "actual_minutes": actual,
        "predicted_minutes": predicted,
        "low_minutes": low,
        "high_minutes": high,
        "interval_covered": low <= actual <= high,
        "absolute_error_minutes": absolute_error,
        "absolute_percentage_error": round_float(
            absolute_error / actual if actual > 0 else 0.0
        ),
        "log_error": round_float(abs(math.log(predicted / actual)))
        if actual > 0 and predicted > 0
        else None,
        "bias_minutes": predicted - actual,
        "model": string_value(estimate.get("model")),
    }


def duration_metrics(predictions: list[dict[str, object]]) -> dict[str, object]:
    if not predictions:
        return {
            "validation_count": 0,
            "mae_minutes": None,
            "mape": None,
            "mean_log_error": None,
            "coverage": None,
            "bias_minutes": None,
        }
    return {
        "validation_count": len(predictions),
        "mae_minutes": round_float(
            mean(float(item["absolute_error_minutes"]) for item in predictions)
        ),
        "mape": round_float(
            mean(float(item["absolute_percentage_error"]) for item in predictions)
        ),
        "mean_log_error": round_float(
            mean(
                float(item["log_error"])
                for item in predictions
                if item["log_error"] is not None
            )
        ),
        "coverage": round_float(
            mean(1.0 if item["interval_covered"] else 0.0 for item in predictions)
        ),
        "bias_minutes": round_float(
            mean(float(item["bias_minutes"]) for item in predictions)
        ),
    }


def worst_misses(predictions: list[dict[str, object]]) -> list[dict[str, object]]:
    ordered = sorted(
        predictions,
        key=lambda item: (
            -int(item["absolute_error_minutes"]),
            string_value(item.get("task_id")),
        ),
    )
    return ordered[:MAX_WORST_MISSES]


def select_duration_candidate(
    candidate_reports: list[dict[str, object]],
    generator_config: PlanningAnalyticsDurationModelConfig,
) -> dict[str, object]:
    if not any(
        int(report["metrics"]["validation_count"]) > 0 for report in candidate_reports
    ):
        generator_id = duration_model_id(generator_config)
        for report in candidate_reports:
            if report["id"] == generator_id:
                return selected_estimator_payload(report)
    ranked = sorted(candidate_reports, key=candidate_rank_key)
    return selected_estimator_payload(ranked[0])


def selected_estimator_payload(report: dict[str, object]) -> dict[str, object]:
    return {
        "id": report["id"],
        "name": report["name"],
        "parameters": report["parameters"],
        "metrics": report["metrics"],
    }


def candidate_rank_key(report: dict[str, object]) -> tuple[object, ...]:
    metrics = report["metrics"]
    return (
        nullable_metric(metrics["mae_minutes"]),
        nullable_metric(metrics["mean_log_error"]),
        abs(float(metrics["bias_minutes"]))
        if metrics["bias_minutes"] is not None
        else math.inf,
        string_value(report["id"]),
    )


def fold_exclusion_checks(folds: tuple[BenchmarkFold, ...]) -> dict[str, object]:
    return {
        "training_excludes_validation_tasks": all(
            not set(fold.validation_task_ids) & set(fold.training_task_ids)
            for fold in folds
        ),
        "training_excludes_shared_validation_commits": all(
            fold.leakage_free for fold in folds
        ),
        "fold_count": len(folds),
    }


def duration_model_payload(
    config: PlanningAnalyticsDurationModelConfig,
) -> dict[str, object]:
    return {
        "id": duration_model_id(config),
        "name": config.name,
        "parameters": model_parameters(config),
    }


def duration_model_id(config: PlanningAnalyticsDurationModelConfig) -> str:
    parameters = model_parameters(config)
    parts = [config.name]
    parts.extend(
        f"{key}={format_parameter(parameters[key])}" for key in sorted(parameters)
    )
    return ";".join(parts)


def duration_benchmark_paths(config: VibeConfig) -> tuple[Path, Path]:
    outputs = planning_analytics_output_report(config)
    json_path = Path(str(outputs["benchmark_json"]["path"]))
    markdown_path = Path(str(outputs["benchmark_markdown"]["path"]))
    return json_path, markdown_path


def write_duration_benchmark_reports(
    config: VibeConfig,
    report: dict[str, object],
) -> tuple[Path, Path]:
    json_path, markdown_path = duration_benchmark_paths(config)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(duration_benchmark_json(report), encoding="utf-8")
    markdown_path.write_text(duration_benchmark_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def check_duration_benchmark_reports(
    config: VibeConfig,
    report: dict[str, object],
) -> list[str]:
    errors: list[str] = []
    selected = report["selected_estimator"]
    if isinstance(selected, dict) and not selected.get("matches_generator_config"):
        errors.append(
            "configured duration model does not match benchmark-selected estimator: "
            f"configured={report['duration_model']['generator_config']['id']} "
            f"selected={selected.get('id')}"
        )
    json_path, markdown_path = duration_benchmark_paths(config)
    expected = (
        (json_path, duration_benchmark_json(report), "JSON"),
        (markdown_path, duration_benchmark_markdown(report), "Markdown"),
    )
    for path, content, label in expected:
        if not path.exists():
            errors.append(f"{label} benchmark report is missing: {path}")
            continue
        actual = path.read_text(encoding="utf-8")
        if actual != content:
            errors.append(f"{label} benchmark report is stale: {path}")
    return errors


def duration_benchmark_json(report: dict[str, object]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def duration_benchmark_markdown(report: dict[str, object]) -> str:
    selected = report["selected_estimator"]
    lines = [
        "# Duration Model Benchmark",
        "",
        f"Generated by `{report['generated_by']}`.",
        "",
        "## Selected Estimator",
        "",
        f"- Estimator: `{selected['id']}`",
        f"- Matches generator config: `{str(selected['matches_generator_config']).lower()}`",
        "",
        "## Candidate Metrics",
        "",
        "| Estimator | Validation | MAE | MAPE | Mean log error | Coverage | Bias |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for candidate in report["candidates"]:
        metrics = candidate["metrics"]
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{candidate['id']}`",
                    str(metrics["validation_count"]),
                    markdown_metric(metrics["mae_minutes"]),
                    markdown_metric(metrics["mape"]),
                    markdown_metric(metrics["mean_log_error"]),
                    markdown_metric(metrics["coverage"]),
                    markdown_metric(metrics["bias_minutes"]),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Folds",
            "",
            "| Fold | Validation tasks | Training tasks | Validation commits | Leakage free |",
            "| --- | --- | --- | ---: | --- |",
        ]
    )
    for fold in report["folds"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(fold["fold_id"]),
                    ", ".join(str(item) for item in fold["validation_task_ids"]),
                    ", ".join(str(item) for item in fold["training_task_ids"]),
                    str(len(fold["validation_commits"])),
                    str(fold["leakage_free"]).lower(),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Worst Misses",
            "",
            "| Task | Fold | Actual | Predicted | Absolute error | Covered |",
            "| --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    selected_candidate = next(
        candidate
        for candidate in report["candidates"]
        if candidate["id"] == selected["id"]
    )
    for miss in selected_candidate["worst_misses"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(miss["task_id"]),
                    str(miss["fold_id"]),
                    str(miss["actual_minutes"]),
                    str(miss["predicted_minutes"]),
                    str(miss["absolute_error_minutes"]),
                    str(miss["interval_covered"]).lower(),
                ]
            )
            + " |"
        )
    if not selected_candidate["worst_misses"]:
        lines.append("| none |  |  |  |  |  |")
    lines.append("")
    return "\n".join(lines)


def dedupe_warning_payloads(
    warnings: list[dict[str, object]],
) -> list[dict[str, object]]:
    seen: set[str] = set()
    deduped: list[dict[str, object]] = []
    for warning in warnings:
        marker = json.dumps(warning, sort_keys=True)
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(warning)
    return deduped


def nullable_metric(value: object) -> float:
    if value is None:
        return math.inf
    return float(value)


def markdown_metric(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def mean(values: Iterable[float]) -> float:
    materialized = list(values)
    if not materialized:
        return 0.0
    return sum(materialized) / len(materialized)


def round_float(value: float) -> float:
    return round(value, 6)


def stable_digest(parts: list[str]) -> str:
    payload = "\x00".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def format_parameter(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.12g}"
    return str(value)


def string_value(value: object) -> str:
    if value is None:
        return ""
    return str(value)
