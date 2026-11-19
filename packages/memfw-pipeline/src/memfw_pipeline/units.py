"""Hardened service-manager units for one stage: systemd (service + timer) or launchd.

The output is a template to review, never installed by this package. Settings
come from the stage's ``[stages.<name>.unit]`` table::

    python = "/opt/memfw/venv/bin/python"        # default: the running interpreter
    description = "memfw pipeline: example"
    user = "memfw"                               # systemd User= (omit: root)
    schedule = "*-*-* 03:30:00"                  # systemd OnCalendar= (default daily)
    randomized_delay_seconds = 600
    timeout_seconds = 3600                       # systemd TimeoutStartSec=
    capabilities = ["CAP_DAC_READ_SEARCH"]       # CapabilityBoundingSet= (default none)
    read_only_paths = ["/srv/memfw/authority"]
    read_write_paths = []                        # beyond the state dir and target parent
    environment = { MEMFW_EMBEDDING_URL = "http://127.0.0.1:11434/v1/embeddings" }
    launchd_label = "org.example.memfw.example"
    launchd_hour = 3                             # StartCalendarInterval
    launchd_minute = 30
    launchd_interval_seconds = 3600              # StartInterval (instead of hour/minute)
    log_dir = "/var/log/memfw"                   # launchd StandardOut/ErrorPath

Paths and values may not hold whitespace, quotes or control characters, so the
generated lines need no quoting.
"""

from __future__ import annotations

import plistlib
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .config import StageSpec
from .contract import SetupError

_SAFE_RE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]{1,512}$")
_CAP_RE = re.compile(r"^CAP_[A-Z_]{2,32}$")
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$")
_CALENDAR_RE = re.compile(r"^[A-Za-z0-9*:/,. -]{1,64}$")
_DESCRIPTION_RE = re.compile(r"^[A-Za-z0-9 _.,:()/-]{1,120}$")
_UNIT_KEYS = frozenset(
    {
        "python",
        "description",
        "user",
        "schedule",
        "randomized_delay_seconds",
        "timeout_seconds",
        "capabilities",
        "read_only_paths",
        "read_write_paths",
        "environment",
        "launchd_label",
        "launchd_hour",
        "launchd_minute",
        "launchd_interval_seconds",
        "log_dir",
    }
)
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
#: uid 0 with AF_UNIX could still connect() to these (a read-only mount does not stop
#: connect): the docker socket or the system bus would start a full-root unit.
INACCESSIBLE = (
    "-/run/docker.sock",
    "-/var/run/docker.sock",
    "-/run/containerd/containerd.sock",
    "-/run/podman/podman.sock",
    "-/run/dbus",
    "-/run/systemd/private",
)


def _safe(value: object, reason: str) -> str:
    if not isinstance(value, str) or not _SAFE_RE.fullmatch(value):
        raise SetupError(reason)
    return value


def _abs(value: object, reason: str) -> str:
    text = _safe(value, reason)
    if not text.startswith("/") or "/../" in f"{text}/":
        raise SetupError(reason)
    return text


def _int(unit: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = unit.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise SetupError("unit_invalid")
    return value


def _paths(unit: Mapping[str, Any], key: str) -> list[str]:
    value = unit.get(key, [])
    if not isinstance(value, list):
        raise SetupError("unit_invalid")
    return [_abs(item, "unit_path_invalid") for item in value]


def _env(unit: Mapping[str, Any]) -> list[tuple[str, str]]:
    value = unit.get("environment", {})
    if not isinstance(value, dict):
        raise SetupError("unit_invalid")
    out: list[tuple[str, str]] = []
    for key, item in sorted(value.items()):
        if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key) or key in PROXY_VARS:
            raise SetupError("unit_env_invalid")
        out.append((key, _safe(item, "unit_env_invalid")))
    return out


