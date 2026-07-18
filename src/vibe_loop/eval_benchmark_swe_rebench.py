from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


INFRASTRUCTURE_EXIT_CODE = 2
AGENT_FAILURE_EXIT_CODE = 1


class InfrastructureError(RuntimeError):
    pass


class AgentPatchError(ValueError):
    pass


@dataclass(frozen=True)
class SweRebenchContract:
    instance_id: str
    harness_path: Path
    harness_revision: str
    task: Mapping[str, object]


def canonical_record_sha256(record: Mapping[str, object]) -> str:
    payload = json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_json(path: Path, *, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InfrastructureError(f"cannot read {label} {path}: {exc}") from exc


def validate_contract_from_environment(*, inspect_image: bool) -> SweRebenchContract:
    manifest_path = required_environment_path("VIBE_LOOP_BENCHMARK_MANIFEST")
    harness_path = required_environment_path("SWE_REBENCH_V2_HARNESS")
    tasks_path = required_environment_path("SWE_REBENCH_V2_TASKS_JSON")
    instance_id = required_environment("VIBE_LOOP_BENCHMARK_INSTANCE_ID")

    manifest = require_mapping(load_json(manifest_path, label="benchmark manifest"))
    harness = require_mapping(manifest.get("harness"), label="manifest harness")
    harness_revision = require_string(
        harness.get("revision"), label="manifest harness revision"
    )
    validate_harness_checkout(harness_path, harness_revision)

    metadata = require_mapping(manifest.get("metadata"), label="manifest metadata")
    fingerprints = require_mapping(
        metadata.get("task_record_sha256"),
        label="manifest task_record_sha256",
    )
    instances = require_sequence(manifest.get("instances"), label="manifest instances")
    expected_ids = {
        require_string(
            require_mapping(item, label="manifest instance").get("instance_id"),
            label="manifest instance_id",
        )
        for item in instances
    }
    if instance_id not in expected_ids:
        raise InfrastructureError(f"instance {instance_id} is absent from the manifest")
    task = validate_task_export(tasks_path, expected_ids, fingerprints, instance_id)

    expected_image = required_environment("VIBE_LOOP_BENCHMARK_IMAGE")
    task_image = require_string(task.get("image_name"), label="task image_name")
    if task_image != expected_image:
        raise InfrastructureError(
            f"task image mismatch for {instance_id}: {task_image!r} != {expected_image!r}"
        )
    if inspect_image:
        validate_local_image(expected_image)
    return SweRebenchContract(
        instance_id=instance_id,
        harness_path=harness_path,
        harness_revision=harness_revision,
        task=task,
    )


def validate_harness_checkout(path: Path, expected_revision: str) -> None:
    evaluator = path / "scripts" / "eval.py"
    if not evaluator.is_file():
        raise InfrastructureError(f"upstream evaluator not found: {evaluator}")
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise InfrastructureError(f"cannot inspect harness revision: {exc}") from exc
    actual_revision = completed.stdout.strip()
    if completed.returncode != 0:
        raise InfrastructureError(
            f"cannot inspect harness revision: {completed.stderr.strip()}"
        )
    if actual_revision != expected_revision:
        raise InfrastructureError(
            f"harness revision mismatch: {actual_revision!r} != {expected_revision!r}"
        )


def validate_task_export(
    path: Path,
    expected_ids: set[str],
    expected_fingerprints: Mapping[str, object],
    selected_id: str,
) -> Mapping[str, object]:
    raw_tasks = require_sequence(
        load_json(path, label="task export"), label="task export"
    )
    tasks: dict[str, Mapping[str, object]] = {}
    for raw_task in raw_tasks:
        task = require_mapping(raw_task, label="task export entry")
        instance_id = require_string(task.get("instance_id"), label="task instance_id")
        if instance_id in tasks:
            raise InfrastructureError(
                f"duplicate task export instance_id: {instance_id}"
            )
        tasks[instance_id] = task
    actual_ids = set(tasks)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        unexpected = sorted(actual_ids - expected_ids)
        raise InfrastructureError(
            f"task export instance set mismatch: missing={missing} unexpected={unexpected}"
        )
    if set(expected_fingerprints) != expected_ids:
        raise InfrastructureError(
            "manifest task fingerprint set does not match instances"
        )
    for instance_id in sorted(expected_ids):
        expected = require_string(
            expected_fingerprints.get(instance_id),
            label=f"task fingerprint for {instance_id}",
        )
        actual = canonical_record_sha256(tasks[instance_id])
        if actual != expected:
            raise InfrastructureError(
                f"task fingerprint mismatch for {instance_id}: {actual} != {expected}"
            )
    return tasks[selected_id]


def validate_patch_file(path: Path, instance_id: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentPatchError(f"cannot read agent patches {path}: {exc}") from exc
    if not isinstance(payload, list) or len(payload) != 1:
        raise AgentPatchError("patches.json must contain exactly one patch entry")
    entry = payload[0]
    if not isinstance(entry, Mapping):
        raise AgentPatchError("patches.json entry must be an object")
    patch_instance_id = entry.get("instance_id")
    if patch_instance_id != instance_id:
        raise AgentPatchError(
            f"patch instance_id {patch_instance_id!r} does not match {instance_id!r}"
        )
    patch_text = entry.get("patch")
    if not isinstance(patch_text, str) or not patch_text.strip():
        raise AgentPatchError("patches.json entry requires a non-empty patch")


def run_harness(contract: SweRebenchContract, patches_path: Path) -> int:
    with tempfile.TemporaryDirectory(prefix="vibe-loop-swe-rebench-") as directory:
        temporary_root = Path(directory)
        harness_root = temporary_root / "harness"
        materialize_harness_snapshot(
            contract.harness_path,
            contract.harness_revision,
            harness_root,
        )
        task_path = temporary_root / "task.json"
        report_path = temporary_root / "report.json"
        task_path.write_text(
            json.dumps([contract.task], ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        command = [
            sys.executable,
            str(harness_root / "scripts" / "eval.py"),
            "--json",
            str(task_path),
            "--instance-ids",
            contract.instance_id,
            "--patches",
            str(patches_path),
            "--max-workers",
            "1",
            "--report-json",
            str(report_path),
        ]
        try:
            completed = subprocess.run(command, check=False)
        except OSError as exc:
            raise InfrastructureError(f"cannot run upstream evaluator: {exc}") from exc
        return classify_report(report_path, contract.instance_id, completed.returncode)


def materialize_harness_snapshot(
    harness_path: Path, revision: str, destination: Path
) -> None:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(harness_path),
                "archive",
                "--format=tar",
                revision,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise InfrastructureError(f"cannot archive pinned harness: {exc}") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise InfrastructureError(f"cannot archive pinned harness: {stderr}")
    destination.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
            archive.extractall(destination, filter="data")
    except (OSError, tarfile.TarError) as exc:
        raise InfrastructureError(f"cannot extract pinned harness: {exc}") from exc
    evaluator = destination / "scripts" / "eval.py"
    if not evaluator.is_file():
        raise InfrastructureError("pinned harness archive has no scripts/eval.py")


def classify_report(report_path: Path, instance_id: str, return_code: int) -> int:
    report = require_mapping(load_json(report_path, label="upstream report"))
    items = require_sequence(report.get("items"), label="upstream report items")
    if len(items) != 1:
        raise InfrastructureError(f"upstream report has {len(items)} items, expected 1")
    item = require_mapping(items[0], label="upstream report item")
    if item.get("instance_id") != instance_id:
        raise InfrastructureError("upstream report instance_id does not match trial")
    error = item.get("error")
    if isinstance(error, str) and error:
        raise InfrastructureError(f"upstream evaluator error: {error}")
    if item.get("exit_code") == 125:
        raise InfrastructureError("Docker failed before the container command ran")
    passed = item.get("passed_match")
    if passed is True and return_code == 0:
        return 0
    if passed is False and return_code == 1:
        return AGENT_FAILURE_EXIT_CODE
    raise InfrastructureError(
        f"inconsistent upstream outcome: passed_match={passed!r} exit={return_code}"
    )


def validate_local_image(image: str) -> None:
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", image],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise InfrastructureError(
            f"cannot inspect Docker image {image}: {exc}"
        ) from exc
    if completed.returncode != 0:
        raise InfrastructureError(
            f"required Docker image is unavailable: {image}: {completed.stderr.strip()}"
        )


def required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise InfrastructureError(f"required environment variable is unset: {name}")
    return value


def required_environment_path(name: str) -> Path:
    return Path(required_environment(name)).expanduser().resolve()


def require_mapping(
    value: object, *, label: str = "JSON value"
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InfrastructureError(f"{label} must be an object")
    return value


def require_sequence(value: object, *, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise InfrastructureError(f"{label} must be an array")
    return value


def require_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InfrastructureError(f"{label} must be a non-empty string")
    return value.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and run SWE-rebench V2")
    parser.add_argument("command", choices=("validate", "grade"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        contract = validate_contract_from_environment(inspect_image=True)
        if args.command == "validate":
            return 0
        patches_path = Path.cwd() / "patches.json"
        validate_patch_file(patches_path, contract.instance_id)
        return run_harness(contract, patches_path)
    except AgentPatchError as exc:
        print(f"agent patch rejected: {exc}", file=sys.stderr)
        return AGENT_FAILURE_EXIT_CODE
    except InfrastructureError as exc:
        print(f"SWE-rebench V2 infrastructure failure: {exc}", file=sys.stderr)
        return INFRASTRUCTURE_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
