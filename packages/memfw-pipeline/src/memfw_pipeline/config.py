"""The pipeline config file (TOML, or JSON by suffix) and stage loading.

::

    [pipeline]
    state_dir = "/srv/memfw/pipeline-state"   # lock + status, one directory per stage

    [stages.example]
    factory = "memfw_pipeline.example:factory" # module:callable(options) -> Stage
    target = "/srv/memfw/derived/example"      # the live artifact directory
    max_lag_seconds = 86400                    # freshness threshold (default one day)
    trust_paths = false                        # root-run deployments: true
    options = { source = "/srv/memfw/source" }

    [stages.example.unit]                      # only `memfw-pipeline unit` reads this
    schedule = "*-*-* 03:30:00"

Every path must be absolute. Unknown keys are rejected so a typo fails at
startup instead of silently changing behaviour.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import fsops
from ._ports import check_name
from .contract import SetupError, Stage

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
FACTORY_RE = re.compile(rf"^{_IDENT}(?:\.{_IDENT})*:{_IDENT}$")
DEFAULT_MAX_LAG_SECONDS = 86400
_PIPELINE_KEYS = frozenset({"state_dir"})
_STAGE_KEYS = frozenset({"factory", "target", "options", "max_lag_seconds", "trust_paths", "unit"})


@dataclass(frozen=True)
class StageSpec:
    name: str
    factory: str
    target: Path
    state_dir: Path
    config_path: Path
    options: Mapping[str, Any] = field(default_factory=dict)
    max_lag_seconds: int = DEFAULT_MAX_LAG_SECONDS
    trust_paths: bool = False
    unit: Mapping[str, Any] = field(default_factory=dict)

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "lock"

    @property
    def status_path(self) -> Path:
        return self.state_dir / "status.json"


@dataclass(frozen=True)
class PipelineConfig:
    path: Path
    state_dir: Path
    stages: Mapping[str, StageSpec]

    def stage(self, name: str) -> StageSpec:
        try:
            return self.stages[name]
        except KeyError:
            raise SetupError("unknown_stage") from None


def _absolute(value: object, reason: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value:
        raise SetupError(reason)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise SetupError(reason)
    return path


def _overlaps(a: Path, b: Path) -> bool:
    """``a`` and ``b`` are the same path or one lies inside the other."""
    return a == b or a in b.parents or b in a.parents


def load_config(path: Path) -> PipelineConfig:
    """Parse and validate the config; raises :class:`SetupError` with a fixed code."""
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        raw_bytes = path.read_bytes()
    except OSError:
        raise SetupError("config_unreadable") from None
    try:
        if path.suffix == ".json":
            data = json.loads(raw_bytes)
        else:
            data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise SetupError("config_invalid") from None
    if not isinstance(data, dict) or set(data) - {"pipeline", "stages"}:
        raise SetupError("config_invalid")
    pipeline = data.get("pipeline")
    if not isinstance(pipeline, dict) or set(pipeline) - _PIPELINE_KEYS:
        raise SetupError("config_invalid")
    state_dir = _absolute(pipeline.get("state_dir"), "state_dir_invalid")
    stages_raw = data.get("stages") or {}
    if not isinstance(stages_raw, dict):
        raise SetupError("config_invalid")
    stages: dict[str, StageSpec] = {}
    targets: set[Path] = set()
    for name, raw in stages_raw.items():
        try:
            check_name(name, what="stage name")
        except ValueError:
            raise SetupError("stage_name_invalid") from None
        if not isinstance(raw, dict) or set(raw) - _STAGE_KEYS:
            raise SetupError("stage_config_invalid")
        factory = raw.get("factory")
        if not isinstance(factory, str) or not FACTORY_RE.fullmatch(factory):
            raise SetupError("factory_invalid")
        target = _absolute(raw.get("target"), "target_invalid")
        if _overlaps(target, state_dir) or any(_overlaps(target, t) for t in targets):
            # a swap would carry the lock/status or another stage's artifact with it
            raise SetupError("target_invalid")
        targets.add(target)
        options = raw.get("options", {})
        unit = raw.get("unit", {})
        lag = raw.get("max_lag_seconds", DEFAULT_MAX_LAG_SECONDS)
        trust = raw.get("trust_paths", False)
        if not isinstance(options, dict) or not isinstance(unit, dict):
            raise SetupError("stage_config_invalid")
        if not isinstance(lag, int) or isinstance(lag, bool) or not 60 <= lag <= 365 * 86400:
            raise SetupError("max_lag_invalid")
        if not isinstance(trust, bool):
            raise SetupError("stage_config_invalid")
        stages[name] = StageSpec(
            name=name,
            factory=factory,
            target=target,
            state_dir=state_dir / name,
            config_path=path,
            options=options,
            max_lag_seconds=lag,
            trust_paths=trust,
            unit=unit,
        )
    return PipelineConfig(path=path, state_dir=state_dir, stages=stages)


def _module_paths(module: str) -> list[Path]:
    """Where the top-level package of ``module`` lives, found without importing it."""
    top = module.split(".", 1)[0]
    try:
        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError):
        return []
    if spec is None:
        return []
    paths = [Path(p) for p in (spec.submodule_search_locations or [])]
    if spec.origin and spec.origin not in ("built-in", "frozen"):
        paths.append(Path(spec.origin))
    return paths


def trust_failures(spec: StageSpec) -> list[str]:
    """Fixed codes for every untrusted path a root-run stage depends on."""
    failures: list[str] = []
    checks = (
        ("config_untrusted", spec.config_path),
        ("state_dir_untrusted", spec.state_dir),
        ("target_untrusted", spec.target),
    )
    for reason, path in checks:
        if not fsops.path_is_trusted(path):
            failures.append(reason)
    module = spec.factory.split(":", 1)[0]
    paths = _module_paths(module)
    if not paths or not all(fsops.path_is_trusted(p) for p in paths):
        failures.append("factory_untrusted")
    return failures


def load_stage(spec: StageSpec) -> Stage:
    """Import ``module:callable`` and call it with the stage options."""
    module_name, attr = spec.factory.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 - any import failure is a setup error
        raise SetupError("factory_unloadable") from None
    factory = getattr(module, attr, None)
    if not callable(factory):
        raise SetupError("factory_unloadable")
    try:
        stage = factory(dict(spec.options))
    except SetupError:
        raise
    except Exception:  # noqa: BLE001
        raise SetupError("factory_failed") from None
    if not isinstance(stage, Stage):
        raise SetupError("not_a_stage")
    try:
        check_name(stage.name, what="stage kind")
    except (ValueError, TypeError):
        raise SetupError("not_a_stage") from None
    return stage


__all__ = [
    "DEFAULT_MAX_LAG_SECONDS",
    "PipelineConfig",
    "StageSpec",
    "load_config",
    "load_stage",
    "trust_failures",
]