def _settings(spec: StageSpec, python: str | None) -> dict[str, Any]:
    unit = dict(spec.unit)
    if set(unit) - _UNIT_KEYS:
        raise SetupError("unit_invalid")
    interpreter = _abs(python or unit.get("python") or sys.executable, "python_invalid")
    description = unit.get("description", f"memfw pipeline: {spec.name}")
    if not isinstance(description, str) or not _DESCRIPTION_RE.fullmatch(description):
        raise SetupError("unit_invalid")
    config = _abs(str(spec.config_path), "config_path_invalid")
    return {
        "unit": unit,
        "python": interpreter,
        "description": description,
        "argv": [interpreter, "-I", "-m", "memfw_pipeline", "run", spec.name, "--config", config],
        "env": _env(unit),
    }


CAPABILITY_NOTE = """\
# Capabilities. The default is none: a stage that runs as root still cannot read
# files it does not own unless it is given CAP_DAC_READ_SEARCH (read and list).
# Some filesystems add their own ACL layer on top of the mode bits; there
# CAP_DAC_READ_SEARCH alone can fail to list another user's 0700 directories and
# CAP_DAC_OVERRIDE is needed for READS. Writes outside ReadWritePaths stay
# impossible either way (ProtectSystem=strict makes the rest read-only). Check the
# first run (`journalctl -u`): an empty build is refused by the shrink guard."""


def _require_trust(spec: StageSpec, *, as_root: bool, allow_untrusted: bool) -> None:
    """A unit that runs as root needs ``trust_paths = true`` set explicitly."""
    if as_root and spec.trust_paths is not True and not allow_untrusted:
        raise SetupError("unit_requires_trust_paths")


def systemd_units(
    spec: StageSpec, *, python: str | None = None, allow_untrusted: bool = False
) -> str:
    """A ``.service`` and a ``.timer`` for ``spec``, as one text with file headers.

    Without ``user`` the unit runs as root, so the stage must set
    ``trust_paths = true`` (or the caller passes ``allow_untrusted``).
    """
    s = _settings(spec, python)
    unit = s["unit"]
    _require_trust(spec, as_root=unit.get("user") is None, allow_untrusted=allow_untrusted)
    caps = unit.get("capabilities", [])
    if not isinstance(caps, list) or not all(
        isinstance(c, str) and _CAP_RE.fullmatch(c) for c in caps
    ):
        raise SetupError("unit_capability_invalid")
    user = unit.get("user")
    if user is not None and (not isinstance(user, str) or not _USER_RE.fullmatch(user)):
        raise SetupError("unit_invalid")
    schedule = unit.get("schedule", "daily")
    if not isinstance(schedule, str) or not _CALENDAR_RE.fullmatch(schedule):
        raise SetupError("unit_invalid")
    delay = _int(unit, "randomized_delay_seconds", 600, 0, 86400)
    timeout = _int(unit, "timeout_seconds", 3600, 60, 7 * 86400)
    state = _abs(str(spec.state_dir), "state_dir_invalid")
    target_parent = _abs(str(spec.target.parent), "target_invalid")
    rw = [state, target_parent, *_paths(unit, "read_write_paths")]
    ro = _paths(unit, "read_only_paths")
    name = f"memfw-pipeline-{spec.name}"
    lines = [
        f"# --- {name}.service ---",
        f"# Generated by memfw-pipeline for stage {spec.name}. Review before installing.",
        "# Exit 75 = another run holds the lock (success). 1 inputs unavailable, 2 setup,",
        "# 3 build/verify/publish failed, 4 a post-publish step failed, 5 shrink refused:",
        "# failures, so `systemctl --failed` shows them; status.json says which.",
        "[Unit]",
        f"Description={s['description']}",
        f"RequiresMountsFor={' '.join(dict.fromkeys([state, target_parent, *ro]))}",
        "",
        "[Service]",
        "Type=oneshot",
    ]
    if user is not None:
        lines.append(f"User={user}")
    lines += [
        "UMask=0077",
        "Nice=10",
        "IOSchedulingClass=idle",
        f"UnsetEnvironment={' '.join(PROXY_VARS)}",
        "Environment=NO_PROXY=127.0.0.1,localhost,::1",
    ]
    lines += [f"Environment={key}={value}" for key, value in s["env"]]
    lines += [
        f"ExecStart={' '.join(s['argv'])}",
        "SuccessExitStatus=75",
        f"TimeoutStartSec={timeout}",
        "# Loopback network only.",
        "IPAddressDeny=any",
        "IPAddressAllow=localhost",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "# uid 0 + AF_UNIX could still connect() to these sockets and escape the sandbox.",
        f"InaccessiblePaths={' '.join(INACCESSIBLE)}",
        "NoNewPrivileges=true",
        CAPABILITY_NOTE,
        f"CapabilityBoundingSet={' '.join(caps)}",
    ]
    if user is not None and caps:
        # A non-root process gets no capability from the bounding set alone.
        lines.append(f"AmbientCapabilities={' '.join(caps)}")
    lines += [
        "ProtectSystem=strict",
        f"ReadWritePaths={' '.join(dict.fromkeys(rw))}",
    ]
    if ro:
        lines.append(f"ReadOnlyPaths={' '.join(ro)}")
    lines += [
        "ProtectHome=read-only",
        "PrivateTmp=true",
        "PrivateDevices=true",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectControlGroups=true",
        "ProtectKernelLogs=true",
        "ProtectClock=true",
        "ProtectHostname=true",
        "LockPersonality=true",
        "RestrictSUIDSGID=true",
        "RestrictRealtime=true",
        "RestrictNamespaces=true",
        "SystemCallArchitectures=native",
        "SystemCallFilter=@system-service",
        # a filtered call fails with EPERM instead of killing the run with SIGSYS
        "SystemCallErrorNumber=EPERM",
        "",
        f"# --- {name}.timer ---",
        "[Unit]",
        f"Description={s['description']} (timer)",
        "",
        "[Timer]",
        f"OnCalendar={schedule}",
        f"RandomizedDelaySec={delay}",
        "Persistent=true",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ]
    return "\n".join(lines)


