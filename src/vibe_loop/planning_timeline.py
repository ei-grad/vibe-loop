from __future__ import annotations

import dataclasses
import json
import math
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vibe_loop.config import PlanningAnalyticsDurationModelConfig, VibeConfig
from vibe_loop.planning_evidence import (
    DEFAULT_GIT_COMMIT_LIMIT,
    GitCommit,
    PlanningEvidence,
    collect_planning_evidence,
)
from vibe_loop.tasks import DONE_STATUS, STATUS_RANK, priority_rank


PLANNING_TIMELINE_SCHEMA_VERSION = 2
ACTUAL_IDLE_GAP_CLIP_MINUTES = 8 * 60
FIRST_COMMIT_FLOOR_MINUTES = 1
DEFAULT_PROJECTED_DURATION_MINUTES = 60
DEFAULT_PROJECTION_ANCHOR = datetime(1970, 1, 1, tzinfo=timezone.utc)
TIMELINE_COMMAND = "vibe-loop planning timeline"
DURATION_MODEL_NAME = "robust-duration-baseline-v1"
GROUP_MIN_SAMPLE_COUNT = 2
SIMILARITY_MIN_SCORE = 0.35
SIMILARITY_MAX_EXAMPLES = 3
SIMILARITY_BLEND_WEIGHT = 0.25
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_./+-]*")
TOKEN_FIELDS = ("title", "scope", "acceptance")
TRACEABILITY_TASK_FIELDS = (
    "requirement_ids",
    "spec_paths",
    "design_refs",
    "approval_state",
    "source_fingerprints",
)
TOKEN_STOPWORDS = {
    "acceptance",
    "add",
    "and",
    "are",
    "done",
    "for",
    "from",
    "later",
    "none",
    "task",
    "that",
    "the",
    "this",
    "with",
    "work",
    "works",
}


@dataclasses.dataclass(frozen=True)
class ActualSpan:
    task_id: str
    start: datetime
    end: datetime
    duration_minutes: int
    raw_duration_minutes: int
    idle_gap_clipped_minutes: int
    commits: tuple[dict[str, object], ...]
    mapping_sources: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "start": format_time(self.start),
            "end": format_time(self.end),
            "duration_minutes": self.duration_minutes,
            "raw_duration_minutes": self.raw_duration_minutes,
            "idle_gap_clip_minutes": ACTUAL_IDLE_GAP_CLIP_MINUTES,
            "idle_gap_clipped_minutes": self.idle_gap_clipped_minutes,
            "commit_count": len(self.commits),
            "commits": list(self.commits),
            "mapping_sources": list(self.mapping_sources),
            "provenance": "authoritative_commit_mappings",
        }


@dataclasses.dataclass(frozen=True)
class ProjectedSpan:
    task_id: str
    start: datetime | None
    end: datetime | None
    duration_minutes: int | None
    sequence: int | None
    ready_at: datetime | None
    blocked: bool
    blockers: tuple[str, ...]
    policy_order_key: tuple[object, ...]
    estimate: dict[str, object]

    def to_json(self) -> dict[str, object]:
        return {
            "start": format_optional_time(self.start),
            "end": format_optional_time(self.end),
            "duration_minutes": self.duration_minutes,
            "sequence": self.sequence,
            "ready_at": format_optional_time(self.ready_at),
            "blocked": self.blocked,
            "blockers": list(self.blockers),
            "policy_order_key": list(self.policy_order_key),
            "estimate": self.estimate,
        }


def build_planning_timeline(
    config: VibeConfig,
    *,
    evidence: PlanningEvidence | None = None,
    git_commit_limit: int = DEFAULT_GIT_COMMIT_LIMIT,
) -> dict[str, object]:
    if evidence is None:
        evidence = collect_planning_evidence(config, git_commit_limit=git_commit_limit)
    return PlanningTimelineBuilder(config, evidence).build()


