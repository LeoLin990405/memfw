"""Recall resolvers: meta-index lifecycle for every provider, ``kh:`` notes, the ``att:`` hook."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.notes import NotesIndex
from memfw.ports.types import Availability, Candidate, RecallBudget
from memfw.projection import stable_id
from memfw.recall import (
    Coordinator,
    FilterPolicy,
    MemoryMeta,
    MemoryPathResolver,
    Resolution,
    SchemePathResolver,
    SegmentResolution,
    parse_kh_ref,
)
from memfw.recall.builtin import (
    DEFAULT_LIFECYCLE_SOURCE,
    FtsProvider,
    NotesSegmentResolver,
    VaultIdResolver,
    default_resolvers,
)
from memfw.runtime import ensure_search_index
from memfw.runtime.search import note_rows

NOW = datetime.now(UTC)
PAST = (NOW - timedelta(days=3)).isoformat()
FUTURE = (NOW + timedelta(days=30)).isoformat()

STRICT = FilterPolicy()
RETIRED_OK = FilterPolicy(include_retired=True)
COMPAT = FilterPolicy.compat_fts(tenant="")


class FakeProvider:
    """Returns fixed refs; the stand-in for an external provider (QMD, PageIndex)."""

    def __init__(self, name: str, refs: Sequence[str], *, ref_kind: str = "memory_id") -> None:
        self.name = name
        self.refs = list(refs)
        self.ref_kind = ref_kind
        self.lexical = False
        self.projection_version = ""

    def available(self) -> Availability:
        return Availability.ok()

    def search(
        self,
        query: str,
        *,
        limit: int,
        budget: RecallBudget,
        tenant: str,
        as_of: datetime | None,
    ) -> Sequence[Candidate]:
        return [
            Candidate(
                ref=ref,
                ref_kind=self.ref_kind,  # type: ignore[arg-type]
                score=1.0 / (index + 1),
                provider=self.name,
                rank=index + 1,
            )
            for index, ref in enumerate(self.refs[:limit])
        ]


# --- part 1: lifecycle from the meta-index ---------------------------------------------

#: (memory id, meta-index item fields or None for "no item", strict reason,
#: kept with include_retired, label with include_retired).
MATRIX: tuple[tuple[str, dict[str, Any] | None, str | None, bool, str | None], ...] = (
    ("m-plain", {}, None, True, None),
    ("m-active", {"lifecycle": "active", "valid_until": None, "privacy": None}, None, True, None),
    ("m-retired", {"lifecycle": "retired"}, "retired", True, "retired"),
    ("m-superseded", {"lifecycle": "superseded"}, "superseded", True, "superseded"),
    ("m-supby", {"superseded_by": "new.md"}, "superseded", True, "superseded"),
    ("m-override", {"lifecycle": "active", "lifecycle_override": "retired"}, "retired", True, None),
    ("m-archived", {"lifecycle": "archived"}, "lifecycle", False, None),
    ("m-oddlife", {"lifecycle": 7}, "lifecycle", False, None),
    ("m-until-past", {"valid_until": PAST}, "expired", True, "expired"),
    ("m-until-future", {"valid_until": FUTURE}, None, True, None),
    ("m-until-bad", {"valid_until": "next tuesday"}, "expiry_unparsable", False, None),
    ("m-until-odd", {"valid_until": 20250101}, "expiry_unparsable", False, None),
    ("m-validto-past", {"valid_to": PAST}, "expired", True, "expired"),
    ("m-private", {"privacy": "local-private"}, "privacy", False, None),
    ("m-unjoined", None, "unjoined", False, None),
)


def _lifecycle_vault(tmp_path: Path, *, meta_index: bool = True) -> Repository:
    """One ``backup`` memory per matrix row, each with a ``fleet_path`` tag."""

    repository = Repository(tmp_path / "vault")
    repository.init()
    items: list[dict[str, Any]] = []
    for memory_id, fields, _reason, _kept, _label in MATRIX:
        rel = f"{memory_id}.md"
        repository.add(
            f"Backup {memory_id}",
            f"backup notes for {memory_id}",
            memory_id=memory_id,
            tags=[f"fleet_path:{rel}"],
        )
        if fields is not None:
            items.append({"path": rel, **fields})
    repository.add("Native backup", "backup notes native", memory_id="m-native")
    if meta_index:
        _write_meta_index(repository.root, items)
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


def _write_meta_index(root: Path, items: list[dict[str, Any]]) -> Path:
    path = root / DEFAULT_LIFECYCLE_SOURCE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": "2026-11-17T00:00:00Z", "root": "files", "items": items}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _expected(policy: FilterPolicy) -> set[str]:
    kept = {"m-native"}
    for memory_id, _fields, reason, retired_ok, _label in MATRIX:
        if reason is None or (policy.include_retired and retired_ok):
            kept.add(memory_id)
    return kept


def _all_ids() -> list[str]:
    return [row[0] for row in MATRIX] + ["m-native"]


def _fts(root: Path) -> FtsProvider:
    return FtsProvider(root, prepare=ensure_search_index)


@pytest.mark.parametrize("provider_kind", ["fts", "fake"])
@pytest.mark.parametrize("policy", [STRICT, RETIRED_OK], ids=["strict", "include_retired"])
def test_lifecycle_drop_matrix_through_the_coordinator(
    tmp_path: Path, provider_kind: str, policy: FilterPolicy
) -> None:
    repository = _lifecycle_vault(tmp_path)
    root = repository.root
    provider = _fts(root) if provider_kind == "fts" else FakeProvider("fake", _all_ids())
    coordinator = Coordinator([provider], resolvers=default_resolvers(root))
    result = coordinator.search("backup notes", limit=50, policy=policy)
    assert set(result.ids) == _expected(policy)
    dropped = result.metrics.dropped
    for memory_id, _fields, reason, retired_ok, label in MATRIX:
        if reason is None or (policy.include_retired and retired_ok):
            if policy.include_retired and label:
                hit = next(hit for hit in result.hits if hit.memory_id == memory_id)
                assert label in hit.labels, memory_id
            continue
        assert dropped.get(reason, 0) >= 1, (memory_id, reason, dropped)


@pytest.mark.parametrize("provider_kind", ["fts", "fake"])
def test_meta_index_fields_never_change_compat_fts(tmp_path: Path, provider_kind: str) -> None:
    """``compat_fts`` (mode=fts equivalence) reads no meta-index field."""

    with_index = _lifecycle_vault(tmp_path / "a")
    without = _lifecycle_vault(tmp_path / "b", meta_index=False)

    def ids(root: Path) -> list[str]:
        provider = _fts(root) if provider_kind == "fts" else FakeProvider("fake", _all_ids())
        coordinator = Coordinator([provider], resolvers=default_resolvers(root))
        return coordinator.search("backup notes", limit=50, policy=COMPAT).ids

    assert ids(with_index.root) == ids(without.root)
    assert set(ids(with_index.root)) == set(_all_ids())


def test_no_meta_index_keeps_the_vault_only_reading(tmp_path: Path) -> None:
    repository = _lifecycle_vault(tmp_path, meta_index=False)
    coordinator = Coordinator(
        [FakeProvider("fake", _all_ids())], resolvers=default_resolvers(repository.root)
    )
    assert set(coordinator.search("backup", limit=50, policy=STRICT).ids) == set(_all_ids())


def test_meta_index_without_lifecycle_fields_is_identical_to_none(tmp_path: Path) -> None:
    """Every document joined, no lifecycle field: the same metadata as no meta-index."""

    repository = Repository(tmp_path / "vault")
    repository.init()
    for memory_id in ("m-a", "m-b"):
        repository.add("t", "backup", memory_id=memory_id, tags=[f"fleet_path:{memory_id}.md"])
    ids = ["m-a", "m-b"]
    before = VaultIdResolver(repository.root).resolve(ids)
    _write_meta_index(
        repository.root, [{"path": "m-a.md", "scope": "fleet"}, {"path": "m-b.md", "status": "x"}]
    )
    after = VaultIdResolver(repository.root).resolve(ids)
    for memory_id in ids:
        old, new = before[memory_id].meta, after[memory_id].meta
        assert old is not None and new is not None
        assert new.lifecycle_join == "ok"
        assert replace(new, lifecycle_join="") == old


@pytest.mark.parametrize("state", ["symlink", "malformed", "not_json"])
def test_unreadable_meta_index_fails_closed_for_tagged_documents(
    tmp_path: Path, state: str
) -> None:
    repository = _lifecycle_vault(tmp_path, meta_index=False)
    root = repository.root
    target = root / DEFAULT_LIFECYCLE_SOURCE
    target.parent.mkdir(parents=True, exist_ok=True)
    if state == "symlink":
        real = tmp_path / "elsewhere.json"
        real.write_text(json.dumps({"items": []}), encoding="utf-8")
        target.symlink_to(real)
    elif state == "malformed":
        target.write_text(json.dumps({"items": [{"no_path": 1}]}), encoding="utf-8")
    else:
        target.write_text("{not json", encoding="utf-8")
    coordinator = Coordinator([FakeProvider("fake", _all_ids())], resolvers=default_resolvers(root))
    strict = coordinator.search("backup", limit=50, policy=RETIRED_OK)
    assert strict.ids == ["m-native"]  # untagged memories are unaffected
    assert strict.metrics.dropped == {"unjoined": len(MATRIX)}
    compat = coordinator.search("backup", limit=50, policy=COMPAT)
    assert set(compat.ids) == set(_all_ids())


def test_named_lifecycle_source_and_missing_named_file(tmp_path: Path) -> None:
    repository = _lifecycle_vault(tmp_path, meta_index=False)
    root = repository.root
    outside = tmp_path / "status" / "memory-meta-index.json"
    outside.parent.mkdir()
    outside_items = [{"path": f"{row[0]}.md", "lifecycle": "retired"} for row in MATRIX]
    outside.write_text(json.dumps({"items": outside_items}), encoding="utf-8")
    named = Coordinator(
        [FakeProvider("fake", _all_ids())],
        resolvers=default_resolvers(root, lifecycle_source=outside),
    )
    assert named.search("backup", limit=50, policy=STRICT).ids == ["m-native"]
    missing = Coordinator(
        [FakeProvider("fake", _all_ids())],
        resolvers=default_resolvers(root, lifecycle_source=tmp_path / "absent.json"),
    )
    result = missing.search("backup", limit=50, policy=STRICT)
    assert result.ids == ["m-native"]
    assert result.metrics.dropped == {"unjoined": len(MATRIX)}


def test_duplicate_path_and_id_mismatch_are_unjoined(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    good = stable_id("good.md")
    repository.add("t", "backup", memory_id=good, tags=["fleet_path:good.md"])
    repository.add("t", "backup", memory_id=stable_id("x.md"), tags=["fleet_path:other.md"])
    repository.add("t", "backup", memory_id="m-dup", tags=["fleet_path:dup.md"])
    repository.add("t", "backup", memory_id="m-two", tags=["fleet_path:a.md", "fleet_path:b.md"])
    _write_meta_index(
        repository.root,
        [
            {"path": "good.md", "lifecycle": "active"},
            {"path": "other.md"},
            {"path": "dup.md"},
            {"path": "dup.md", "lifecycle": "active"},
            {"path": "a.md"},
            {"path": "b.md"},
        ],
    )
    ids = [good, stable_id("x.md"), "m-dup", "m-two"]
    coordinator = Coordinator(
        [FakeProvider("fake", ids)], resolvers=default_resolvers(repository.root)
    )
    result = coordinator.search("backup", limit=10, policy=STRICT)
    assert result.ids == [good]
    assert result.metrics.dropped == {"unjoined": 3}


def test_meta_index_is_not_read_without_tagged_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("t", "backup", memory_id="m-native")
    _write_meta_index(repository.root, [])
    from memfw.recall import builtin

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("meta-index read")

    monkeypatch.setattr(builtin, "load_lifecycle", boom)
    found = VaultIdResolver(repository.root).resolve(["m-native"])
    assert found["m-native"].meta is not None


def test_meta_index_privacy_only_restricts(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("t", "b", memory_id="m-tag", tags=["fleet_path:t.md", "sensitivity:private"])
    repository.add("t", "b", memory_id="m-odd", tags=["fleet_path:o.md"])
    _write_meta_index(
        repository.root,
        [{"path": "t.md", "privacy": "public"}, {"path": "o.md", "privacy": ["x"]}],
    )
    metas = VaultIdResolver(repository.root).resolve(["m-tag", "m-odd"])
    cleared = FilterPolicy(privacy_clearance=frozenset({"none", "local-private"}))
    from memfw.recall import evaluate

    tagged = metas["m-tag"].meta
    odd = metas["m-odd"].meta
    assert tagged is not None and odd is not None
    assert evaluate(tagged, cleared, now=NOW).kept  # public never loosens the tag
    assert evaluate(tagged, STRICT, now=NOW).reason == "privacy"
    assert evaluate(odd, cleared, now=NOW).reason == "privacy"  # non-string → unknown


# --- part 2: kh: notes --------------------------------------------------------------------

NOTES: dict[str, str] = {
    "public.md": "---\nprivacy: public\n---\n# Public guide\n\n## Setup\n\n"
    "Install the backup agent.\n\n## Restore\n\nRestore drills monthly.\n",
    "shared.md": "---\nprivacy: shared\n---\n# Shared runbook\n\nshared backup steps\n",
    "private.md": "---\nprivacy: local-private\n---\n# Private\n\n## Keys\n\nbackup keys here\n",
    "legacy.md": "# Legacy\n\nlegacy backup note without a privacy field\n",
    "unknown.md": "---\nprivacy: secret\n---\n# Unknown\n\nunknown backup level\n",
    "tagged.md": "---\nprivacy: public\ntags: [sensitivity:private]\n---\n# Tagged\n\n"
    "tagged backup\n",
    "Course A/Week 1 notes.md": "---\nprivacy: public\n---\n# Week 1\n\n## Intro part\n\n"
    "intro backup lecture\n",
    "迁移/计划.md": "---\nprivacy: public\n---\n# 迁移\n\n## 步骤 一\n\n先备份 backup\n",
}


def _notes(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "kh"
    for rel, text in NOTES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (tmp_path / "outside.md").write_text("# Outside\n\nsecret backup\n", encoding="utf-8")
    db = tmp_path / "notes.sqlite3"
    NotesIndex(db).index(root)
    return root, db


def _resolve(resolver: NotesSegmentResolver, *refs: str) -> dict[str, Any]:
    return dict(resolver.resolve(list(refs)))


def test_kh_ref_grammar() -> None:
    assert parse_kh_ref("kh:a/b.md#Setup") == ("a/b.md", "Setup")
    assert parse_kh_ref("kh:Course%20A/Week%201%20notes.md#Intro%20part") == (
        "Course A/Week 1 notes.md",
        "Intro part",
    )
    assert parse_kh_ref("kh:a.md") == ("a.md", "")
    assert parse_kh_ref("kh:a%23b.md#x%23y") == ("a#b.md", "x#y")
    for bad in (
        "kh:",
        "kh:../x.md",
        "kh:a/../../x.md",
        "kh:%2E%2E/x.md",
        "kh:/etc/x.md",
        "kh:%2Fetc/x.md",
        "kh:a//b.md",
        "kh:./a.md",
        "kh:a%5Cb.md",
        "kh:a%00.md",
        "kh:a.md#x%0Ay",
        "kh:%FF.md",
        "memories/m.md",
        "att:prx#1",
    ):
        assert parse_kh_ref(bad) is None, bad


def test_kh_privacy_levels_with_the_default_clearance(tmp_path: Path) -> None:
    root, db = _notes(tmp_path)
    resolver = NotesSegmentResolver(root, db)
    found = _resolve(
        resolver,
        "kh:public.md#Setup",
        "kh:shared.md",
        "kh:private.md#Keys",
        "kh:legacy.md",
        "kh:unknown.md",
        "kh:tagged.md",
    )
    assert set(found) == {"kh:public.md#Setup", "kh:shared.md"}
    assert found["kh:public.md#Setup"].privacy == "public"
    assert found["kh:shared.md"].privacy == "shared"


def test_kh_local_private_needs_clearance_and_unknown_never_resolves(tmp_path: Path) -> None:
    root, db = _notes(tmp_path)
    resolver = NotesSegmentResolver(
        root, db, clearance=frozenset({"public", "shared", "local-private"})
    )
    found = _resolve(
        resolver, "kh:private.md#Keys", "kh:legacy.md", "kh:unknown.md", "kh:tagged.md"
    )
    assert set(found) == {"kh:private.md#Keys", "kh:legacy.md", "kh:tagged.md"}
    assert {item.privacy for item in found.values()} == {"local-private"}
    legacy_public = NotesSegmentResolver(root, db, legacy_privacy_default="public")
    assert set(_resolve(legacy_public, "kh:legacy.md")) == {"kh:legacy.md"}


def test_kh_returns_exactly_what_notes_search_returns(tmp_path: Path) -> None:
    root, db = _notes(tmp_path)
    hits = NotesIndex(db).search("restore drills", limit=1, mode="fts")
    assert hits and hits[0].path == "public.md"
    hit = hits[0]
    ref = f"kh:public.md#{hit.heading}"
    found = _resolve(NotesSegmentResolver(root, db), ref)[ref]
    assert (found.title, found.anchor, found.snippet) == (hit.title, hit.heading, hit.content)
    assert found.source == "notes"
    assert found.path == "public.md"
    assert "Install" not in found.snippet  # one section, not the note


def test_kh_heading_rules(tmp_path: Path) -> None:
    root, db = _notes(tmp_path)
    resolver = NotesSegmentResolver(root, db)
    found = _resolve(
        resolver,
        "kh:public.md",
        "kh:public.md#Nope",
        "kh:public.md#setup",
        "kh:Course%20A/Week%201%20notes.md#Intro%20part",
        "kh:迁移/计划.md#步骤%20一",
    )
    assert set(found) == {
        "kh:public.md",
        "kh:Course%20A/Week%201%20notes.md#Intro%20part",
        "kh:迁移/计划.md#步骤%20一",
    }
    assert found["kh:public.md"].anchor == "Setup"  # the first section
    assert "备份" in found["kh:迁移/计划.md#步骤%20一"].snippet


def test_kh_path_traversal_symlinks_and_missing_notes(tmp_path: Path) -> None:
    root, db = _notes(tmp_path)
    resolver = NotesSegmentResolver(root, db)
    # A note indexed, then swapped for a symlink; a symlinked directory.
    (root / "shared.md").unlink()
    (root / "shared.md").symlink_to(root / "public.md")
    (root / "linked").symlink_to(root / "Course A", target_is_directory=True)
    (root / "迁移/计划.md").unlink()  # indexed, file gone
    (tmp_path / "kh2").mkdir()
    refs = [
        "kh:../outside.md",
        "kh:%2E%2E/outside.md",
        "kh:/etc/hosts",
        "kh:shared.md",
        "kh:linked/Week%201%20notes.md",
        "kh:迁移/计划.md",
        "kh:never-indexed.md",
    ]
    (root / "never-indexed.md").write_text("---\nprivacy: public\n---\n# N\n\nx\n", "utf-8")
    assert _resolve(resolver, *refs) == {}


def test_kh_stale_note_and_unusable_index_are_withheld(tmp_path: Path) -> None:
    root, db = _notes(tmp_path)
    resolver = NotesSegmentResolver(root, db)
    path = root / "public.md"
    path.write_text(path.read_text("utf-8") + "\nedited\n", "utf-8")
    assert _resolve(resolver, "kh:public.md#Setup") == {}
    assert _resolve(NotesSegmentResolver(root, tmp_path / "missing.sqlite3"), "kh:shared.md") == {}
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(db)
    assert _resolve(NotesSegmentResolver(root, linked), "kh:shared.md") == {}
    with pytest.raises(ValueError):
        NotesSegmentResolver(root, db, clearance=frozenset({"unknown"}))


def _notes_coordinator(
    tmp_path: Path,
    refs: Sequence[str],
    *,
    memory_ids: Sequence[str] = ("m-backup",),
    clearance: frozenset[str] = frozenset({"public", "shared", "local-private"}),
    tenant: str = "",
) -> Coordinator:
    root, db = _notes(tmp_path)
    vault = Repository(tmp_path / "vault")
    vault.init()
    vault.add("Backup", "backup", memory_id="m-backup")
    notes = NotesSegmentResolver(root, db, clearance=clearance, tenant=tenant)
    return Coordinator(
        [FakeProvider("mem", memory_ids), FakeProvider("qmd-notes", refs, ref_kind="path")],
        resolvers=default_resolvers(vault.root, schemes={"kh": notes}),
    )


def test_coordinator_keeps_notes_apart_from_memory_hits(tmp_path: Path) -> None:
    refs = ["kh:public.md#Setup", "kh:private.md#Keys", "kh:unknown.md", "kh:public.md#Nope"]
    coordinator = _notes_coordinator(tmp_path, refs)
    result = coordinator.search("backup", limit=10, policy=STRICT)
    assert result.ids == ["m-backup"]
    assert result.hits[0].providers == ("mem",)
    # Strict clearance {"none"}: the local-private section is dropped here.
    assert [(seg.source, seg.path, seg.anchor) for seg in result.segments] == [
        ("notes", "public.md", "Setup")
    ]
    assert result.segments[0].providers == ("qmd-notes",)
    assert result.metrics.dropped == {"privacy": 1, "unresolved": 2}
    assert result.metrics.returned == 1 and result.metrics.segments == 1
    assert result.metrics.as_dict()["segments"] == 1
    cleared = FilterPolicy(privacy_clearance=frozenset({"none", "local-private"}))
    wide = coordinator.search("backup", limit=10, policy=cleared)
    assert [seg.path for seg in wide.segments] == ["public.md", "private.md"]
    assert wide.segments[1].labels == ("privacy:local-private",)


def test_notes_do_not_change_memory_output(tmp_path: Path) -> None:
    with_notes = _notes_coordinator(tmp_path / "a", ["kh:public.md#Setup"])
    vault = Repository(tmp_path / "b" / "vault")
    vault.init()
    vault.add("Backup", "backup", memory_id="m-backup")
    alone = Coordinator(
        [FakeProvider("mem", ["m-backup"])], resolvers=default_resolvers(vault.root)
    )
    a = with_notes.search("backup", limit=10, policy=STRICT)
    b = alone.search("backup", limit=10, policy=STRICT)
    assert [(h.memory_id, h.score, h.providers, h.labels) for h in a.hits] == [
        (h.memory_id, h.score, h.providers, h.labels) for h in b.hits
    ]
    assert "segments" not in b.metrics.as_dict()
    assert b.segments == ()


def test_notes_tenant_and_source_restrictions(tmp_path: Path) -> None:
    coordinator = _notes_coordinator(tmp_path, ["kh:public.md#Setup"], tenant="alpha")
    assert coordinator.search("backup", limit=5, policy=STRICT).segments == ()
    alpha = FilterPolicy(tenant="alpha")
    assert len(coordinator.search("backup", limit=5, policy=alpha).segments) == 1
    restricted = FilterPolicy(tenant="alpha", allowed_sources=("manual",))
    result = coordinator.search("backup", limit=5, policy=restricted)
    assert result.segments == () and result.metrics.dropped.get("source") == 1


def test_two_refs_to_one_section_fuse(tmp_path: Path) -> None:
    root, db = _notes(tmp_path)
    vault = Repository(tmp_path / "vault")
    vault.init()
    notes = NotesSegmentResolver(root, db)
    # Two encodings of one section: ``%20`` and ``%2F``-free UTF-8 escapes.
    qmd = "kh:Course%20A/Week%201%20notes.md#Intro%20part"
    pageindex = "kh:Course%20A/Week%201%20notes%2Emd#Intro%20part"
    coordinator = Coordinator(
        [
            FakeProvider("qmd-notes", [qmd], ref_kind="path"),
            FakeProvider("pageindex", [pageindex], ref_kind="path"),
        ],
        resolvers=default_resolvers(vault.root, schemes={"kh": notes}),
    )
    result = coordinator.search("intro", limit=5, policy=STRICT)
    assert len(result.segments) == 1
    assert result.segments[0].providers == ("qmd-notes", "pageindex")


def test_note_rows_label_notes_and_apply_the_runtime_clearance(tmp_path: Path) -> None:
    coordinator = _notes_coordinator(tmp_path, ["kh:public.md#Setup", "kh:private.md#Keys"])
    cleared = FilterPolicy(privacy_clearance=frozenset({"none", "local-private"}))
    segments = coordinator.search("backup", limit=5, policy=cleared).segments
    rows = note_rows(segments)
    assert [row["source"] for row in rows] == ["notes", "notes"]
    assert set(rows[0]) == {
        "source",
        "ref",
        "path",
        "title",
        "heading",
        "content",
        "score",
        "providers",
        "recall_labels",
    }
    assert rows[0]["content"] == "Install the backup agent."
    public_only = note_rows(segments, clearance=frozenset({"public"}))
    assert [row["path"] for row in public_only] == ["public.md"]
    assert note_rows(()) == []


# --- the att: hook --------------------------------------------------------------------


class StubAttachmentResolver:
    """What an MM2b ``att:`` resolver plugs in as: cleared segments, never memories."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def resolve(self, refs: Sequence[str]) -> Mapping[str, Any]:
        self.calls.append(list(refs))
        out: dict[str, Any] = {}
        for ref in refs:
            if ref == "att:prx-1#page=2":
                out[ref] = SegmentResolution(
                    source="attachments",
                    path="prx-1",
                    anchor="page=2",
                    title="Scan",
                    snippet="page two text",
                    privacy="public",
                )
            if ref == "att:prx-bad":
                out[ref] = SegmentResolution("bogus", "p", "", "", "", "public")
        out["memories/m-backup.md"] = Resolution("m-backup", None)  # never trusted
        return out


