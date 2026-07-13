"""Values crossing benchmark, solver, and runtime trust boundaries."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "basecommit",
        "beforereposetcmd",
        "dockerhubtag",
        "dockerimage",
        "failtopass",
        "goldpatch",
        "instanceid",
        "passtopass",
        "referencepatch",
        "selectedtestfiles",
        "servicedependencies",
        "testpatch",
    }
)


def _freeze_public_metadata(value: Mapping[str, Any]) -> Mapping[str, Any]:
    copied = deepcopy(dict(value))
    frozen: dict[str, Any] = {}
    for key, item in copied.items():
        if not isinstance(key, str):
            raise ValueError("public metadata keys must be strings")
        normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
        if normalized in _FORBIDDEN_PUBLIC_KEYS or normalized == "patch":
            raise ValueError(f"public metadata contains a sealed field: {key}")
        frozen[key] = _freeze_json_value(item)
    return MappingProxyType(frozen)


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_public_metadata(value)
    elif isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("public metadata floats must be finite")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("public metadata must contain JSON-like values")
    return value


@dataclass(frozen=True, slots=True)
class PublicTask:
    """The only benchmark data visible to a solver process."""

    task_id: str
    repo: str
    problem_statement: str
    hints: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if re.fullmatch(r"solver-[0-9a-f]{32}", self.task_id) is None:
            raise ValueError("public task_id must be an anonymous digest")
        if not self.repo.strip() or not self.problem_statement.strip():
            raise ValueError("public task requires repo and problem statement")
        object.__setattr__(self, "hints", tuple(str(item) for item in self.hints))
        object.__setattr__(self, "metadata", _freeze_public_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class JudgeSpec:
    """Sealed benchmark data retained by the evaluation process."""

    instance_id: str
    base_commit: str
    docker_image: str
    fail_to_pass: tuple[str, ...] = ()
    pass_to_pass: tuple[str, ...] = ()
    test_patch: str = ""

    def __post_init__(self) -> None:
        if not self.instance_id.strip():
            raise ValueError("judge spec requires instance_id")
        object.__setattr__(self, "fail_to_pass", tuple(self.fail_to_pass))
        object.__setattr__(self, "pass_to_pass", tuple(self.pass_to_pass))


@dataclass(frozen=True, slots=True)
class BenchmarkTask:
    public: PublicTask
    judge: JudgeSpec