class PlanningTimelineBuilder:
    def __init__(self, config: VibeConfig, evidence: PlanningEvidence):
        self.config = config
        self.evidence = evidence
        self.tasks = list(evidence.tasks)
        self.task_ids = {task_id(task) for task in self.tasks}
        self.commits = {commit.commit: commit for commit in evidence.commits}
        self.warnings: list[dict[str, object]] = [
            warning.to_json() for warning in evidence.warnings
        ]

    def build(self) -> dict[str, object]:
        completed_ids = self.completed_task_ids()
        actual_spans = self.actual_spans(completed_ids)
        self.add_completed_without_actual_warnings(completed_ids, actual_spans)
        self.add_unknown_dependency_warnings()
        self.add_stale_run_record_warnings()
        duration_model = DurationBaselineModel(
            self.tasks,
            actual_spans,
            self.config.planning_analytics.duration_model,
        )
        projections = self.projected_spans(completed_ids, actual_spans, duration_model)
        task_payloads = self.timeline_tasks(actual_spans, projections)
        return {
            "schema_version": PLANNING_TIMELINE_SCHEMA_VERSION,
            "generated_by": TIMELINE_COMMAND,
            "schedule_policy": self.config.planning_analytics.schedule_policy,
            "source_provenance": self.source_provenance(
                actual_spans,
                projections,
                duration_model,
            ),
            "sections": sections_for_tasks(task_payloads),
            "requirements": list(self.evidence.requirement_coverage),
            "tasks": task_payloads,
            "warnings": dedupe_warning_payloads(self.warnings),
        }

    def source_provenance(
        self,
        actual_spans: dict[str, ActualSpan],
        projections: dict[str, ProjectedSpan],
        duration_model: DurationBaselineModel,
    ) -> dict[str, object]:
        projected = [span for span in projections.values() if not span.blocked]
        anchor, anchor_source = projection_anchor_with_default(
            self.evidence,
            actual_spans,
        )
        return {
            "task_source_origin": self.evidence.task_source_origin,
            "git": {
                "commit_limit": self.evidence.git_commit_limit,
                "commits_collected": len(self.evidence.commits),
            },
            "worklog": {"configured": self.evidence.worklog_configured},
            "requirements": requirement_coverage_summary(
                self.evidence.requirement_coverage
            ),
            "actual": {
                "task_count": len(actual_spans),
                "idle_gap_clip_minutes": ACTUAL_IDLE_GAP_CLIP_MINUTES,
                "first_commit_floor_minutes": FIRST_COMMIT_FLOOR_MINUTES,
            },
            "projection": {
                "task_count": len(projected),
                "anchor": format_optional_time(anchor),
                "anchor_source": anchor_source,
                "duration_model": duration_model.summary(),
            },
        }

    def completed_task_ids(self) -> set[str]:
        completed = {
            task_id(task)
            for task in self.tasks
            if string_value(task.get("status")) == DONE_STATUS
        }
        for item in self.evidence.completion_evidence:
            if item.get("authoritative") is True:
                completed.add(string_value(item.get("task_id")))
        completed.discard("")
        return completed

    def actual_spans(self, completed_ids: set[str]) -> dict[str, ActualSpan]:
        mappings = {
            current_task_id: task_mappings
            for current_task_id, task_mappings in authoritative_mappings_by_task(
                self.evidence,
                self.commits,
            ).items()
            if current_task_id in completed_ids
        }
        previous_time_by_commit = previous_author_time_by_commit(
            self.evidence,
            mappings,
        )
        spans: dict[str, ActualSpan] = {}
        for item in self.tasks:
            current_task_id = task_id(item)
            task_mappings = mappings.get(current_task_id, ())
            if not task_mappings:
                continue
            commits = unique_commits_for_mappings(task_mappings, self.commits)
            if not commits:
                continue
            spans[current_task_id] = build_actual_span(
                current_task_id,
                task_mappings,
                commits,
                previous_time_by_commit,
            )
        return spans

    def projected_spans(
        self,
        completed_ids: set[str],
        actual_spans: dict[str, ActualSpan],
        duration_model: DurationBaselineModel,
    ) -> dict[str, ProjectedSpan]:
        raw_anchor, _raw_anchor_source = projection_anchor(self.evidence, actual_spans)
        anchor, _anchor_source = projection_anchor_with_default(
            self.evidence,
            actual_spans,
        )
        if raw_anchor is None:
            self.warnings.append(
                {
                    "code": "projection_anchor_missing",
                    "message": (
                        "projection anchor defaulted because no actual span or "
                        "git author time was available"
                    ),
                    "source": "timeline",
                }
            )
        known_end_times = {task: span.end for task, span in actual_spans.items()} | {
            task: anchor for task in completed_ids if task not in actual_spans
        }
        remaining = [
            task
            for task in self.tasks
            if task_id(task) not in completed_ids and string_value(task.get("status"))
        ]
        projections: dict[str, ProjectedSpan] = {}
        sequence = 0
        current_time = anchor
        while remaining:
            ready = [
                task
                for task in remaining
                if dependencies_ready(task, self.task_ids, known_end_times)
            ]
            if not ready:
                break
            ready.sort(key=lambda task: projection_sort_key(task, self.config))
            task = ready[0]
            current_task_id = task_id(task)
            dependency_ready_at = latest_dependency_end(task, known_end_times)
            start = max_datetime(current_time, dependency_ready_at, anchor)
            estimate = duration_model.estimate(task).to_json()
            end = start + timedelta(minutes=int(estimate["minutes"]))
            sequence += 1
            projections[current_task_id] = ProjectedSpan(
                task_id=current_task_id,
                start=start,
                end=end,
                duration_minutes=int(estimate["minutes"]),
                sequence=sequence,
                ready_at=dependency_ready_at or anchor,
                blocked=False,
                blockers=(),
                policy_order_key=projection_sort_key(task, self.config),
                estimate=estimate,
            )
            known_end_times[current_task_id] = end
            current_time = end
            remaining = [item for item in remaining if task_id(item) != current_task_id]
        for task in remaining:
            current_task_id = task_id(task)
            blockers = blockers_for_task(task, self.task_ids, known_end_times)
            projections[current_task_id] = ProjectedSpan(
                task_id=current_task_id,
                start=None,
                end=None,
                duration_minutes=None,
                sequence=None,
                ready_at=None,
                blocked=True,
                blockers=blockers,
                policy_order_key=projection_sort_key(task, self.config),
                estimate=duration_model.estimate(task).to_json(),
            )
            self.warnings.append(
                {
                    "code": "projected_task_blocked",
                    "message": "task could not be projected from known dependencies",
                    "task_id": current_task_id,
                    "source": "timeline",
                }
            )
        return projections

    def latest_runs_by_task(self) -> dict[str, dict[str, object]]:
        latest: dict[str, dict[str, object]] = {}
        for attempt in self.evidence.run_attempts:
            tid = string_value(attempt.get("task_id"))
            if not tid:
                continue
            existing = latest.get(tid)
            if existing is None:
                latest[tid] = dict(attempt)
            else:
                existing_index = int_value(existing.get("record_index"))
                current_index = int_value(attempt.get("record_index"))
                if current_index > existing_index:
                    latest[tid] = dict(attempt)
        return latest

    def timeline_tasks(
        self,
        actual_spans: dict[str, ActualSpan],
        projections: dict[str, ProjectedSpan],
    ) -> list[dict[str, object]]:
        latest_runs = self.latest_runs_by_task()
        payloads = []
        for task in self.tasks:
            current_task_id = task_id(task)
            actual = actual_spans.get(current_task_id)
            projected = projections.get(current_task_id)
            latest_run = latest_runs.get(current_task_id)
            payload: dict[str, object] = {
                "id": current_task_id,
                "title": string_value(task.get("title")),
                "section": string_value(task.get("section")),
                "status": string_value(task.get("status")),
                "priority": string_value(task.get("priority")),
                "dependencies": string_list(task.get("dependencies")),
                "source": {
                    "path": string_value(task.get("source")),
                    "order": int_value(task.get("order")),
                },
                "actual": actual.to_json() if actual else None,
                "projected": projected.to_json() if projected else None,
                "timeline_order": timeline_order(task, actual, projected),
                "latest_run": _latest_run_summary(latest_run),
            }
            for field in TRACEABILITY_TASK_FIELDS:
                if field in task:
                    payload[field] = task[field]
            payloads.append(payload)
        payloads.sort(key=timeline_payload_sort_key)
        return payloads

    def add_completed_without_actual_warnings(
        self,
        completed_ids: set[str],
        actual_spans: dict[str, ActualSpan],
    ) -> None:
        for current_task_id in sorted(completed_ids - set(actual_spans)):
            if current_task_id not in self.task_ids:
                continue
            self.warnings.append(
                {
                    "code": "completed_task_without_actual_span",
                    "message": "completed task has no authoritative mapped commit span",
                    "task_id": current_task_id,
                    "source": "timeline",
                }
            )

    def add_unknown_dependency_warnings(self) -> None:
        for task in self.tasks:
            current_task_id = task_id(task)
            for dependency in string_list(task.get("dependencies")):
                if dependency in self.task_ids:
                    continue
                self.warnings.append(
                    {
                        "code": "unknown_dependency",
                        "message": (
                            f"task {current_task_id} depends on unknown task "
                            f"{dependency}"
                        ),
                        "task_id": current_task_id,
                        "dependency": dependency,
                        "source": "timeline",
                    }
                )

    def add_stale_run_record_warnings(self) -> None:
        for attempt in self.evidence.run_attempts:
            current_task_id = string_value(attempt.get("task_id"))
            if not current_task_id or current_task_id in self.task_ids:
                continue
            payload = {
                "code": "stale_run_record",
                "message": "run record references a task absent from the task source",
                "task_id": current_task_id,
                "source": "run_attempt",
            }
            run_id = string_value(attempt.get("run_id"))
            if run_id:
                payload["run_id"] = run_id
            self.warnings.append(payload)