def test_att_scheme_hook_with_a_stub(tmp_path: Path) -> None:
    vault = Repository(tmp_path / "vault")
    vault.init()
    vault.add("Backup", "backup", memory_id="m-backup")
    refs = ["att:prx-1#page=2", "att:prx-bad", "memories/m-backup.md"]
    provider = FakeProvider("pageindex", refs, ref_kind="path")
    plain = Coordinator([provider], resolvers=default_resolvers(vault.root))
    before = plain.search("scan", limit=5, policy=STRICT)
    assert before.ids == ["m-backup"] and before.segments == ()
    assert before.metrics.dropped == {"unresolved": 2}
    stub = StubAttachmentResolver()
    hooked = Coordinator([provider], resolvers=default_resolvers(vault.root, schemes={"att": stub}))
    result = hooked.search("scan", limit=5, policy=STRICT)
    assert stub.calls == [["att:prx-1#page=2", "att:prx-bad"]]
    assert result.ids == ["m-backup"]  # resolved by the memory path resolver, not the stub
    assert [(seg.source, seg.path, seg.anchor) for seg in result.segments] == [
        ("attachments", "prx-1", "page=2")
    ]
    assert result.metrics.dropped == {"unresolved": 1}  # the malformed segment
    assert note_rows(result.segments) == []  # attachments are not notes


