"""A5a swap drills: every port has at least two implementations passing the same contract kit.

Each port is parametrized over every implementation memfw ships and run
through the *same* ``memfw.ports.testing.check_*`` function. A coordinator
drill then swaps the FTS provider for the stdlib ``InMemoryRecallProvider``
and checks that the pipeline (resolve, filter policy, fusion, labels) and
tenant/privacy isolation still hold.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memfw.core.repository import Repository
from memfw.dream.claims import DreamClaimStager, DreamClaimVerifier
from memfw.index import MemoryIndex
from memfw.notes import HashEmbeddingProvider
from memfw.ports import (
    AdapterRegistry,
    AdaptersConfig,
    ClaimEvidence,
    Clock,
    DeriveRequest,
    FixedClock,
    IdGen,
    IngestItem,
    ProjectionPolicy,
    RecallBudget,
    SeededIdGen,
    StagedClaim,
    SystemClock,
    UuidIdGen,
)
from memfw.ports.reference import (
    ConcatDeriver,
    FileClaimStager,
    InMemoryRecallProvider,
    JsonIngestor,
    JsonlProjector,
    KeywordClaimVerifier,
    MarkdownIngestor,
    MirrorProjector,
    TimelineDeriver,
    items_from_json,
    items_from_markdown,
    reference_specs,
    register_reference,
)
from memfw.ports.reference._vault import parse_memory, read_memory
from memfw.ports.reference.ingestors import read_header
from memfw.ports.testing import (
    ContractError,
    check_claim_stager,
    check_claim_verifier,
    check_clock,
    check_deriver,
    check_id_gen,
    check_ingestor,
    check_projector,
    check_recall_provider,
    tree_digest,
)
from memfw.recall import Coordinator, FilterPolicy
from memfw.recall.builtin import (
    FtsProvider,
    HybridProvider,
    MultihopProvider,
    default_resolvers,
    meta_from_document,
)
from tests.conversation_fixtures import make_vault

T0 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
LOCK = (".memfw.lock",)
QUERIES = ("homelab backup", "HAL9000", "cron before disk migration", "迁移 备份", "release")


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    add = repository.add
    add("Homelab backup", "nightly homelab backup to the NAS", memory_id="m-backup")
    add("HAL9000 rack", "HAL9000 rack in the homelab, backup power", memory_id="m-hal")
    add("NAS1 migration", "pause cron before the disk migration on NAS1", memory_id="m-nas1")
    add("Release", "release v0.2.0 notes for memfw", memory_id="m-release")
    add("迁移计划", "迁移之前先暂停定时任务，再做备份", memory_id="m-zh")
    add("Private backup", "private backup keys", memory_id="m-priv", tags=["sensitivity:private"])
    add(
        "Retired backup",
        "retired homelab backup plan",
        memory_id="m-old",
        tags=["lifecycle:retired"],
    )  # noqa: E501
    add("Alpha backup", "tenant alpha homelab backup notes", memory_id="m-alpha", tenant="alpha")
    MemoryIndex(repository.index_path).rebuild(repository)
    return repository


# --- the matrix -------------------------------------------------------------

Factory = Callable[[Path], object]

RECALL: dict[str, Factory] = {
    "fts": lambda root: FtsProvider(root),
    "multihop": lambda root: MultihopProvider(root),
    "hybrid": lambda root: HybridProvider(root, embedder=HashEmbeddingProvider(8)),
    "inmemory": lambda root: InMemoryRecallProvider.from_vault(root),
}
#: Providers whose search writes to the vault, so the kit's byte-identical
#: vault check is not applied to them: the hybrid retriever builds its vector
#: index in the vault on first use and appends ``memory_access`` events on
#: every search (pre-existing behaviour, tracked separately; as in
#: ``tests/test_recall_builtin.py``).
WRITES_VAULT = frozenset({"hybrid"})
PROJECTORS: dict[str, Callable[[], object]] = {
    "jsonl-index": lambda: JsonlProjector(clock=FixedClock(T0)),
    "markdown-mirror": lambda: MirrorProjector(),
}
INGESTORS: dict[str, Callable[[], object]] = {
    "json": JsonIngestor,
    "markdown": MarkdownIngestor,
}
DERIVERS: dict[str, Callable[[], object]] = {
    "concat": ConcatDeriver,
    "timeline": TimelineDeriver,
}
CLOCKS: dict[str, Callable[[], Clock]] = {
    "system": SystemClock,
    "fixed": lambda: FixedClock(T0),
}
ID_GENS: dict[str, Callable[[], IdGen]] = {
    "uuid": UuidIdGen,
    "seeded": lambda: SeededIdGen("drill"),
}
VERIFIERS: dict[str, Callable[[], object]] = {
    "dream": DreamClaimVerifier,
    "keyword": KeywordClaimVerifier,
}
STAGERS: dict[str, Callable[[], object]] = {
    "dream": DreamClaimStager,
    "file": FileClaimStager,
}

MATRIX: dict[str, dict[str, object]] = {
    "RecallProvider": dict(RECALL),
    "Projector": dict(PROJECTORS),
    "Ingestor": dict(INGESTORS),
    "Deriver": dict(DERIVERS),
    "Clock": dict(CLOCKS),
    "IdGen": dict(ID_GENS),
    "ClaimVerifier": dict(VERIFIERS),
    "ClaimStager": dict(STAGERS),
}


def test_every_port_has_at_least_two_implementations() -> None:
    """Architecture section 9: each port has two implementations passing one kit."""

    assert {port: len(impls) for port, impls in MATRIX.items() if len(impls) < 2} == {}


# --- RecallProvider ---------------------------------------------------------


@pytest.mark.parametrize("name", sorted(RECALL))
def test_recall_providers_pass_the_same_contract(tmp_path: Path, name: str) -> None:
    repository = _vault(tmp_path)
    provider = RECALL[name](repository.root)
    tenants = {doc.id: doc.tenant for doc in repository.list()}
    seen = check_recall_provider(
        provider,  # type: ignore[arg-type]
        queries=QUERIES,
        limit=5,
        vault=None if name in WRITES_VAULT else repository.root,
        vault_exclude=LOCK,
        tenant_of=lambda candidate: tenants.get(candidate.ref),
    )
    assert seen["HAL9000"] and seen["HAL9000"][0].ref == "m-hal"
    alpha = check_recall_provider(
        provider,  # type: ignore[arg-type]
        queries=["homelab backup"],
        tenant="alpha",
        tenant_of=lambda candidate: tenants.get(candidate.ref),
    )
    assert [c.ref for c in alpha["homelab backup"]] in ([], ["m-alpha"])


def test_inmemory_provider_ranks_and_tokenizes() -> None:
    provider = InMemoryRecallProvider(
        {"a": "backup the homelab nightly", "b": "homelab rack", "c": "迁移之前先备份"},
        tenants={"b": "alpha"},
    )
    seen = check_recall_provider(provider, queries=["homelab backup", "迁移 备份", ""])
    assert [c.ref for c in seen["homelab backup"]] == ["a"]
    assert [c.ref for c in seen["迁移 备份"]] == ["c"]
    assert seen[""] == []
    alpha = check_recall_provider(provider, queries=["homelab"], tenant="alpha")
    assert [c.ref for c in alpha["homelab"]] == ["b"]
    with pytest.raises(ContractError, match="raised on an ordinary query"):
        check_recall_provider(provider, queries=["x" * 600])
    check_recall_provider(provider, queries=["a"], error_queries=["x" * 600])
    with pytest.raises(ValueError, match="memory ids"):
        InMemoryRecallProvider({"../etc": "x"})


# --- Projector --------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PROJECTORS))
def test_projectors_pass_the_same_contract(tmp_path: Path, name: str) -> None:
    repository = _vault(tmp_path)
    policy = ProjectionPolicy(version="drill-v1")
    manifest = check_projector(
        PROJECTORS[name](),  # type: ignore[arg-type]
        vault=repository.root,
        out=tmp_path / "out",
        policy=policy,
        vault_exclude=LOCK,
    )
    assert manifest.projected == 7
    assert manifest.skipped == {"private": 1}
    everything = PROJECTORS[name]().export(  # type: ignore[attr-defined]
        repository.root, tmp_path / "all", ProjectionPolicy("v2", {"include_private": True})
    )
    assert everything.projected == 8 and everything.skipped == {}
    with pytest.raises(ValueError, match="inside the vault"):
        PROJECTORS[name]().export(repository.root, repository.root / "out", policy)  # type: ignore[attr-defined]


def test_jsonl_projector_carries_no_body(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    JsonlProjector().export(repository.root, tmp_path / "out", ProjectionPolicy("v1"))
    text = (tmp_path / "out" / "memories.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in text.splitlines()]
    assert [r["id"] for r in records] == sorted(r["id"] for r in records)
    assert "nightly homelab backup" not in text
    alpha = next(r for r in records if r["id"] == "m-alpha")
    assert alpha["tenant"] == "alpha" and len(alpha["body_sha256"]) == 64


def test_mirror_projector_copies_bytes(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    MirrorProjector().export(repository.root, tmp_path / "out", ProjectionPolicy("v1"))
    for memory_id in ("m-backup", "m-zh"):
        name = f"{memory_id}.md"
        expected = (repository.memories_dir / name).read_bytes()
        assert (tmp_path / "out" / "memories" / name).read_bytes() == expected
    assert not (tmp_path / "out" / "memories" / "m-priv.md").exists()


# --- privacy floor: never looser than core -----------------------------------

RANK = {"none": 0, "local-private": 1, "unknown": 2}
TAG_CORPUS: list[list[str]] = [
    [],
    ["x"],
    ["sensitivity:private"],
    ["Sensitivity:PRIVATE"],
    ["sensitivity: private "],
    ["sensitivity:secret"],
    ["sensitivity:local-private"],
    ["sensitivity:"],
    ["sensitivity:private", "sensitivity:secret"],
    ["privacy:none"],
    ["privacy:public"],
    ["privacy:fleet"],
    ["Privacy:Shared"],
    ["privacy:"],
    ["privacy:null"],
    ["privacy:local-private"],
    ["privacy:LOCAL-PRIVATE"],
    ["privacy:private"],
    ["privacy:restricted"],
    ["privacy:team-only"],
    ["privacy:none", "privacy:local-private"],
    ["privacy:local-private", "privacy:none"],
    ["privacy:none", "sensitivity:private"],
    ["privacy:local-private", "sensitivity:secret"],
    ["privacy:restricted", "privacy:none"],
    ["privacy: x"],
    ["a 'quoted' tag", "it's", 'say "hi"', "sensitivity:private # not a comment"],
    ["- dash", "#hash", "sensitivity:Secret"],
]


def test_reference_privacy_class_is_never_looser_than_core(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "vault")
    repository.init()
    for index, tags in enumerate(TAG_CORPUS):
        repository.add(f"Doc {index}", "body", memory_id=f"t{index:02d}", tags=tags)
    for document in repository.list():
        core, _mismatch = meta_from_document(document).privacy_class()
        memory = read_memory(repository.root, document.id)
        assert memory is not None, document.id
        reference = memory.privacy_class()
        assert RANK[reference] >= RANK[core], (document.tags, core, reference)
        assert reference == core, (document.tags, core, reference)  # exact on this corpus


@pytest.mark.parametrize(
    ("front", "expected"),
    [
        ("tags: [sensitivity:private]", None),
        ("tags: []", "none"),
        ("tags:\n  - sensitivity:private", None),
        ("tags:\n    - x", None),
        ("tags: |\n  sensitivity:private", None),
        ("tags:\n- a\ntags:\n- b", None),
        ("tags: {a: 1}", None),
        ("tags:\n- sensitivity:secret", "unknown"),
        ("tags:\n- 'privacy:restricted' # comment", "unknown"),
        ("privacy: restricted", "unknown"),
        ("sensitivity: private", "local-private"),
        ('tags:\n- "sensitivity:\\x70rivate"', "local-private"),
        ('tags:\n- "bad \\q escape"', None),
    ],
)
def test_parser_fails_closed_on_privacy_forms(front: str, expected: str | None) -> None:
    memory = parse_memory("m1", f"---\nid: m1\n{front}\nversion: 1\n---\nbody\n".encode())
    assert (None if memory is None else memory.privacy_class()) == expected


def test_scalars_drop_inline_comments_like_yaml() -> None:
    memory = parse_memory(
        "m1",
        b"---\nid: m1\ntenant: alpha # team\ntitle: 'It''s # here' # c\nsource: a#b\n---\n",
    )
    assert memory is not None
    assert (memory.text("tenant"), memory.text("title"), memory.text("source")) == (
        "alpha",
        "It's # here",
        "a#b",
    )


def _privacy_vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "pvault")
    repository.init()
    repository.add("Public", "public body", memory_id="p-pub")
    repository.add("Secret", "secret body", memory_id="p-secret", tags=["sensitivity:secret"])
    repository.add("Restricted", "r body", memory_id="p-restr", tags=["privacy:restricted"])
    repository.add("Private", "private body", memory_id="p-priv", tags=["Sensitivity:PRIVATE"])
    for memory_id, tags in (("p-flow", "tags: [sensitivity:private]"), ("p-indent", None)):
        repository.add(memory_id, "hand edited", memory_id=memory_id, tags=["sensitivity:private"])
        path = repository.memories_dir / f"{memory_id}.md"
        text = path.read_text(encoding="utf-8")
        if tags is not None:
            text = text.replace("tags:\n- sensitivity:private\n", tags + "\n")
        else:
            text = text.replace("- sensitivity:private", "  - sensitivity:private")
        path.write_text(text, encoding="utf-8")
    return repository


@pytest.mark.parametrize("name", sorted(PROJECTORS))
def test_projectors_apply_the_privacy_floor(tmp_path: Path, name: str) -> None:
    repository = _privacy_vault(tmp_path)
    strict = PROJECTORS[name]().export(  # type: ignore[attr-defined]
        repository.root, tmp_path / "strict", ProjectionPolicy("v1")
    )
    assert strict.projected == 1
    assert strict.skipped == {"private": 1, "privacy_unknown": 2, "unreadable": 2}
    cleared = PROJECTORS[name]().export(  # type: ignore[attr-defined]
        repository.root, tmp_path / "cleared", ProjectionPolicy("v1", {"include_private": True})
    )
    assert cleared.projected == 2  # unknown and unreadable are never exported
    assert cleared.skipped == {"privacy_unknown": 2, "unreadable": 2}
    exported = b"".join((tmp_path / "cleared" / f).read_bytes() for f in cleared.files)
    for body in (b"secret body", b"r body", b"hand edited"):
        assert body not in exported


@pytest.mark.parametrize("name", sorted(DERIVERS))
def test_derivers_apply_the_privacy_floor(tmp_path: Path, name: str) -> None:
    repository = _privacy_vault(tmp_path)
    ids = ("p-pub", "p-secret", "p-restr", "p-priv", "p-flow", "p-indent")
    view = DERIVERS[name]().derive(  # type: ignore[attr-defined]
        repository.root, DeriveRequest(kind="digest", memory_ids=ids)
    )
    assert view.cited_ids() == ("p-pub",)
    cleared = DERIVERS[name]().derive(  # type: ignore[attr-defined]
        repository.root,
        DeriveRequest(kind="digest", memory_ids=ids, options={"include_private": True}),
    )
    assert set(cleared.cited_ids()) == {"p-pub", "p-priv"}


def test_mirror_removes_copies_of_memories_that_became_private(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    out = tmp_path / "out"
    MirrorProjector().export(repository.root, out, ProjectionPolicy("v1"))
    assert (out / "memories" / "m-release.md").is_file()
    repository.update("m-release", tags=["sensitivity:private"])
    manifest = MirrorProjector().export(repository.root, out, ProjectionPolicy("v1"))
    assert not (out / "memories" / "m-release.md").exists()
    assert sorted(p.name for p in (out / "memories").glob("*.md")) == sorted(
        name.split("/")[-1] for name in manifest.files
    )


@pytest.mark.parametrize("name", sorted(PROJECTORS))
def test_projectors_refuse_symlinked_outputs(tmp_path: Path, name: str) -> None:
    repository = _vault(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        PROJECTORS[name]().export(repository.root, linked, ProjectionPolicy("v1"))  # type: ignore[attr-defined]
    out = tmp_path / "out"
    out.mkdir()
    (out / "memories").symlink_to(elsewhere, target_is_directory=True)
    if name == "markdown-mirror":
        with pytest.raises(ValueError, match="symlink"):
            MirrorProjector().export(repository.root, out, ProjectionPolicy("v1"))
    assert list(elsewhere.iterdir()) == []


def test_writes_replace_a_symlinked_file_instead_of_following_it(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    target = tmp_path / "victim.txt"
    target.write_text("keep", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "memories.jsonl").symlink_to(target)
    JsonlProjector().export(repository.root, out, ProjectionPolicy("v1"))
    assert target.read_text(encoding="utf-8") == "keep"
    assert not (out / "memories.jsonl").is_symlink()


# --- Ingestor ---------------------------------------------------------------

ITEMS = [
    IngestItem(title="Note one", body="first body", external_ref="ext-1"),
    IngestItem(title="Note two", body="second body", external_ref="ext-2", metadata={"k": 1}),
    IngestItem(title="Note one", body="first body", external_ref="ext-1"),
]


@pytest.mark.parametrize("name", sorted(INGESTORS))
def test_ingestors_pass_the_same_contract(tmp_path: Path, name: str) -> None:
    repository = _vault(tmp_path)
    before = tree_digest(repository.root, exclude=("_candidates", *LOCK))
    result = check_ingestor(INGESTORS[name](), vault=repository.root, items=ITEMS)  # type: ignore[arg-type]
    assert len(result.staged) == 2 and len(result.duplicates) == 1
    assert tree_digest(repository.root, exclude=("_candidates", *LOCK)) == before
    assert sorted(p.name for p in (repository.root / "_candidates" / name).iterdir()) == sorted(
        f"{cid}.{'md' if name == 'markdown' else 'json'}" for cid in result.staged
    )


def test_ingestor_ids_agree_across_formats(tmp_path: Path) -> None:
    """The same item gets the same candidate id under the same ingestor name."""

    first = make_vault(tmp_path / "a")
    second = make_vault(tmp_path / "b")
    one = JsonIngestor(name="ext").stage(first, ITEMS[:1], proposal_id="p1", source="s")
    two = MarkdownIngestor(name="ext").stage(second, ITEMS[:1], proposal_id="p1", source="s")
    assert one.staged == two.staged
    header = read_header(second / "_candidates" / "ext" / f"{two.staged[0]}.md")
    assert header["title"] == "Note one" and header["proposal_id"] == "p1"


def test_item_loaders(tmp_path: Path) -> None:
    lines = '{"title": "A", "body": "x"}\n{"title": "B", "body": "y", "metadata": {"n": 2}}\n'
    assert [item.title for item in items_from_json(lines)] == ["A", "B"]
    assert items_from_json('[{"title": "C"}]')[0].body == ""
    with pytest.raises(ValueError):
        items_from_json('[{"title": "C", "metadata": {"deep": {}}}]')
    note = tmp_path / "note.md"
    note.write_text("\n# Heading\n\nbody text\n", encoding="utf-8")
    plain = tmp_path / "plain.md"
    plain.write_text("no heading here\n", encoding="utf-8")
    loaded = items_from_markdown([note, plain])
    assert [(i.title, i.body, i.external_ref) for i in loaded] == [
        ("Heading", "body text", "note.md"),
        ("plain", "no heading here", "plain.md"),
    ]


# --- Deriver ----------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(DERIVERS))
def test_derivers_pass_the_same_contract(tmp_path: Path, name: str) -> None:
    repository = _vault(tmp_path)
    request = DeriveRequest(kind="digest", memory_ids=("m-nas1", "m-backup", "missing", "m-nas1"))
    view = check_deriver(
        DERIVERS[name](),  # type: ignore[arg-type]
        vault=repository.root,
        request=request,
        vault_exclude=LOCK,
    )
    assert set(view.cited_ids()) == {"m-nas1", "m-backup"}
    assert all(citation.version == 1 for citation in view.citations)
    assert "nightly" not in view.content  # titles only, never a body
    with pytest.raises(ValueError, match="none of the requested"):
        DERIVERS[name]().derive(  # type: ignore[attr-defined]
            repository.root, DeriveRequest(kind="digest", memory_ids=("missing",))
        )


def test_concat_keeps_request_order_timeline_sorts(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    request = DeriveRequest(kind="digest", memory_ids=("m-release", "m-backup"))
    concat = ConcatDeriver().derive(repository.root, request)
    assert concat.content.splitlines() == ["- Release [m-release]", "- Homelab backup [m-backup]"]
    timeline = TimelineDeriver().derive(repository.root, request)
    assert timeline.cited_ids() == ("m-backup", "m-release")  # created first


# --- Clock / IdGen ----------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CLOCKS))
def test_clocks_pass_the_same_contract(name: str) -> None:
    check_clock(CLOCKS[name]())


@pytest.mark.parametrize("name", sorted(ID_GENS))
def test_id_gens_pass_the_same_contract(name: str) -> None:
    check_id_gen(ID_GENS[name]())


# --- ClaimVerifier / ClaimStager ---------------------------------------------

PAIRS = [
    ("Uploader retry uses exponential backoff.", "The uploader retry uses exponential backoff."),
    ("Uploader retry uses exponential backoff.", "The uploader retry does not use backoff."),
    ("Port is 8080.", "Unrelated text about lunch."),
]
CLAIMS = [
    StagedClaim(
        claim="Uploader retry uses exponential backoff.",
        oid="conversation-codex",
        evidence=(ClaimEvidence(ref="conv:codex:s1@0", session_id="s1", timestamp=None),),
    )
]


@pytest.mark.parametrize("name", sorted(VERIFIERS))
def test_claim_verifiers_pass_the_same_contract(name: str) -> None:
    answers = check_claim_verifier(VERIFIERS[name](), pairs=PAIRS)  # type: ignore[arg-type]
    assert answers[0] == "support" and answers[2] == "neutral"


def test_keyword_verifier_polarities() -> None:
    verifier = KeywordClaimVerifier()
    assert [verifier.polarity(claim, text) for claim, text in PAIRS] == [
        "support",
        "contradict",
        "neutral",
    ]
    assert verifier.polarity("the a is", "anything") == "neutral"
    with pytest.raises(ValueError):
        KeywordClaimVerifier(min_overlap=0)


@pytest.mark.parametrize("name", sorted(STAGERS))
def test_claim_stagers_pass_the_same_contract(tmp_path: Path, name: str) -> None:
    vault = make_vault(tmp_path / "vault")
    staged = check_claim_stager(STAGERS[name](), vault=vault, claims=CLAIMS)  # type: ignore[arg-type]
    assert staged


def test_file_claim_stager_writes_one_batch_file(tmp_path: Path) -> None:
    vault = make_vault(tmp_path / "vault")
    stager = FileClaimStager()
    assert stager.stage_claims(vault, "b1", CLAIMS, {"n": 1}, created_at="2026-01-01T00:00:00Z")
    record = json.loads((vault / "_candidates" / "claims-ref" / "b1.json").read_text("utf-8"))
    assert record["claims"][0]["evidence"][0]["ref"] == "conv:codex:s1@0"
    assert record["report"] == {"n": 1}
    with pytest.raises(ValueError, match="batch_id"):
        stager.stage_claims(vault, "../x", CLAIMS, {}, created_at="2026-01-01T00:00:00Z")


# --- registry: opt-in, default off -------------------------------------------


def test_reference_specs_are_off_until_enabled(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    registry = AdapterRegistry()
    names = register_reference(registry)
    assert set(names) == {spec.name for spec in reference_specs()}
    assert register_reference(registry) == ()
    off = registry.load_enabled(AdaptersConfig())
    assert off == ()
    assert registry.load("concat", AdaptersConfig()).reason == "disabled"
    config = AdaptersConfig.from_mapping(
        {"enabled": list(names), "inmemory": {"vault": str(repository.root)}}
    )
    statuses = {status.name: status for status in registry.load_enabled(config)}
    assert all(status.available for status in statuses.values()), statuses
    provider = statuses["inmemory"].adapter
    check_recall_provider(provider, queries=QUERIES)  # type: ignore[arg-type]
    missing = AdaptersConfig.from_mapping({"enabled": ["inmemory"]})
    assert registry.load("inmemory", missing).reason == "factory_error"


def test_importing_memfw_ports_does_not_register_reference() -> None:
    from memfw.ports import default_registry

    assert not {spec.name for spec in reference_specs()} & {
        info.name for info in default_registry().listing()
    }


# --- coordinator drill --------------------------------------------------------


def test_coordinator_runs_with_the_reference_provider_in_place_of_fts(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    root = repository.root
    resolvers = default_resolvers(root)
    fts = Coordinator([FtsProvider(root)], resolvers=resolvers)
    swapped = Coordinator([InMemoryRecallProvider.from_vault(root)], resolvers=resolvers)
    policy = FilterPolicy()
    for query in QUERIES:
        expected = fts.search(query, limit=10, policy=policy)
        result = swapped.search(query, limit=10, policy=policy)
        assert result.labels.providers == ("inmemory",)
        assert result.labels.degraded == {}
        assert not result.labels.fail_closed
        assert result.labels.route == expected.labels.route
        # Same visible set from a different ranker: the policy, not the
        # provider, decides what is visible.
        assert set(result.ids) <= {"m-backup", "m-hal", "m-nas1", "m-release", "m-zh"}
        assert all(hit.providers == ("inmemory",) for hit in result.hits)
    backup = swapped.search("homelab backup", limit=10, policy=policy)
    assert backup.ids[0] == "m-backup"
    assert backup.metrics.dropped["privacy"] == 1  # m-priv matched; the policy dropped it
    assert backup.metrics.dropped["retired"] == 1
    cleared = FilterPolicy(privacy_clearance=frozenset({"none", "local-private"}))
    private = swapped.search("private backup keys", limit=10, policy=cleared)
    assert "m-priv" in private.ids
    assert "m-priv" not in swapped.search("private backup keys", limit=10, policy=policy).ids
    assert "m-old" not in swapped.search("retired homelab backup", limit=10, policy=policy).ids


def test_coordinator_isolation_holds_when_the_provider_ignores_tenants(tmp_path: Path) -> None:
    """A provider that does not know tenants still cannot leak across them."""

    repository = _vault(tmp_path)
    root = repository.root
    documents = {doc.id: f"{doc.title}\n{doc.body}" for doc in repository.list()}
    blind = InMemoryRecallProvider(documents)  # every doc in the empty tenant
    coordinator = Coordinator([blind], resolvers=default_resolvers(root))
    result = coordinator.search("homelab backup", limit=10, policy=FilterPolicy())
    raw = blind.search("homelab backup", limit=10, budget=RecallBudget(), tenant="", as_of=None)
    assert "m-alpha" in [candidate.ref for candidate in raw]
    assert "m-alpha" not in result.ids
    assert result.metrics.dropped["tenant"] >= 1
    assert "m-old" not in result.ids and result.metrics.dropped["retired"] >= 1


def test_fusion_with_fts_and_reference_provider(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    root = repository.root
    coordinator = Coordinator(
        [FtsProvider(root), InMemoryRecallProvider.from_vault(root)],
        resolvers=default_resolvers(root),
    )
    result = coordinator.search("homelab backup power", limit=10, policy=FilterPolicy())
    assert result.labels.providers == ("fts", "inmemory")
    assert not result.labels.fail_closed
    both = [hit for hit in result.hits if hit.providers == ("fts", "inmemory")]
    assert both, result.hits
    assert set(both[0].ranks) == {"fts", "inmemory"}
    alpha = coordinator.search(
        "homelab backup", limit=10, policy=FilterPolicy().with_tenant("alpha")
    )
    assert alpha.ids == ["m-alpha"]