@dataclasses.dataclass(frozen=True)
class DurationTrainingExample:
    task_id: str
    duration_minutes: int
    adjusted_duration_minutes: int
    workstream: str
    priority: str
    tokens: frozenset[str]


@dataclasses.dataclass(frozen=True)
class OutlierHandling:
    method: str
    applied: bool
    training_sample_count: int
    clipped_sample_count: int
    lower_minutes: int | None
    upper_minutes: int | None

    def to_json(self) -> dict[str, object]:
        return {
            "method": self.method,
            "applied": self.applied,
            "training_sample_count": self.training_sample_count,
            "clipped_sample_count": self.clipped_sample_count,
            "lower_minutes": self.lower_minutes,
            "upper_minutes": self.upper_minutes,
        }


@dataclasses.dataclass(frozen=True)
class DurationEstimate:
    minutes: int
    low_minutes: int
    high_minutes: int
    model: str
    sample_count: int
    training_sample_counts: dict[str, int]
    outlier_handling: OutlierHandling
    outlier_notes: tuple[str, ...]
    feature_reasons: tuple[str, ...]
    evidence_reasons: tuple[str, ...]
    reasons: tuple[str, ...]
    interval_method: str
    interval_coverage: str
    interval_sample_count: int
    features: dict[str, object]
    similarity_examples: tuple[dict[str, object], ...]

    def to_json(self) -> dict[str, object]:
        return {
            "minutes": self.minutes,
            "low_minutes": self.low_minutes,
            "high_minutes": self.high_minutes,
            "interval": {
                "low_minutes": self.low_minutes,
                "high_minutes": self.high_minutes,
                "coverage": self.interval_coverage,
                "method": self.interval_method,
                "sample_count": self.interval_sample_count,
            },
            "model": self.model,
            "sample_count": self.sample_count,
            "training_sample_counts": self.training_sample_counts,
            "outlier_handling": self.outlier_handling.to_json(),
            "outlier_notes": list(self.outlier_notes),
            "feature_reasons": list(self.feature_reasons),
            "evidence_reasons": list(self.evidence_reasons),
            "reasons": list(self.reasons),
            "features": self.features,
            "similarity_examples": list(self.similarity_examples),
        }