def test_scheme_registration_rules(tmp_path: Path) -> None:
    ids = VaultIdResolver(tmp_path)
    resolver = SchemePathResolver(MemoryPathResolver(ids))
    resolver.register("att", StubAttachmentResolver())
    with pytest.raises(ValueError):
        resolver.register("att", StubAttachmentResolver())
    for bad in ("", "KH", "k h", "x" * 17, "a:b"):
        with pytest.raises(ValueError):
            resolver.register(bad, StubAttachmentResolver())
    # Without a registration, scheme refs go to the fallback exactly as before.
    seen: list[list[str]] = []

    class Fallback:
        def resolve(self, refs: Sequence[str]) -> Mapping[str, Resolution]:
            seen.append(list(refs))
            return {ref: Resolution("m1", MemoryMeta("m1", "")) for ref in refs}

    routed = SchemePathResolver(Fallback())
    assert set(routed.resolve(["kh:a.md", "qmd://c/x.md", "files/a.md"])) == {
        "kh:a.md",
        "qmd://c/x.md",
        "files/a.md",
    }
    assert seen == [["kh:a.md", "qmd://c/x.md", "files/a.md"]]


def test_default_resolvers_leave_kh_unresolved_without_a_notes_resolver(tmp_path: Path) -> None:
    _notes(tmp_path)
    vault = Repository(tmp_path / "vault")
    vault.init()
    coordinator = Coordinator(
        [FakeProvider("qmd-notes", ["kh:public.md#Setup"], ref_kind="path")],
        resolvers=default_resolvers(vault.root),
    )
    result = coordinator.search("setup", limit=5, policy=STRICT)
    assert result.segments == () and result.metrics.dropped == {"unresolved": 1}


