from __future__ import annotations

import dataclasses
import json
import re
import shlex
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from vibe_loop.config import AgentDetection, VibeConfig
from vibe_loop.locks import LockBusy, LockManager
from vibe_loop.runs import RunResult, RunStore
from vibe_loop.tasks import Task, TaskSource, build_task_source, runnable_tasks


SESSION_ID_RE = re.compile(
    r"\bsession(?:[_ -]?id)\s*[:=]\s*"
    r"(?P<session_id>[A-Za-z0-9](?:[A-Za-z0-9_.:/+-]*[A-Za-z0-9])?)\b",
    re.IGNORECASE,
)


@dataclasses.dataclass(frozen=True)
class SessionIdObservation:
    session_id: str
    source: str


@dataclasses.dataclass(frozen=True)
class StreamingCommandResult:
    exit_code: int
    session_id: str | None = None
    session_id_source: str | None = None


class SessionIdObserver:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._observation: SessionIdObservation | None = None

    @property
    def observation(self) -> SessionIdObservation | None:
        with self._lock:
            return self._observation

    def observe_line(self, line: str, stream_name: str) -> None:
        session_id = parse_worker_session_id(line)
        if session_id is None:
            return
        with self._lock:
            if self._observation is None:
                self._observation = SessionIdObservation(
                    session_id=session_id,
                    source=f"native:{stream_name}",
                )


