"""A2b: the vault opener and invocation overrides in core, candidate files in storage."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer

import memfw.capture.review as capture_review
import memfw.capture.staging as capture_staging
import memfw.storage.candidate_files as candidate_files
import memfw.storage.candidate_staging as candidate_staging
from memfw import cli
from memfw.core import invocation
from memfw.core.repository import Repository, VaultOpenError, open_vault
from memfw.ports.clock import SeededIdGen

NOW = datetime(2026, 10, 18, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_invocation() -> Iterator[None]:
    yield
    invocation.set_invocation(now=None, id_seed=None)


def _vault(path: Path) -> Repository:
    repository = Repository(path)
    repository.init()
    return repository


def test_invocation_defaults_and_set() -> None:
    assert invocation.injected_now() is None
    assert invocation.injected_id_seed() is None
    invocation.set_invocation(now=NOW, id_seed="seed")
    assert invocation.injected_now() == NOW
    assert invocation.injected_id_seed() == "seed"


def test_unseeded_id_factory_is_random(tmp_path: Path) -> None:
    factory = invocation.id_factory(tmp_path)
    assert factory() != factory()


def test_seeded_id_factory_matches_seeded_id_gen_and_resumes(tmp_path: Path) -> None:
    invocation.set_invocation(now=None, id_seed="s")
    fresh = invocation.id_factory(tmp_path)
    expected = SeededIdGen("s")
    assert [fresh(), fresh()] == [expected.new_id(), expected.new_id()]

    (tmp_path / "events.jsonl").write_bytes(b"a\n\nb\nc\n")
    resumed = invocation.id_factory(tmp_path)
    assert resumed() == hashlib.sha256(b"s:7").hexdigest()[:32]
    assert SeededIdGen("s", start=6).new_id() == hashlib.sha256(b"s:7").hexdigest()[:32]


def test_open_vault_reads_no_invocation_state(tmp_path: Path) -> None:
    root = _vault(tmp_path / "v").root
    invocation.set_invocation(now=NOW, id_seed="s")
    repository = open_vault(root)
    assert repository._now() != NOW
    assert repository.id_factory() != SeededIdGen("s").new_id()


def test_open_vault_uses_the_given_clock_and_ids(tmp_path: Path) -> None:
    root = _vault(tmp_path / "v").root
    ids = SeededIdGen("x")
    repository = open_vault(root, clock=lambda: NOW, id_factory=ids.new_id)
    assert repository._now() == NOW
    assert repository.id_factory() == SeededIdGen("x").new_id()


def test_invocation_clock_reads_now_per_call(tmp_path: Path) -> None:
    repository = open_vault(_vault(tmp_path / "v").root, clock=invocation.clock())
    assert repository._now() != NOW
    invocation.set_invocation(now=NOW, id_seed=None)
    assert repository._now() == NOW


def test_open_vault_refuses_non_vault_and_pending(tmp_path: Path) -> None:
    with pytest.raises(VaultOpenError, match="Not a memfw vault"):
        open_vault(tmp_path / "missing")
    repository = _vault(tmp_path / "pending")
    repository.pending_path.write_text("{}", encoding="utf-8")
    with pytest.raises(VaultOpenError, match="pending transaction"):
        open_vault(repository.root)
    assert open_vault(repository.root, allow_pending=True).root == repository.root


def test_open_vault_raises_the_callers_error_type(tmp_path: Path) -> None:
    with pytest.raises(typer.BadParameter, match="Not a memfw vault"):
        open_vault(tmp_path / "missing", error=typer.BadParameter)


def test_cli_aliases_pass_the_invocation_overrides(tmp_path: Path) -> None:
    with pytest.raises(typer.BadParameter, match="Not a memfw vault"):
        cli._repo(tmp_path / "missing")
    invocation.set_invocation(now=NOW, id_seed="s")
    assert cli._id_factory(tmp_path)() == SeededIdGen("s").new_id()
    repository = cli._repo(_vault(tmp_path / "v").root)
    assert repository._now() == NOW
    assert repository.id_factory() == SeededIdGen("s").new_id()


def test_capture_modules_re_export_the_storage_objects() -> None:
    for name in capture_staging.__all__:
        assert getattr(capture_staging, name) is getattr(candidate_staging, name)
    for name in candidate_files.__all__:
        assert getattr(capture_review, name) is getattr(candidate_files, name)
    assert set(capture_review.__all__) == set(candidate_files.__all__) | {"list_staged_candidates"}
