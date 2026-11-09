"""Regressions for the retrieval blockers that stayed open through R4."""

from __future__ import annotations

import hashlib
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


def test_preserved_mtime_update_marks_multihop_stale(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add(
        "alpha original",
        "alpha keyword marker",
        memory_id="record",
        tenant="alpha",
    )
    MemoryIndex(repository.index_path).rebuild(repository)
    gateway = Gateway(repository.root, tenant="alpha", entity_enabled=False)
    path = repository.memories_dir / "record.md"
    before_stat = path.stat()
    repository.update("record", title="bravo current", body="bravo replaced marker")
    os.utime(path, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
    assert path.stat().st_mtime_ns == before_stat.st_mtime_ns
    live = repository.get("record")
    found = gateway.search("alpha keyword", mode="multihop")
    hit = found["results"][0]
    file_hash = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert hit["content_hash"] == file_hash
    assert hit["index_version"] == "1"
    assert hit["sot_version"] == str(live.version)
    assert hit["sot_version"] == "2"
    assert hit["stale"] is True
    fts = gateway.search("alpha keyword", mode="fts")["results"][0]
    assert fts["index_version"] == "1"
    assert fts["sot_version"] == "2"
    assert fts["stale"] is True


def test_raw_hash_cache_misses_when_ctime_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memfw.mcp import server

    path = tmp_path / "record.md"
    path.write_bytes(b"---\nversion: 1\n---\n\nbody\n")
    cache = server._RawHashCache(1)
    first_hash, first_version = cache.lookup(path)
    assert first_version == "1"
    assert first_hash == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert server._frontmatter_version(b"---\nid: a\n---\n\nversion: 9\n") == ""
    reads = {"n": 0}
    real_read = Path.read_bytes

    def counting(self: Path) -> bytes:
        reads["n"] += 1
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    assert cache.lookup(path) == (first_hash, first_version)
    assert reads["n"] == 0
    before = path.stat()
    path.write_bytes(b"---\nversion: 2\n---\n\nbody changed\n")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    second_hash, second_version = cache.lookup(path)
    assert second_version == "2"
    assert second_hash != first_hash
    other = tmp_path / "other.md"
    other.write_bytes(b"---\nversion: 3\n---\n\nother\n")
    assert cache.lookup(other)[1] == "3"
    assert cache.lookup(path)[1] == "2"
    assert reads["n"] == 3


def test_multihop_freshness_does_not_parse_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add(
        "alpha original",
        "alpha keyword marker",
        memory_id="record",
        tenant="alpha",
    )
    MemoryIndex(repository.index_path).rebuild(repository)

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("yaml.safe_load")

    monkeypatch.setattr("memfw.mcp.server.yaml.safe_load", boom)
    found = Gateway(repository.root, tenant="alpha", entity_enabled=False).search(
        "alpha keyword",
        mode="multihop",
    )
    hit = found["results"][0]
    assert hit["id"] == "record"
    assert hit["sot_version"] == "1"
    assert hit["stale"] is False


def test_freshness_checks_only_the_first_ten_returned_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memfw.mcp import server

    repository = Repository(tmp_path / "vault")
    repository.init()
    for index in range(12):
        repository.add(
            f"Bulletin {index}",
            f"shared marker bulletin {index}",
            memory_id=f"m{index:02d}",
            tenant="alpha",
        )
    MemoryIndex(repository.index_path).rebuild(repository)
    seen: list[str] = []
    real_lookup = server._RAW_HASH_CACHE.lookup

    def counting(path: Path) -> tuple[str, str]:
        seen.append(path.name)
        return real_lookup(path)

    monkeypatch.setattr(server._RAW_HASH_CACHE, "lookup", counting)
    found = Gateway(repository.root, tenant="alpha", entity_enabled=False).search(
        "shared marker",
        mode="multihop",
        limit=12,
    )
    assert len(found["results"]) > 10
    assert [name.removesuffix(".md") for name in seen] == [
        item["id"] for item in found["results"][:10]
    ]
    tail = found["results"][10]
    assert tail["content_hash"] == ""
    assert tail["stale"] is False
    assert tail["sot_version"] == tail["index_version"]


def test_returned_hit_compares_live_bytes_to_the_indexed_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add(
        "alpha original",
        "alpha keyword marker",
        memory_id="record",
        tenant="alpha",
    )
    MemoryIndex(repository.index_path).rebuild(repository)
    path = repository.root / "memories" / "record.md"
    live = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def indexed(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "mode": "multihop",
            "degraded": False,
            "errors": [],
            "model_versions": {},
            "route": "direct",
            "results": [
                {
                    "id": "record",
                    "title": "alpha original",
                    "score": 1.0,
                    "source": "manual",
                    "why": "lexical",
                    "index_version": "1",
                    "layer": "project",
                    "tier": "warm",
                    "updated_at": "2026-11-08T00:00:00+00:00",
                    "valid_from": None,
                    "valid_to": None,
                    "superseded_by": None,
                    "provenance": "user_said",
                    "memory_type": "semantic",
                    "content_hash": "sha256:" + ("ab" * 32),
                }
            ],
        }

    monkeypatch.setattr("memfw.mcp.server.search_multihop", indexed)
    hit = Gateway(repository.root, tenant="alpha", entity_enabled=False).search(
        "alpha keyword",
        mode="multihop",
    )["results"][0]
    assert hit["content_hash"] == live
    assert hit["index_version"] == "1"
    assert hit["sot_version"] == "1"
    assert hit["stale"] is True


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
