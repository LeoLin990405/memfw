"""Regressions for the retrieval blockers that stayed open through R4."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from memfw.retrieval.judge import LocalJudge, judge_from_environment

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "http://127.0.0.1:9/judge"


def _pair(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add(
        "Public weather",
        "weather forecast bulletin",
        memory_id="public",
        tenant="",
    )
    repository.add(
        "Private weather",
        "weather forecast alpha-private",
        memory_id="secret",
        tenant="alpha",
    )
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


def _ids(result: dict) -> list[str]:
    return [item["id"] for item in result["results"]]


def test_empty_tenant_is_not_every_tenant(tmp_path: Path) -> None:
    repository = _pair(tmp_path)
    gateway = Gateway(repository.root, tenant="", entity_enabled=False)
    for mode in ("fts", "hybrid", "multihop"):
        found = gateway.search("weather forecast", mode=mode)
        assert "secret" not in _ids(found), mode
        assert "public" in _ids(found), mode
    with pytest.raises(ValueError, match="not visible"):
        gateway.get("secret")
    direct = gateway.search("secret", mode="multihop")
    assert "secret" not in _ids(direct)
    expanded = gateway.search("Where is the weather forecast bulletin", mode="multihop")
    assert "secret" not in _ids(expanded)


def test_named_tenant_sees_only_itself(tmp_path: Path) -> None:
    repository = _pair(tmp_path)
    gateway = Gateway(repository.root, tenant="alpha", entity_enabled=False)
    for mode in ("fts", "hybrid", "multihop"):
        found = gateway.search("weather", mode=mode)
        assert _ids(found) == ["secret"], mode
    assert "public" not in _ids(gateway.search("public", mode="multihop"))


def test_plain_word_is_lexical_and_a_path_is_an_id(tmp_path: Path) -> None:
    repository = _pair(tmp_path)
    gateway = Gateway(repository.root, tenant="alpha", entity_enabled=False)
    word = gateway.search("weather", mode="multihop")
    assert word["route"] == "lexical"
    assert _ids(word) == ["secret"]
    assert word["degraded"] is False
    existing = gateway.search("secret", mode="multihop")
    assert existing["route"] == "direct"
    assert _ids(existing) == ["secret"]
    missing = gateway.search("memories/missing-id.md", mode="multihop")
    assert missing["route"] == "direct"
    assert missing["results"] == []
    path = gateway.search("memories/secret.md", mode="multihop")
    assert path["route"] == "direct"
    assert _ids(path) == ["secret"]
    quoted = gateway.search('"weather forecast"', mode="multihop")
    assert quoted["route"] == "direct"
    assert _ids(quoted) == ["secret"]


def test_a_changed_file_does_not_inherit_the_live_version(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("alpha original", "alpha keyword", memory_id="record", tenant="alpha")
    gateway = Gateway(repository.root, tenant="alpha", entity_enabled=False)
    before = gateway.search("alpha")
    assert before["results"][0]["stale"] is False
    assert before["results"][0]["index_version"] == before["results"][0]["sot_version"]
    repository.update("record", title="bravo current", body="bravo replaced")
    after = gateway.search("alpha")
    hit = after["results"][0]
    assert hit["title"] == "bravo current"
    assert hit["sot_version"] == str(repository.get("record").version)
    assert hit["index_version"] != hit["sot_version"]
    assert hit["stale"] is True
    assert hit["projection_hash"]


def test_as_of_hides_a_memory_created_later(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault", clock=lambda: datetime(2026, 11, 8, tzinfo=UTC))
    repository.init()
    document = repository.add(
        "future beacon",
        "future beacon emitted today",
        memory_id="future",
        tenant="alpha",
    )
    assert document.valid_from is None
    gateway = Gateway(repository.root, tenant="alpha", entity_enabled=False)
    found = gateway.search("future beacon", mode="multihop", as_of="2025-01-01T00:00:00+00:00")
    assert _ids(found) == []


def test_configured_judge_is_not_reached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMFW_JUDGE_URL", ENDPOINT)
    monkeypatch.setenv("MEMFW_JUDGE_ALLOWLIST", ENDPOINT)
    configured = judge_from_environment()
    assert configured is not None
    assert configured.transport is not None
    calls: list[str] = []

    def explode(endpoint: str, question: str, passage: str, context: str) -> float:
        del endpoint, question, passage, context
        calls.append("called")
        raise TimeoutError("synthetic local judge timeout")

    monkeypatch.setattr("memfw.retrieval.judge.local_judge_transport", explode)
    repository = _pair(tmp_path)
    gateway = Gateway(repository.root, tenant="alpha", entity_enabled=False)
    found = gateway.search("weather", mode="multihop")
    assert calls == []
    assert found["mode"] == "multihop"
    assert _ids(found) == ["secret"]
    assert all("judge_score" not in item for item in found["results"])


def test_multihop_fallback_filters_source_and_tenant(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add(
        "Allowed bulletin",
        "alpha marker bulletin",
        memory_id="allowed",
        tenant="alpha",
        source="nas",
    )
    repository.add(
        "Blocked bulletin",
        "alpha marker bulletin",
        memory_id="blocked",
        tenant="alpha",
        source="manual",
    )
    repository.add(
        "Other tenant bulletin",
        "alpha marker bulletin",
        memory_id="other",
        tenant="beta",
        source="nas",
    )
    MemoryIndex(repository.index_path).rebuild(repository)
    gateway = Gateway(
        repository.root,
        tenant="alpha",
        entity_enabled=False,
        allowed_sources=("nas",),
    )
    found = gateway.search("alpha marker bulletin", mode="multihop")
    assert found["mode"] == "multihop"
    assert found["route"] == "expand"
    assert found["degraded"] is True
    assert _ids(found) == ["allowed"]


def test_a_bad_judge_config_does_not_break_fts_or_hybrid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMFW_JUDGE_URL", ENDPOINT)
    monkeypatch.setenv("MEMFW_JUDGE_ALLOWLIST", "")
    repository = _pair(tmp_path)
    gateway = Gateway(repository.root, tenant="alpha", entity_enabled=False)
    for mode in ("fts", "hybrid"):
        found = gateway.search("weather", mode=mode)
        assert _ids(found) == ["secret"], mode


def test_nan_judge_score_is_rejected() -> None:
    judge = LocalJudge(ENDPOINT, (ENDPOINT,), lambda *_args: float("nan"))
    with pytest.raises(ValueError, match="0, 1"):
        judge.score("q", "p", "")


def test_wheel_search_runs_outside_the_checkout(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0
    wheel = next(dist.glob("*.whl"))
    site = tmp_path / "site"
    site.mkdir()
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        archive.extractall(site)
    assert any(name.endswith("memfw/memfw_readonly_service.py") for name in names)
    assert any(name.endswith("memfw/read_path.py") for name in names)
    vault = tmp_path / "vault"
    script = tmp_path / "smoke.py"
    script.write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "import memfw\n"
        "from memfw.core.repository import Repository\n"
        "from memfw.mcp.server import Gateway\n"
        "root = Path(os.environ['VAULT'])\n"
        "repo = Repository(root)\n"
        "repo.init()\n"
        "repo.add('Weather bulletin', 'weather forecast report', "
        "memory_id='weather-record', tenant='alpha')\n"
        "gate = Gateway(root, tenant='alpha', entity_enabled=False)\n"
        "out = {'module': memfw.__file__}\n"
        "for mode in ('fts', 'hybrid', 'multihop'):\n"
        "    found = gate.search('weather forecast', mode=mode)\n"
        "    out[mode] = [item['id'] for item in found['results']]\n"
        "print(json.dumps(out))\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(site)
    env["VAULT"] = str(vault)
    env.pop("MEMFW_JUDGE_URL", None)
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert payload["module"].startswith(str(site))
    for mode in ("fts", "hybrid", "multihop"):
        assert payload[mode] == ["weather-record"], mode