class DurationBaselineModel:
    def __init__(
        self,
        tasks: list[dict[str, object]],
        actual_spans: dict[str, ActualSpan],
        model_config: PlanningAnalyticsDurationModelConfig | None = None,
    ):
        self.model_config = model_config or PlanningAnalyticsDurationModelConfig()
        self.outlier_handling = build_outlier_handling(
            [
                span.duration_minutes
                for span in actual_spans.values()
                if span.duration_minutes > 0
            ]
        )
        self.examples = tuple(
            sorted(
                (
                    build_training_example(task, actual_spans, self.outlier_handling)
                    for task in tasks
                    if task_id(task) in actual_spans
                    and actual_spans[task_id(task)].duration_minutes > 0
                ),
                key=lambda item: item.task_id,
            )
        )

    def summary(self) -> dict[str, object]:
        return {
            "model": self.model_config.name,
            "parameters": model_parameters(self.model_config),
            "training_sample_count": len(self.examples),
            "outlier_handling": self.outlier_handling.to_json(),
            "token_fields": list(TOKEN_FIELDS),
        }

    def estimate(self, task: dict[str, object]) -> DurationEstimate:
        if not self.examples:
            return self.fallback_estimate(task)
        selected, group_name, group_reason = self.select_group(task)
        base_minutes = rounded_median(
            [example.adjusted_duration_minutes for example in selected]
        )
        similarity_examples = self.similarity_examples(task)
        similarity_minutes = (
            rounded_median(
                [
                    int(example["adjusted_duration_minutes"])
                    for example in similarity_examples
                ]
            )
            if similarity_examples
            else None
        )
        minutes = base_minutes
        model = f"{self.model_config.name}/{group_name}"
        evidence_reasons = [
            group_reason,
            outlier_reason(self.outlier_handling),
        ]
        if (
            similarity_minutes is not None
            and self.model_config.similarity_blend_weight > 0
        ):
            minutes = max(
                1,
                int(
                    round(
                        base_minutes * (1 - self.model_config.similarity_blend_weight)
                        + similarity_minutes * self.model_config.similarity_blend_weight
                    )
                ),
            )
            model = f"{model}+similarity"
            evidence_reasons.append(
                (
                    "blended leakage-safe pre-task similarity median from "
                    f"{len(similarity_examples)} historical task(s)"
                )
            )
        else:
            evidence_reasons.append(
                "similarity blend omitted because pre-task token overlap was insufficient"
            )
        low, high, interval_method, interval_coverage, interval_sample_count = (
            estimate_interval(minutes, selected, self.examples)
        )
        training_counts = self.training_sample_counts(task, len(similarity_examples))
        features = task_features(task)
        feature_reasons = feature_reason_payload(features)
        return DurationEstimate(
            minutes=minutes,
            low_minutes=low,
            high_minutes=high,
            model=model,
            sample_count=len(selected),
            training_sample_counts=training_counts,
            outlier_handling=self.outlier_handling,
            outlier_notes=(outlier_reason(self.outlier_handling),),
            feature_reasons=feature_reasons,
            evidence_reasons=tuple(evidence_reasons),
            reasons=tuple([*feature_reasons, *evidence_reasons]),
            interval_method=interval_method,
            interval_coverage=interval_coverage,
            interval_sample_count=interval_sample_count,
            features=features,
            similarity_examples=similarity_examples,
        )

    def fallback_estimate(self, task: dict[str, object]) -> DurationEstimate:
        minutes = self.model_config.fallback_minutes
        features = task_features(task)
        feature_reasons = feature_reason_payload(features)
        evidence_reasons = ("no completed actual spans; using fixed fallback duration",)
        return DurationEstimate(
            minutes=minutes,
            low_minutes=max(1, minutes // 2),
            high_minutes=minutes * 2,
            model="fixed-fallback-v1",
            sample_count=0,
            training_sample_counts={
                "global": 0,
                "workstream": 0,
                "priority": 0,
                "workstream_priority": 0,
                "similarity": 0,
            },
            outlier_handling=self.outlier_handling,
            outlier_notes=("outlier handling skipped because no history exists",),
            feature_reasons=feature_reasons,
            evidence_reasons=evidence_reasons,
            reasons=tuple([*feature_reasons, *evidence_reasons]),
            interval_method="fixed_fallback_multiplier",
            interval_coverage="conservative_small_history",
            interval_sample_count=0,
            features=features,
            similarity_examples=(),
        )

    def select_group(
        self,
        task: dict[str, object],
    ) -> tuple[tuple[DurationTrainingExample, ...], str, str]:
        workstream = workstream_value(task)
        priority = priority_value(task)
        workstream_priority = tuple(
            example
            for example in self.examples
            if example.workstream == workstream and example.priority == priority
        )
        workstream_examples = tuple(
            example for example in self.examples if example.workstream == workstream
        )
        priority_examples = tuple(
            example for example in self.examples if example.priority == priority
        )
        candidates = (
            (
                workstream_priority,
                "workstream-priority",
                (
                    "used workstream+priority median for "
                    f"workstream={workstream} priority={priority}"
                ),
            ),
            (
                workstream_examples,
                "workstream",
                f"used workstream median for workstream={workstream}",
            ),
            (
                priority_examples,
                "priority",
                f"used priority median for priority={priority}",
            ),
        )
        for examples, name, reason in candidates:
            if len(examples) >= self.model_config.group_min_sample_count:
                return stable_examples(examples), name, reason
        return (
            self.examples,
            "global",
            "used global median because specific history was too small",
        )

    def similarity_examples(
        self,
        task: dict[str, object],
    ) -> tuple[dict[str, object], ...]:
        target_tokens = pre_task_tokens(task)
        if (
            not target_tokens
            or self.model_config.similarity_blend_weight <= 0
            or self.model_config.similarity_max_examples <= 0
        ):
            return ()
        scored: list[tuple[float, DurationTrainingExample]] = []
        for example in self.examples:
            score = token_similarity(target_tokens, example.tokens)
            if score >= self.model_config.similarity_min_score:
                scored.append((score, example))
        scored.sort(key=lambda item: (-item[0], item[1].task_id))
        return tuple(
            {
                "task_id": example.task_id,
                "score": round(score, 6),
                "duration_minutes": example.duration_minutes,
                "adjusted_duration_minutes": example.adjusted_duration_minutes,
            }
            for score, example in scored[: self.model_config.similarity_max_examples]
        )

    def training_sample_counts(
        self,
        task: dict[str, object],
        similarity_count: int,
    ) -> dict[str, int]:
        workstream = workstream_value(task)
        priority = priority_value(task)
        return {
            "global": len(self.examples),
            "workstream": sum(
                1 for example in self.examples if example.workstream == workstream
            ),
            "priority": sum(
                1 for example in self.examples if example.priority == priority
            ),
            "workstream_priority": sum(
                1
                for example in self.examples
                if example.workstream == workstream and example.priority == priority
            ),
            "similarity": similarity_count,
        }


def build_training_example(
    task: dict[str, object],
    actual_spans: dict[str, ActualSpan],
    outlier_handling: OutlierHandling,
) -> DurationTrainingExample:
    span = actual_spans[task_id(task)]
    adjusted = winsorized_minutes(span.duration_minutes, outlier_handling)
    return DurationTrainingExample(
        task_id=task_id(task),
        duration_minutes=span.duration_minutes,
        adjusted_duration_minutes=adjusted,
        workstream=workstream_value(task),
        priority=priority_value(task),
        tokens=frozenset(pre_task_tokens(task)),
    )


def model_parameters(config: PlanningAnalyticsDurationModelConfig) -> dict[str, object]:
    return {
        "group_min_sample_count": config.group_min_sample_count,
        "similarity_min_score": config.similarity_min_score,
        "similarity_max_examples": config.similarity_max_examples,
        "similarity_blend_weight": config.similarity_blend_weight,
        "fallback_minutes": config.fallback_minutes,
    }


def build_outlier_handling(durations: list[int]) -> OutlierHandling:
    positive = sorted(duration for duration in durations if duration > 0)
    if not positive:
        return OutlierHandling(
            method="none",
            applied=False,
            training_sample_count=0,
            clipped_sample_count=0,
            lower_minutes=None,
            upper_minutes=None,
        )
    lower, upper, applied = log_space_winsor_bounds(positive)
    clipped = sum(1 for duration in positive if duration < lower or duration > upper)
    return OutlierHandling(
        method="log_space_iqr_winsorization",
        applied=applied,
        training_sample_count=len(positive),
        clipped_sample_count=clipped,
        lower_minutes=lower,
        upper_minutes=upper,
    )


def log_space_winsor_bounds(durations: list[int]) -> tuple[int, int, bool]:
    if len(durations) < 4:
        return min(durations), max(durations), False
    logs = sorted(math.log(duration) for duration in durations)
    first_quartile = percentile(logs, 0.25)
    third_quartile = percentile(logs, 0.75)
    iqr = third_quartile - first_quartile
    if iqr == 0:
        center = statistics.median(logs)
        lower_log = center - math.log(4)
        upper_log = center + math.log(4)
    else:
        lower_log = first_quartile - 1.5 * iqr
        upper_log = third_quartile + 1.5 * iqr
    lower = max(1, int(round(math.exp(lower_log))))
    upper = max(lower, int(round(math.exp(upper_log))))
    return lower, upper, True


def winsorized_minutes(duration: int, outlier_handling: OutlierHandling) -> int:
    if not outlier_handling.applied:
        return duration
    lower = outlier_handling.lower_minutes
    upper = outlier_handling.upper_minutes
    if lower is None or upper is None:
        return duration
    return min(max(duration, lower), upper)


def estimate_interval(
    minutes: int,
    selected: tuple[DurationTrainingExample, ...],
    global_examples: tuple[DurationTrainingExample, ...],
) -> tuple[int, int, str, str, int]:
    interval_examples = selected if len(selected) >= 3 else global_examples
    values = sorted(example.adjusted_duration_minutes for example in interval_examples)
    if len(values) >= 3:
        low = max(1, int(math.floor(percentile(values, 0.10))))
        high = max(low, int(math.ceil(percentile(values, 0.90))))
        return (
            min(low, minutes),
            max(high, minutes),
            "p10_p90_adjusted_history",
            "conservative_80_percent",
            len(values),
        )
    low = max(1, int(math.floor(minutes * 0.5)))
    high = max(low, int(math.ceil(minutes * 2)))
    return (
        min(low, minutes),
        max(high, minutes),
        "small_sample_multiplier",
        "conservative_small_history",
        len(values),
    )


def rounded_median(values: list[int]) -> int:
    if not values:
        return DEFAULT_PROJECTED_DURATION_MINUTES
    return max(1, int(round(statistics.median(sorted(values)))))


def stable_examples(
    examples: tuple[DurationTrainingExample, ...],
) -> tuple[DurationTrainingExample, ...]:
    return tuple(sorted(examples, key=lambda example: example.task_id))


def task_features(task: dict[str, object]) -> dict[str, object]:
    return {
        "workstream": workstream_value(task),
        "priority": priority_value(task),
        "pre_task_token_count": len(pre_task_tokens(task)),
        "token_fields": list(TOKEN_FIELDS),
    }


def feature_reason_payload(features: dict[str, object]) -> tuple[str, ...]:
    return (
        f"workstream feature={features['workstream']}",
        f"priority feature={features['priority']}",
        (
            "pre-task text features from "
            + ", ".join(string_list(features.get("token_fields")))
        ),
    )


def outlier_reason(outlier_handling: OutlierHandling) -> str:
    if not outlier_handling.applied:
        return (
            "log-space winsorization skipped because fewer than four completed "
            "durations were available"
        )
    clipped = outlier_handling.clipped_sample_count
    return (
        "log-space winsorization applied to "
        f"{outlier_handling.training_sample_count} completed duration(s); "
        f"{clipped} sample(s) clipped"
    )


def workstream_value(task: dict[str, object]) -> str:
    return string_value(task.get("section")) or "default"


def priority_value(task: dict[str, object]) -> str:
    return string_value(task.get("priority")) or "unprioritized"


def pre_task_tokens(task: dict[str, object]) -> set[str]:
    tokens: set[str] = set()
    for field in TOKEN_FIELDS:
        for match in TOKEN_RE.finditer(string_value(task.get(field)).casefold()):
            token = match.group(0).strip("_./+-")
            if len(token) < 3 or token in TOKEN_STOPWORDS:
                continue
            tokens.add(token)
    return tokens


def token_similarity(
    left: set[str] | frozenset[str], right: set[str] | frozenset[str]
) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    if intersection == 0:
        return 0.0
    return intersection / len(left | right)


def percentile(values: list[float] | list[int], quantile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * quantile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    lower = ordered[lower_index]
    upper = ordered[upper_index]
    return lower + (upper - lower) * (position - lower_index)


def authoritative_mappings_by_task(
    evidence: PlanningEvidence,
    commits: dict[str, GitCommit],
) -> dict[str, tuple[dict[str, object], ...]]:
    by_task: dict[str, list[dict[str, object]]] = {}
    for mapping in evidence.commit_mappings:
        if mapping.get("authoritative") is not True:
            continue
        commit = string_value(mapping.get("commit"))
        current_task_id = string_value(mapping.get("task_id"))
        if not commit or commit not in commits or not current_task_id:
            continue
        by_task.setdefault(current_task_id, []).append(mapping)
    return {task: tuple(mappings) for task, mappings in by_task.items()}


def previous_author_time_by_commit(
    evidence: PlanningEvidence,
    mappings: dict[str, tuple[dict[str, object], ...]],
) -> dict[str, datetime | None]:
    mapped_commits = {
        string_value(mapping.get("commit"))
        for task_mappings in mappings.values()
        for mapping in task_mappings
    }
    ordered = sorted(
        (
            (commit.commit, parse_time(commit.author_time))
            for commit in evidence.commits
            if commit.commit in mapped_commits
            and parse_time_or_none(commit.author_time) is not None
        ),
        key=lambda item: (item[1], item[0]),
    )
    previous: dict[str, datetime | None] = {}
    last_time: datetime | None = None
    for commit, author_time in ordered:
        previous[commit] = last_time
        last_time = author_time
    return previous


def unique_commits_for_mappings(
    mappings: tuple[dict[str, object], ...],
    commits: dict[str, GitCommit],
) -> tuple[GitCommit, ...]:
    selected = {
        string_value(mapping.get("commit")): commits[
            string_value(mapping.get("commit"))
        ]
        for mapping in mappings
        if string_value(mapping.get("commit")) in commits
    }
    return tuple(
        sorted(
            selected.values(),
            key=lambda commit: (parse_time(commit.author_time), commit.commit),
        )
    )


def build_actual_span(
    current_task_id: str,
    mappings: tuple[dict[str, object], ...],
    commits: tuple[GitCommit, ...],
    previous_time_by_commit: dict[str, datetime | None],
) -> ActualSpan:
    start_candidates: list[datetime] = []
    raw_duration = 0
    duration = 0
    clipped_total = 0
    commit_payloads: list[dict[str, object]] = []
    sources_by_commit = mapping_sources_by_commit(mappings)
    for commit in commits:
        author_time = parse_time(commit.author_time)
        previous = previous_time_by_commit.get(commit.commit)
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
        commit_payloads.append(
            {
                "commit": commit.commit,
                "author_time": format_time(author_time),
                "sources": list(sources_by_commit.get(commit.commit, ())),
            }
        )
    start = min(start_candidates)
    end = max(parse_time(commit.author_time) for commit in commits)
    return ActualSpan(
        task_id=current_task_id,
        start=start,
        end=end,
        duration_minutes=duration,
        raw_duration_minutes=raw_duration,
        idle_gap_clipped_minutes=clipped_total,
        commits=tuple(commit_payloads),
        mapping_sources=tuple(
            sorted(
                {
                    source
                    for sources in sources_by_commit.values()
                    for source in sources
                    if source
                }
            )
        ),
    )


def mapping_sources_by_commit(
    mappings: tuple[dict[str, object], ...],
) -> dict[str, tuple[str, ...]]:
    sources: dict[str, list[str]] = {}
    for mapping in mappings:
        commit = string_value(mapping.get("commit"))
        source = string_value(mapping.get("source"))
        if not commit or not source:
            continue
        sources.setdefault(commit, []).append(source)
    return {
        commit: tuple(sorted(dict.fromkeys(commit_sources)))
        for commit, commit_sources in sources.items()
    }


def projection_anchor(
    evidence: PlanningEvidence,
    actual_spans: dict[str, ActualSpan],
) -> tuple[datetime | None, str]:
    if actual_spans:
        return max(span.end for span in actual_spans.values()), "latest_actual_end"
    commit_times = [
        parse_time(commit.author_time)
        for commit in evidence.commits
        if parse_time_or_none(commit.author_time) is not None
    ]
    if commit_times:
        return max(commit_times), "latest_git_author_time"
    return None, "missing"


def projection_anchor_with_default(
    evidence: PlanningEvidence,
    actual_spans: dict[str, ActualSpan],
) -> tuple[datetime, str]:
    anchor, anchor_source = projection_anchor(evidence, actual_spans)
    if anchor is not None:
        return anchor, anchor_source
    return DEFAULT_PROJECTION_ANCHOR, "default_epoch_no_actual_or_git_evidence"


def dependencies_ready(
    task: dict[str, object],
    task_ids: set[str],
    known_end_times: dict[str, datetime],
) -> bool:
    for dependency in string_list(task.get("dependencies")):
        if dependency not in task_ids:
            return False
        if dependency not in known_end_times:
            return False
    return True


def latest_dependency_end(
    task: dict[str, object],
    known_end_times: dict[str, datetime],
) -> datetime | None:
    end_times = [
        known_end_times[dependency]
        for dependency in string_list(task.get("dependencies"))
        if dependency in known_end_times
    ]
    if not end_times:
        return None
    return max(end_times)


def blockers_for_task(
    task: dict[str, object],
    task_ids: set[str],
    known_end_times: dict[str, datetime],
) -> tuple[str, ...]:
    blockers: list[str] = []
    for dependency in string_list(task.get("dependencies")):
        if dependency not in task_ids:
            blockers.append(f"unknown_dependency:{dependency}")
        elif dependency not in known_end_times:
            blockers.append(f"unscheduled_dependency:{dependency}")
    return tuple(blockers or ("unscheduled_dependency_cycle",))


def projection_sort_key(
    task: dict[str, object],
    config: VibeConfig,
) -> tuple[object, ...]:
    status = string_value(task.get("status"))
    priority = string_value(task.get("priority"))
    order = int_value(task.get("order"))
    if config.planning_analytics.schedule_policy == "lightmetrics-parity":
        return (
            0 if status == "Active" else 1,
            priority_rank(priority),
            STATUS_RANK.get(status, 9),
            order,
            task_id(task),
        )
    return (
        STATUS_RANK.get(status, 9),
        priority_rank(priority),
        order,
        task_id(task),
    )


def timeline_order(
    task: dict[str, object],
    actual: ActualSpan | None,
    projected: ProjectedSpan | None,
) -> dict[str, object]:
    if actual is not None:
        return {
            "kind": "actual",
            "start": format_time(actual.start),
            "sequence": None,
        }
    if projected is not None and projected.sequence is not None:
        return {
            "kind": "projected",
            "start": format_optional_time(projected.start),
            "sequence": projected.sequence,
        }
    return {
        "kind": "unscheduled",
        "start": None,
        "sequence": None,
        "source_order": int_value(task.get("order")),
    }


def timeline_payload_sort_key(payload: dict[str, object]) -> tuple[object, ...]:
    order = payload.get("timeline_order")
    order_payload = order if isinstance(order, dict) else {}
    kind = string_value(order_payload.get("kind"))
    kind_rank = {"actual": 0, "projected": 1, "unscheduled": 2}.get(kind, 9)
    start = string_value(order_payload.get("start"))
    sequence = int_value(order_payload.get("sequence"))
    source = payload.get("source")
    source_order = int_value(source.get("order")) if isinstance(source, dict) else 0
    return (kind_rank, start, sequence, source_order, string_value(payload.get("id")))


def sections_for_tasks(tasks: list[dict[str, object]]) -> list[dict[str, object]]:
    sections: dict[str, dict[str, object]] = {}
    for task in tasks:
        name = string_value(task.get("section"))
        section_id = name or "default"
        section = sections.setdefault(
            section_id,
            {
                "id": section_id,
                "title": name or "Tasks",
                "order": int_value(
                    task.get("source", {}).get("order")
                    if isinstance(task.get("source"), dict)
                    else 0
                ),
                "task_ids": [],
            },
        )
        task_ids = section["task_ids"]
        assert isinstance(task_ids, list)
        task_ids.append(string_value(task.get("id")))
    for section in sections.values():
        task_ids = section["task_ids"]
        assert isinstance(task_ids, list)
        section["task_ids"] = sorted(task_ids, key=task_position(tasks))
    return sorted(
        sections.values(),
        key=lambda item: (int_value(item["order"]), item["id"]),
    )


def task_position(tasks: list[dict[str, object]]):
    positions = {
        string_value(task.get("id")): index for index, task in enumerate(tasks)
    }
    return lambda task: positions.get(task, 0)


def requirement_coverage_summary(
    requirement_coverage: tuple[dict[str, object], ...],
) -> dict[str, object]:
    by_status: dict[str, int] = {}
    for item in requirement_coverage:
        status = string_value(item.get("status")) or "unknown"
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "count": len(requirement_coverage),
        "by_status": dict(sorted(by_status.items())),
    }


def max_datetime(*values: datetime | None) -> datetime:
    concrete = [value for value in values if value is not None]
    if not concrete:
        raise ValueError("max_datetime requires at least one datetime")
    return max(concrete)


def parse_time(value: str) -> datetime:
    parsed = parse_time_or_none(value)
    if parsed is None:
        raise ValueError(f"invalid timestamp: {value}")
    return parsed


def parse_time_or_none(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def format_optional_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return format_time(value)


def round_minutes(delta: timedelta) -> int:
    return int(round(delta.total_seconds() / 60))


def task_id(task: dict[str, object]) -> str:
    return string_value(task.get("id"))


def lookup_timeline_task(
    timeline: dict[str, object],
    target_task_id: str,
) -> dict[str, object] | None:
    tasks = timeline.get("tasks")
    if not isinstance(tasks, list):
        return None
    for task_payload in tasks:
        if not isinstance(task_payload, dict):
            continue
        if string_value(task_payload.get("id")) == target_task_id:
            return {
                "task_id": target_task_id,
                "status": string_value(task_payload.get("status")),
                "has_actual": task_payload.get("actual") is not None,
                "has_projected": task_payload.get("projected") is not None,
                "latest_run": task_payload.get("latest_run"),
            }
    return None


def read_timeline_file(path: Path) -> dict[str, object] | None:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _latest_run_summary(
    attempt: dict[str, object] | None,
) -> dict[str, object] | None:
    if attempt is None:
        return None
    run_id = string_value(attempt.get("run_id"))
    if not run_id:
        return None
    return {
        "run_id": run_id,
        "status": string_value(attempt.get("status")),
        "finished_at": (
            string_value(attempt.get("finished_at"))
            or string_value(attempt.get("reported_at"))
        ),
        "log": string_value(attempt.get("log")),
    }


def string_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def int_value(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


def dedupe_warning_payloads(
    warnings: list[dict[str, object]],
) -> list[dict[str, object]]:
    seen: set[str] = set()
    deduped: list[dict[str, object]] = []
    for warning in warnings:
        key = json.dumps(warning, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(warning)
    return sorted(
        deduped,
        key=lambda item: (
            string_value(item.get("code")),
            string_value(item.get("task_id")),
            string_value(item.get("commit")),
            string_value(item.get("dependency")),
            string_value(item.get("run_id")),
        ),
    )
