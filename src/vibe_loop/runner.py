from __future__ import annotations

import dataclasses
import json
import shlex
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from vibe_loop.config import VibeConfig
from vibe_loop.locks import LockBusy, LockManager
from vibe_loop.tasks import Task, build_task_source, runnable_tasks


@dataclasses.dataclass(frozen=True)
class RunResult:
    run_id: str
    task_id: str
    classification: str
    exit_code: int
    log_path: Path
    start_main: str
    end_main: str
    message: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "classification": self.classification,
            "exit_code": self.exit_code,
            "log": str(self.log_path),
            "start_main": self.start_main,
            "end_main": self.end_main,
            "message": self.message,
            "finished_at": datetime.now(UTC).isoformat(),
        }


class VibeRunner:
    def __init__(self, config: VibeConfig):
        self.config = config
        self.source = build_task_source(config.repo, config.task_source)
        self.lock_manager = LockManager(config.state_path / "locks")
        self.runs_dir = config.state_path / "runs"
        self.runs_jsonl = config.state_path / "runs.jsonl"

    def list_candidates(self, exclude: set[str] | None = None) -> list[Task]:
        excluded = exclude or set()
        return [
            task
            for task in runnable_tasks(
                self.source,
                self.config.task_source.runnable_statuses,
            )
            if task.task_id not in excluded
            and not self.lock_manager.is_locked(task.task_id)
        ]

    def select_task(
        self, ask_agent: bool = False, exclude: set[str] | None = None
    ) -> Task | None:
        candidates = self.list_candidates(exclude=exclude)
        if not candidates:
            return None
        if ask_agent and len(candidates) > 1:
            selected = self.ask_agent_to_select(candidates)
            if selected is not None:
                return selected
        return candidates[0]

    def ask_agent_to_select(self, candidates: list[Task]) -> Task | None:
        prompt = build_selection_prompt(candidates, self.recent_log_context())
        command = self.config.agent.selection_command.format(prompt=shlex.quote(prompt))
        try:
            result = subprocess.run(
                command,
                cwd=self.config.repo,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=900,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        task_id = parse_selected_task_id(result.stdout)
        if task_id is None:
            return None
        return next((task for task in candidates if task.task_id == task_id), None)

    def run_task(self, task: Task) -> RunResult:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        run_id = new_run_id(task.task_id)
        log_path = self.runs_dir / f"{run_id}.log"
        start_main = git_rev_parse(self.config.repo, "HEAD")
        task_lock = self.lock_manager.acquire(task.task_id, run_id)
        exit_code = 1
        message = ""
        try:
            command = self.config.agent.command.format(task_id=task.task_id)
            with log_path.open("w", encoding="utf-8") as log:
                write_log_header(log, task, command, start_main)
                process = subprocess.run(
                    command,
                    cwd=self.config.repo,
                    shell=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                exit_code = process.returncode
                log.write(f"\n[vibe-loop] agent exit_code={exit_code}\n")
                if exit_code == 0:
                    message = self.run_completion_checks(log)
        finally:
            self.lock_manager.release(task_lock)
        end_main = git_rev_parse(self.config.repo, "HEAD")
        classification = self.classify(
            task.task_id, exit_code, start_main, end_main, message
        )
        result = RunResult(
            run_id=run_id,
            task_id=task.task_id,
            classification=classification,
            exit_code=exit_code,
            log_path=log_path,
            start_main=start_main,
            end_main=end_main,
            message=message,
        )
        self.record_result(result)
        return result

    def run_next(
        self, ask_agent: bool = False, exclude: set[str] | None = None
    ) -> RunResult | None:
        task = self.select_task(ask_agent=ask_agent, exclude=exclude)
        if task is None:
            return None
        try:
            return self.run_task(task)
        except LockBusy:
            excluded = set(exclude or set())
            excluded.add(task.task_id)
            return self.run_next(ask_agent=ask_agent, exclude=excluded)

    def run_until_done(
        self,
        ask_agent: bool = False,
        max_slices: int = 0,
        continue_on_failure: bool = False,
    ) -> list[RunResult]:
        results: list[RunResult] = []
        skipped: set[str] = set()
        while max_slices <= 0 or len(results) < max_slices:
            result = self.run_next(ask_agent=ask_agent, exclude=skipped)
            if result is None:
                break
            results.append(result)
            if result.classification == "completed":
                continue
            skipped.add(result.task_id)
            if not continue_on_failure and result.classification in {
                "failed",
                "unknown",
            }:
                break
        return results

    def run_completion_checks(self, log) -> str:
        for command in self.config.completion.commands:
            log.write(f"\n[vibe-loop] completion check: {command}\n")
            result = subprocess.run(
                command,
                cwd=self.config.repo,
                shell=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            log.write(f"[vibe-loop] completion check exit_code={result.returncode}\n")
            if result.returncode != 0:
                return f"completion check failed: {command}"
        return ""

    def classify(
        self,
        task_id: str,
        exit_code: int,
        start_main: str,
        end_main: str,
        message: str,
    ) -> str:
        if exit_code != 0 or message:
            return "failed"
        task = self.source.probe(task_id)
        if task and task.status == "Done":
            return "completed"
        if task and task.status == "Gated":
            return "blocked"
        if start_main != end_main and task is None:
            return "completed"
        return "unknown"

    def record_result(self, result: RunResult) -> None:
        self.config.state_path.mkdir(parents=True, exist_ok=True)
        with self.runs_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result.to_json(), separators=(",", ":")) + "\n")

    def recent_log_context(self, max_runs: int = 5, tail_lines: int = 80) -> str:
        if not self.runs_jsonl.exists():
            return "No prior vibe-loop runs recorded."
        records = []
        for line in self.runs_jsonl.read_text(encoding="utf-8").splitlines()[
            -max_runs:
        ]:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
        chunks = ["Recent vibe-loop runs:"]
        for record in records:
            chunks.append(json.dumps(record, sort_keys=True))
            log_path = Path(str(record.get("log") or ""))
            if log_path.exists():
                chunks.append(f"Log tail for {log_path}:")
                chunks.extend(tail(log_path, tail_lines))
        return "\n".join(chunks)


def build_selection_prompt(candidates: list[Task], recent_log_context: str) -> str:
    return (
        "Choose exactly one next task from the dependency-ready, unlocked "
        "candidates. Use the recent run logs to avoid retrying a task that is "
        "blocked or just failed for a persistent reason. Return JSON only: "
        '{"task_id":"...","reason":"..."}\n\n'
        "Candidates:\n"
        f"{json.dumps([task.to_json() for task in candidates], indent=2)}\n\n"
        f"{recent_log_context}\n"
    )


def parse_selected_task_id(output: str) -> str | None:
    start = output.find("{")
    end = output.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        payload = json.loads(output[start : end + 1])
    except json.JSONDecodeError:
        return None
    task_id = payload.get("task_id") if isinstance(payload, dict) else None
    return str(task_id) if task_id else None


def write_log_header(log, task: Task, command: str, start_main: str) -> None:
    log.write(f"[vibe-loop] task_id={task.task_id}\n")
    log.write(f"[vibe-loop] title={task.title}\n")
    log.write(f"[vibe-loop] command={command}\n")
    log.write(f"[vibe-loop] start_main={start_main}\n\n")


def new_run_id(task_id: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:8]
    safe_task = "".join(
        char if char.isalnum() or char in "-._" else "_" for char in task_id
    )
    return f"{timestamp}-{safe_task}-{suffix}"


def git_rev_parse(repo: Path, rev: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", rev],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def tail(path: Path, line_count: int) -> list[str]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-line_count:]