class VibeRunner:
    def __init__(self, config: VibeConfig):
        self.config = config
        self._source: TaskSource | None = None
        self.lock_manager = LockManager(config.state_path / "locks")
        self.runs_dir = config.state_path / "runs"
        self.run_store = RunStore(config.state_path / "runs.jsonl")

    @property
    def source(self) -> TaskSource:
        if self._source is None:
            self._source = build_task_source(self.config.repo, self.config.task_source)
        return self._source

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
        return self.select_from_candidates(candidates, ask_agent=ask_agent)

    def select_from_candidates(
        self,
        candidates: list[Task],
        ask_agent: bool = False,
    ) -> Task:
        if ask_agent and len(candidates) > 1:
            report_status(
                f"asking agent to select next task from {len(candidates)} candidates"
            )
            selected = self.ask_agent_to_select(candidates)
            if selected is not None:
                report_status(f"agent selected {selected.task_id}: {selected.title}")
                return selected
            report_status("agent selection unavailable; using first ready task")
        return candidates[0]

    def ask_agent_to_select(self, candidates: list[Task]) -> Task | None:
        prompt = build_selection_prompt(candidates, self.recent_log_context())
        command_template = self.config.agent.require_selection_command()
        report_status(
            "agent selection command source: "
            f"{self.config.agent.selection_command_source}"
        )
        command = command_template.format(prompt=shlex.quote(prompt))
        try:
            result = subprocess.run(
                command,
                cwd=self.config.repo,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
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
        command_template = self.config.agent.require_command()
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        run_id = new_run_id(task.task_id)
        log_path = self.runs_dir / f"{run_id}.log"
        start_main = git_rev_parse(self.config.repo, "HEAD")
        task_lock = self.lock_manager.acquire(task.task_id, run_id)
        exit_code = 1
        message = ""
        session_id = run_id
        session_id_source = "fallback:run_id"
        try:
            command = command_template.format(task_id=task.task_id)
            with log_path.open("w", encoding="utf-8") as log:
                write_log_header(
                    log,
                    task,
                    command,
                    start_main,
                    run_id,
                    self.config.agent.command_source,
                    self.config.agent.detected,
                )
                report_status(f"running {task.task_id}: {task.title}", log)
                report_status(f"run_id={run_id}", log)
                report_status(f"log: {log_path}", log)
                report_status(
                    f"agent command source: {self.config.agent.command_source}",
                    log,
                )
                report_status(
                    "detected agents: "
                    f"{format_detected_agents(self.config.agent.detected)}",
                    log,
                )
                report_status("agent command started", log)
                stream_result = run_streaming_command(
                    command,
                    self.config.repo,
                    log,
                    forward_stderr=self.config.agent.forward_stderr,
                )
                exit_code = stream_result.exit_code
                session_id = stream_result.session_id or run_id
                session_id_source = stream_result.session_id_source or "fallback:run_id"
                report_status(f"agent command exit_code={exit_code}", log)
                report_status(f"session_id={session_id}", log)
                report_status(f"session_id_source={session_id_source}", log)
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
            session_id=session_id,
            session_id_source=session_id_source,
            agent_command_source=self.config.agent.command_source,
        )
        self.record_result(result)
        report_status(
            f"recorded {classification} result for {task.task_id}: {log_path}"
        )
        return result

    def run_next(
        self, ask_agent: bool = False, exclude: set[str] | None = None
    ) -> RunResult | None:
        candidates = self.list_candidates(exclude=exclude)
        if not candidates:
            return None
        self.config.agent.require_command()
        task = self.select_from_candidates(candidates, ask_agent=ask_agent)
        try:
            return self.run_task(task)
        except LockBusy:
            report_status(f"task locked during acquire, retrying: {task.task_id}")
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
            report_status(f"completion check started: {command}", log)
            result = subprocess.run(
                command,
                cwd=self.config.repo,
                shell=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            report_status(
                f"completion check exit_code={result.returncode}: {command}", log
            )
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
        self.run_store.append_result(result)

    def recent_log_context(self, max_runs: int = 5, tail_lines: int = 80) -> str:
        return self.run_store.recent_log_context(max_runs, tail_lines)


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


def parse_worker_session_id(line: str) -> str | None:
    match = SESSION_ID_RE.search(line)
    if match is None:
        return None
    return match.group("session_id")


def write_log_header(
    log,
    task: Task,
    command: str,
    start_main: str,
    run_id: str,
    command_source: str,
    detected: AgentDetection,
) -> None:
    log.write(f"[vibe-loop] run_id={run_id}\n")
    log.write(f"[vibe-loop] task_id={task.task_id}\n")
    log.write(f"[vibe-loop] title={task.title}\n")
    log.write(f"[vibe-loop] command={command}\n")
    log.write(f"[vibe-loop] agent_command_source={command_source}\n")
    log.write(f"[vibe-loop] detected_agents={format_detected_agents(detected)}\n")
    log.write(f"[vibe-loop] start_main={start_main}\n\n")


def report_status(message: str, log: TextIO | None = None) -> None:
    line = f"[vibe-loop] {message}"
    print(line, file=sys.stderr)
    if log is not None:
        log.write(line + "\n")
        log.flush()


def run_streaming_command(
    command: str,
    cwd: Path,
    log: TextIO,
    *,
    forward_stderr: bool = False,
) -> StreamingCommandResult:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    log_lock = threading.Lock()
    session_observer = SessionIdObserver()
    stdout_thread = threading.Thread(
        target=stream_pipe,
        args=(process.stdout, log, log_lock, True, session_observer, "stdout"),
    )
    stderr_thread = threading.Thread(
        target=stream_pipe,
        args=(
            process.stderr,
            log,
            log_lock,
            forward_stderr,
            session_observer,
            "stderr",
        ),
    )
    stdout_thread.start()
    stderr_thread.start()
    exit_code = process.wait()
    stdout_thread.join()
    stderr_thread.join()
    observation = session_observer.observation
    return StreamingCommandResult(
        exit_code=exit_code,
        session_id=observation.session_id if observation else None,
        session_id_source=observation.source if observation else None,
    )


def stream_pipe(
    pipe: TextIO,
    log: TextIO,
    log_lock: threading.Lock,
    forward: bool,
    session_observer: SessionIdObserver,
    stream_name: str,
) -> None:
    try:
        for line in pipe:
            session_observer.observe_line(line, stream_name)
            if forward:
                sys.stderr.write(line)
                sys.stderr.flush()
            with log_lock:
                log.write(line)
                log.flush()
    finally:
        pipe.close()


def new_run_id(task_id: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:8]
    safe_task = "".join(
        char if char.isalnum() or char in "-._" else "_" for char in task_id
    )
    return f"{timestamp}-{safe_task}-{suffix}"


def format_detected_agents(detected: AgentDetection) -> str:
    return detected.summary()


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