def launchd_plist(
    spec: StageSpec, *, python: str | None = None, allow_untrusted: bool = False
) -> str:
    """A launchd property list for ``spec`` (StartCalendarInterval or StartInterval).

    launchd has no per-run timeout and this template sets no ``UserName``: as a
    LaunchDaemon it runs as root (so ``trust_paths = true`` is required), as a
    LaunchAgent as the logged-in user. A hung stage runs until killed by hand.
    """
    s = _settings(spec, python)
    unit = s["unit"]
    _require_trust(spec, as_root=True, allow_untrusted=allow_untrusted)
    label = unit.get("launchd_label", f"org.memfw.pipeline.{spec.name}")
    if not isinstance(label, str) or not _LABEL_RE.fullmatch(label):
        raise SetupError("unit_invalid")
    plist: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": s["argv"],
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "Umask": 0o077,
        "RunAtLoad": False,
        "EnvironmentVariables": {
            "NO_PROXY": "127.0.0.1,localhost,::1",
            **dict(s["env"]),
        },
    }
    if "launchd_interval_seconds" in unit:
        if "launchd_hour" in unit or "launchd_minute" in unit:
            raise SetupError("unit_invalid")
        plist["StartInterval"] = _int(unit, "launchd_interval_seconds", 3600, 60, 7 * 86400)
    else:
        plist["StartCalendarInterval"] = {
            "Hour": _int(unit, "launchd_hour", 3, 0, 23),
            "Minute": _int(unit, "launchd_minute", 30, 0, 59),
        }
    if "log_dir" in unit:
        log_dir = Path(_abs(unit["log_dir"], "unit_path_invalid"))
        plist["StandardOutPath"] = str(log_dir / f"memfw-pipeline-{spec.name}.log")
        plist["StandardErrorPath"] = str(log_dir / f"memfw-pipeline-{spec.name}.err")
    return plistlib.dumps(plist, sort_keys=True).decode("utf-8")


def render(
    spec: StageSpec, kind: str, *, python: str | None = None, allow_untrusted: bool = False
) -> str:
    if kind == "systemd":
        return systemd_units(spec, python=python, allow_untrusted=allow_untrusted)
    if kind == "launchd":
        return launchd_plist(spec, python=python, allow_untrusted=allow_untrusted)
    raise SetupError("unit_kind_invalid")


__all__ = ["CAPABILITY_NOTE", "launchd_plist", "render", "systemd_units"]