# --- default output stays byte-identical --------------------------------------------------


def test_plain_meta_index_gives_the_same_memory_search_output(tmp_path: Path) -> None:
    """A meta-index without lifecycle fields: every mode's output is as without one."""

    from tests.harness.recall_resolvers_probe import imported_memory_search

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    none = imported_memory_search({"meta": "none"}, tmp_path / "a")
    plain = imported_memory_search({"meta": "plain"}, tmp_path / "b")
    assert plain == none
    assert '"m-backup"' in plain and '"notes"' not in plain


@pytest.mark.skipif(
    not os.environ.get("MEMFW_ARCH_BASE_REF"),
    reason="opt-in (slow): set MEMFW_ARCH_BASE_REF to compare against that ref",
)
def test_memory_search_is_byte_identical_to_base_ref() -> None:
    from tests.harness import byte_identical

    if not byte_identical.git_available():
        pytest.skip("git is not available")
    results = byte_identical.compare(
        "tests.harness.recall_resolvers_probe:imported_memory_search",
        [{"meta": "none"}, {"meta": "plain"}],
        old_ref=os.environ["MEMFW_ARCH_BASE_REF"],
    )
    for result in results:
        assert result.identical, f"{result.payload}:\n{result.diff[:4000]}"


def test_mcp_two_stage_strict_drops_what_the_meta_index_retires(tmp_path: Path) -> None:
    """End to end: the MCP ``two_stage`` strict path reads the meta-index lifecycle."""

    from memfw.mcp.server import Gateway

    repository = Repository(tmp_path / "vault")
    repository.init()
    for memory_id in ("m-keep", "m-retired", "m-expired"):
        repository.add(
            f"Backup {memory_id}",
            "backup notes",
            memory_id=memory_id,
            tags=[f"fleet_path:{memory_id}.md"],
        )
    _write_meta_index(
        repository.root,
        [
            {"path": "m-keep.md", "lifecycle": "active"},
            {"path": "m-retired.md", "lifecycle": "retired"},
            {"path": "m-expired.md", "valid_until": PAST},
        ],
    )
    MemoryIndex(repository.index_path).rebuild(repository)
    gateway = Gateway(repository.root, tenant="", entity_enabled=False, allowed_sources=())
    sent: list[dict[str, Any]] = []
    gateway.respond = sent.append  # type: ignore[method-assign]

    def ids(arguments: dict[str, Any]) -> set[str]:
        sent.clear()
        gateway.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "memory_search", "arguments": arguments},
            }
        )
        payload = json.loads(sent[0]["result"]["content"][0]["text"])
        assert "notes" not in payload
        return {row["id"] for row in payload["results"]}

    strict = {"query": "backup", "mode": "two_stage", "policy": "strict"}
    assert ids(strict) == {"m-keep"}
    assert ids({"query": "backup", "mode": "two_stage"}) == {"m-keep", "m-retired", "m-expired"}
    assert ids({"query": "backup", "mode": "fts"}) == {"m-keep", "m-retired", "m-expired"}
