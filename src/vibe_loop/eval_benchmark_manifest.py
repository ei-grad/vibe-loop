from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from vibe_loop.eval_benchmark import BenchmarkGraderResult, BenchmarkInstance


MANIFEST_ADAPTER_VERSION = "1.0.0"


@dataclasses.dataclass(frozen=True)
class ManifestInstance:
    instance: BenchmarkInstance
    setup_command: str | None = None
    setup_copy: tuple[tuple[Path, Path], ...] = ()
    grader_command: str | None = None
    grader_name: str = "manifest-command"
    grader_provenance: str = ""
    timeout_seconds: int = 600


class ManifestBenchmarkAdapter:
    def __init__(self, manifest_path: Path):
        self.manifest_path = manifest_path.resolve()
        payload = load_manifest_payload(self.manifest_path)
        self._name = string_value(payload.get("name")) or "manifest"
        self._version = string_value(payload.get("version")) or MANIFEST_ADAPTER_VERSION
        self._harness = mapping_value(payload.get("harness"))
        self._instances = tuple(parse_manifest_instances(payload, self.manifest_path))

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return self._version

    def list_instances(self) -> Sequence[BenchmarkInstance]:
        return [item.instance for item in self._instances]

    def setup_instance(self, instance: BenchmarkInstance, workdir: Path) -> None:
        item = self._item_for(instance.instance_id)
        for source, destination in item.setup_copy:
            target = workdir / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target)
        if item.setup_command:
            completed = subprocess.run(
                item.setup_command,
                cwd=workdir,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=item.timeout_seconds,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    "setup command failed "
                    f"exit={completed.returncode} stderr={completed.stderr.strip()}"
                )

    def grade_instance(
        self, instance: BenchmarkInstance, workdir: Path
    ) -> BenchmarkGraderResult:
        item = self._item_for(instance.instance_id)
        if not item.grader_command:
            return BenchmarkGraderResult(
                instance_id=instance.instance_id,
                passed=False,
                grader=item.grader_name,
                exit_code=-1,
                duration_seconds=0.0,
                failure_reason="manifest instance has no grader_command",
                metadata=self._grader_metadata(item),
            )
        start = time.monotonic()
        try:
            completed = subprocess.run(
                item.grader_command,
                cwd=workdir,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=item.timeout_seconds,
            )
            duration = time.monotonic() - start
        except subprocess.TimeoutExpired as exc:
            return BenchmarkGraderResult(
                instance_id=instance.instance_id,
                passed=False,
                grader=item.grader_name,
                exit_code=-1,
                duration_seconds=time.monotonic() - start,
                log=(exc.stdout or "") + (exc.stderr or ""),
                failure_reason="grader timeout",
                metadata=self._grader_metadata(item),
            )
        return BenchmarkGraderResult(
            instance_id=instance.instance_id,
            passed=completed.returncode == 0,
            grader=item.grader_name,
            exit_code=completed.returncode,
            duration_seconds=duration,
            log=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
            failure_reason="" if completed.returncode == 0 else "grader command failed",
            metadata=self._grader_metadata(item),
        )

    def teardown_instance(self, instance: BenchmarkInstance, workdir: Path) -> None:
        pass

    def _item_for(self, instance_id: str) -> ManifestInstance:
        for item in self._instances:
            if item.instance.instance_id == instance_id:
                return item
        raise KeyError(f"unknown manifest instance: {instance_id}")

    def _grader_metadata(self, item: ManifestInstance) -> dict[str, object]:
        return {
            "manifest_path": str(self.manifest_path),
            "harness": dict(self._harness),
            "grader_provenance": item.grader_provenance,
            "grader_command_configured": item.grader_command is not None,
        }


def load_manifest_payload(path: Path) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read benchmark manifest {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("benchmark manifest must be a JSON object")
    return payload


def parse_manifest_instances(
    payload: Mapping[str, object],
    manifest_path: Path,
) -> list[ManifestInstance]:
    raw_instances = payload.get("instances")
    if not isinstance(raw_instances, Sequence) or isinstance(raw_instances, str):
        raise ValueError("benchmark manifest requires an instances array")
    base = manifest_path.parent
    parsed = []
    for index, raw in enumerate(raw_instances, start=1):
        table = mapping_value(raw)
        if not table:
            raise ValueError(f"benchmark manifest instance {index} must be an object")
        instance_id = required_string(table, "instance_id", index)
        dataset = required_string(table, "dataset", index)
        split = required_string(table, "split", index)
        setup = mapping_value(table.get("setup"))
        grader = mapping_value(table.get("grader"))
        parsed.append(
            ManifestInstance(
                instance=BenchmarkInstance(
                    instance_id=instance_id,
                    dataset=dataset,
                    split=split,
                    repo=string_value(table.get("repo")),
                    language=string_value(table.get("language")),
                    image=string_value(table.get("image")),
                    image_digest=string_value(table.get("image_digest")),
                    metadata=mapping_value(table.get("metadata")),
                ),
                setup_command=optional_string(setup.get("command")),
                setup_copy=parse_copy_specs(setup.get("copy"), base),
                grader_command=optional_string(grader.get("command")),
                grader_name=string_value(grader.get("name")) or "manifest-command",
                grader_provenance=string_value(grader.get("provenance")),
                timeout_seconds=integer_value(table.get("timeout_seconds")) or 600,
            )
        )
    return parsed


def parse_copy_specs(value: object, base: Path) -> tuple[tuple[Path, Path], ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise ValueError("benchmark manifest setup.copy must be an array")
    specs = []
    for index, raw in enumerate(value, start=1):
        table = mapping_value(raw)
        source = optional_string(table.get("from"))
        destination = optional_string(table.get("to"))
        if not source or not destination:
            raise ValueError(f"setup.copy entry {index} requires from and to")
        source_path = (base / source).resolve()
        try:
            source_path.relative_to(base.resolve())
        except ValueError as exc:
            raise ValueError(
                "setup.copy source must stay under manifest directory"
            ) from exc
        if not source_path.exists():
            raise ValueError(f"setup.copy source does not exist: {source}")
        destination_path = Path(destination)
        if destination_path.is_absolute() or ".." in destination_path.parts:
            raise ValueError("setup.copy destination must be a relative path")
        specs.append((source_path, destination_path))
    return tuple(specs)


def required_string(table: Mapping[str, object], key: str, index: int) -> str:
    value = optional_string(table.get(key))
    if not value:
        raise ValueError(f"benchmark manifest instance {index} requires {key}")
    return value


def optional_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    raise ValueError("benchmark manifest string fields must be strings")


def string_value(value: object) -> str:
    return optional_string(value) or ""


def integer_value(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("benchmark manifest integer fields must be integers")
    return value


def mapping_value(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}
