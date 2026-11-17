"""O1a: the read-only Obsidian projection (``memfw.projections.obsidian``)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from memfw.cli import app
from memfw.core.repository import Repository
from memfw.ports import FixedClock, ProjectionPolicy, Projector
from memfw.ports.testing import check_projector, tree_digest
from memfw.projections import MANIFEST_PATH, ObsidianProjector, parse_clearance
from memfw.projections.obsidian import privacy_level, safe_name

T0 = datetime(2026, 1, 1, tzinfo=UTC)
LOCK = (".memfw.lock",)

#: Markers that only hidden memories carry (id, title, body, concept); none may
#: appear anywhere in a default-clearance board.
HIDDEN_MARKERS = (
    "m-priv",
    "m-upper",
    "m-ptag",
    "m-pkey",
    "m-secret",
    "m-restricted",
    "m-mixed",
    "m-alpha",
    "ZEBRAPRIVATE",
    "QUOKKAALPHA",
    "concept:hidden:only",
    "concept-hidden-only",
)


def _raw(vault: Path, memory_id: str, front: str, body: str) -> None:
    text = (
        f"---\nid: {memory_id}\ntitle: {memory_id} title\n"
        "created_at: '2026-01-01T00:00:00Z'\nupdated_at: '2026-01-01T00:00:00Z'\n"
        f"{front}---\n\n{body}\n"
    )
    (vault / "memories" / f"{memory_id}.md").write_text(text, encoding="utf-8")


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    add = repository.add
    add(
        "Backup",
        "nightly backup to the NAS",
        memory_id="m-pub",
        tags=["privacy:public", "ops:backup", "fleet_path:x/y.md"],
        about=["concept:demo:backup", "concept:hidden:shared"],
        epistemic="fact",
    )
    add("Shared note", "shared default note", memory_id="m-shared", about=["concept:demo:backup"])
    add(
        "Private",
        "ZEBRAPRIVATE keys",
        memory_id="m-priv",
        tags=["sensitivity:private"],
        about=["concept:hidden:only", "concept:hidden:shared"],
    )
    add("Upper", "ZEBRAPRIVATE upper", memory_id="m-upper", tags=["SENSITIVITY:Private"])
    add("Ptag", "ZEBRAPRIVATE ptag", memory_id="m-ptag", tags=["privacy:LOCAL-PRIVATE"])
    add("Secret", "ZEBRAPRIVATE secret", memory_id="m-secret", tags=["sensitivity:secret"])
    add("Restricted", "ZEBRAPRIVATE r", memory_id="m-restricted", tags=["privacy:restricted"])
    add(
        "Mixed",
        "ZEBRAPRIVATE mixed",
        memory_id="m-mixed",
        tags=["privacy:public", "sensitivity:private"],
    )
    add("Alpha", "QUOKKAALPHA tenant", memory_id="m-alpha", tenant="alpha")
    # A top-level privacy key core ignores but the strict reader honours.
    _raw(repository.root, "m-pkey", "privacy: local-private\n", "ZEBRAPRIVATE key")
    # Links from visible memories to hidden ones must not be written.
    _raw(repository.root, "m-old", "superseded_by: m-priv\n", "old public note")
    add(
        "Which backup?",
        "open question body",
        memory_id="q-1",
        memory_type="open-question",
        open_question={
            "question": "Which backup target?",
            "conflicts": ["m-pub", "m-priv"],
            "resolved_by": None,
        },
    )
    repository.supersede(
        "m-shared", "Shared note v2", "shared default note v2", memory_id_new="m-new"
    )
    return repository


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _blob(root: Path) -> str:
    return "\n".join(f"{name}\n{data.decode('utf-8')}" for name, data in _tree(root).items())


# --- contract -----------------------------------------------------------------


def test_obsidian_projector_passes_the_projector_contract(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    projector = ObsidianProjector(clock=FixedClock(T0))
    assert isinstance(projector, Projector)
    manifest = check_projector(
        projector,
        vault=repository.root,
        out=tmp_path / "out",
        policy=ProjectionPolicy("obsidian-v1"),
        vault_exclude=LOCK,
    )
    assert manifest.projector == "obsidian"
    assert manifest.projected == 5  # m-pub, m-shared, m-new, m-old, q-1
    assert "memories/m-pub.md" in manifest.files and "questions/q-1.md" in manifest.files


# --- privacy ------------------------------------------------------------------


def test_default_clearance_leaks_no_local_private_or_unknown(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    out = tmp_path / "board"
    ObsidianProjector().build(repository.root, out)
    blob = _blob(out)
    for marker in HIDDEN_MARKERS:
        assert marker not in blob, marker
    names = set(_tree(out))
    assert {
        "memories/m-pub.md",
        "memories/m-shared.md",
        "memories/m-new.md",
        "memories/m-old.md",
        "questions/q-1.md",
    } <= names
    concept = next(name for name in names if name.startswith("concepts/concept-demo-backup"))
    front = yaml.safe_load(_tree(out)[concept].decode().split("---\n")[1])
    assert front["memory_count"] == 3  # m-pub, m-shared and its successor m-new
    shared = next(name for name in names if name.startswith("concepts/concept-hidden-shared"))
    # used by m-pub and m-priv: the page exists, but counts and links only m-pub
    shared_front = yaml.safe_load(_tree(out)[shared].decode().split("---\n")[1])
    assert shared_front["memory_count"] == 1
    assert shared_front["memories"] == ["[[memories/m-pub]]"]
    old = (out / "memories" / "m-old.md").read_text(encoding="utf-8")
    assert "superseded_by: null" in old
    question = (out / "questions" / "q-1.md").read_text(encoding="utf-8")
    assert "[[memories/m-pub]]" in question and "m-priv" not in question


def test_clearance_levels(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    public = tmp_path / "public"
    ObsidianProjector(clearance="PUBLIC").build(repository.root, public)
    assert sorted(n for n in _tree(public) if n.startswith("memories/")) == ["memories/m-pub.md"]
    private = tmp_path / "private"
    ObsidianProjector(clearance="public,shared,local-private").build(repository.root, private)
    names = set(_tree(private))
    for memory_id in ("m-priv", "m-upper", "m-ptag", "m-pkey", "m-mixed"):
        assert f"memories/{memory_id}.md" in names
    blob = _blob(private)
    # unknown privacy is never projected, whatever the clearance
    assert "m-secret" not in blob and "m-restricted" not in blob
    assert "privacy: local-private" in (private / "memories" / "m-priv.md").read_text()


@pytest.mark.parametrize("bad", ["", "unknown", "public,secret", "none", " , "])
def test_bad_clearance_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_clearance(bad)


def test_privacy_strictest_wins(tmp_path: Path) -> None:
    from memfw.core.markdown_parse import parse_memory_markdown
    from memfw.ports.reference._vault import parse_memory

    repository = _vault(tmp_path)
    for memory_id, expected in {
        "m-pub": "public",
        "m-shared": "shared",
        "m-priv": "local-private",
        "m-upper": "local-private",
        "m-ptag": "local-private",
        "m-pkey": "local-private",
        "m-mixed": "local-private",
        "m-secret": "unknown",
        "m-restricted": "unknown",
    }.items():
        raw = (repository.memories_dir / f"{memory_id}.md").read_bytes()
        document = parse_memory_markdown(raw.decode(), expected_id=memory_id)
        memory = parse_memory(memory_id, raw)
        assert memory is not None
        assert privacy_level(document, memory) == expected, memory_id


# --- tenant -------------------------------------------------------------------


def test_tenants_never_mix(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    out = tmp_path / "alpha"
    result = ObsidianProjector(tenant="alpha").build(repository.root, out)
    pages = sorted(n for n in _tree(out) if n.startswith(("memories/", "questions/", "concepts/")))
    assert pages == ["memories/m-alpha.md"]
    assert result.manifest.projected == 1
    blob = _blob(out)
    assert "m-pub" not in blob and "q-1" not in blob
    default = tmp_path / "default"
    ObsidianProjector().build(repository.root, default)
    assert "QUOKKAALPHA" not in _blob(default)


# --- determinism ----------------------------------------------------------------


def test_byte_identical_across_runs_and_clocks(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    first = ObsidianProjector(clock=FixedClock(T0)).build(repository.root, tmp_path / "a")
    later = FixedClock(T0 + timedelta(days=400))
    second = ObsidianProjector(clock=later).build(repository.root, tmp_path / "b")
    assert _tree(tmp_path / "a") == _tree(tmp_path / "b")
    assert first.manifest.content_sha256 == second.manifest.content_sha256
    assert "2027" not in _blob(tmp_path / "b")  # no run timestamp anywhere
    manifest = json.loads((tmp_path / "a" / MANIFEST_PATH).read_text())
    assert list(manifest["files"]) == sorted(manifest["files"])


def test_rebuild_without_changes_writes_nothing(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    out = tmp_path / "board"
    ObsidianProjector().build(repository.root, out)
    stamps = {p: p.stat().st_mtime_ns for p in out.rglob("*") if p.is_file()}
    before = _tree(out)
    result = ObsidianProjector().build(repository.root, out)
    assert result.written == () and result.deleted == () and result.edited == ()
    assert _tree(out) == before
    assert {p: p.stat().st_mtime_ns for p in out.rglob("*") if p.is_file()} == stamps


# --- rebuild --------------------------------------------------------------------


def test_rebuild_deletes_only_manifest_files(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    out = tmp_path / "board"
    ObsidianProjector().build(repository.root, out)
    (out / "memories" / "mine.md").write_text("my own note\n")
    (out / "notes.md").write_text("top level\n")
    (out / ".obsidian").mkdir()
    (out / ".obsidian" / "app.json").write_text("{}\n")
    (repository.memories_dir / "m-pub.md").unlink()
    result = ObsidianProjector().build(repository.root, out)
    assert "memories/m-pub.md" in result.deleted
    assert not (out / "memories" / "m-pub.md").exists()
    # the concept only m-pub used for hidden-shared never existed; demo-backup lost a member
    assert (out / "memories" / "mine.md").read_text() == "my own note\n"
    assert (out / "notes.md").exists() and (out / ".obsidian" / "app.json").exists()
    manifest = json.loads((out / MANIFEST_PATH).read_text())
    assert "memories/m-pub.md" not in manifest["files"]
    assert "m-pub" not in _blob(out / "concepts")


def test_memory_made_private_disappears_on_rebuild(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    out = tmp_path / "board"
    ObsidianProjector().build(repository.root, out)
    path = repository.memories_dir / "m-shared.md"
    path.write_text(
        path.read_text()
        .replace("tags: []\n", "")
        .replace("---\n\n", "tags:\n- sensitivity:private\n---\n\n", 1)
    )
    ObsidianProjector().build(repository.root, out)
    assert not (out / "memories" / "m-shared.md").exists()
    assert "m-shared" not in _blob(out)


def test_hand_edits_are_reported_then_overwritten(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    out = tmp_path / "board"
    ObsidianProjector().build(repository.root, out)
    page = out / "memories" / "m-pub.md"
    original = page.read_bytes()
    page.write_text("edited by hand\n")
    result = ObsidianProjector().build(repository.root, out)
    assert result.edited == ("memories/m-pub.md",)
    assert page.read_bytes() == original


def test_cli_warns_about_hand_edits(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    out = tmp_path / "board"
    runner = CliRunner()
    args = ["project", "obsidian", "--root", str(repository.root), "--out", str(out)]
    assert runner.invoke(app, args).exit_code == 0
    (out / "questions" / "q-1.md").write_text("x\n")
    result = runner.invoke(app, args)
    assert result.exit_code == 0
    assert "hand-edited" in result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["edited"] == ["questions/q-1.md"]


def test_unlisted_file_at_a_generated_path_is_refused(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    out = tmp_path / "board"
    (out / "memories").mkdir(parents=True)
    (out / "memories" / "m-pub.md").write_text("someone else's file\n")
    with pytest.raises(ValueError, match="not in the previous manifest"):
        ObsidianProjector().build(repository.root, out)
    assert (out / "memories" / "m-pub.md").read_text() == "someone else's file\n"
    assert not (out / MANIFEST_PATH).exists()


# --- path safety ----------------------------------------------------------------


def test_out_inside_containing_or_equal_to_vault_is_refused(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    for out in (repository.root, repository.root / "board", repository.root / "memories", tmp_path):
        with pytest.raises(ValueError, match="vault"):
            ObsidianProjector().build(repository.root, out)


def test_symlinked_out_or_folder_is_refused(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        ObsidianProjector().build(repository.root, link)
    out = tmp_path / "board"
    out.mkdir()
    (out / "memories").symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        ObsidianProjector().build(repository.root, out)
    assert list(real.iterdir()) == []


def test_escaping_concept_ids_stay_inside(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    repository.add(
        "Escape", "x", memory_id="m-esc", about=["../../etc/passwd", "..", "a/b\\c", "ok-name"]
    )
    out = tmp_path / "board"
    ObsidianProjector().build(repository.root, out)
    concepts = sorted(p.name for p in (out / "concepts").iterdir())
    assert all(
        p.resolve().parent == (out / "concepts").resolve() for p in (out / "concepts").iterdir()
    )
    assert "ok-name.md" in concepts
    assert not any(".." in name.removesuffix(".md") for name in concepts)
    assert not (tmp_path / "etc").exists()
    assert safe_name("..") != safe_name(".") and safe_name("a:b") != safe_name("a/b")


def test_manifest_with_unsafe_path_is_refused(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    out = tmp_path / "board"
    ObsidianProjector().build(repository.root, out)
    victim = tmp_path / "victim.md"
    victim.write_text("keep me\n")
    manifest = json.loads((out / MANIFEST_PATH).read_text())
    manifest["files"]["../victim.md"] = "0" * 64
    (out / MANIFEST_PATH).write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="unsafe path"):
        ObsidianProjector().build(repository.root, out)
    assert victim.read_text() == "keep me\n"


def test_symlinked_memory_files_are_not_followed(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text(
        "---\nid: m-link\ntitle: t\ncreated_at: '2026-01-01T00:00:00Z'\n"
        "updated_at: '2026-01-01T00:00:00Z'\n---\n\nOUTSIDEVAULT\n"
    )
    os.symlink(outside, repository.memories_dir / "m-link.md")
    out = tmp_path / "board"
    ObsidianProjector().build(repository.root, out)
    assert "OUTSIDEVAULT" not in _blob(out)


# --- the vault is only read -------------------------------------------------------


def test_never_writes_the_vault(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    before = tree_digest(repository.root)
    out = tmp_path / "board"
    ObsidianProjector().build(repository.root, out)
    (out / "memories" / "m-pub.md").write_text("edit\n")
    ObsidianProjector(clearance="local-private").build(repository.root, out)
    runner = CliRunner()
    result = runner.invoke(
        app, ["project", "obsidian", "--root", str(repository.root), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert tree_digest(repository.root) == before


# --- corroboration ----------------------------------------------------------------


def test_corroboration_count_only_with_a_projection(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    plain = tmp_path / "plain"
    ObsidianProjector().build(repository.root, plain)
    assert "corroboration" not in _blob(plain)
    seen: list[list[str]] = []

    def lookup(ids: object) -> dict[str, int]:
        seen.append(list(ids))  # type: ignore[call-overload]
        return {"m-pub": 3}

    counted = tmp_path / "counted"
    ObsidianProjector(corroboration=lookup).build(repository.root, counted)
    front = yaml.safe_load((counted / "memories" / "m-pub.md").read_text().split("---\n")[1])
    assert front["corroboration"] == {"count": 3}
    assert "m-priv" not in seen[0]  # only exported ids are looked up


def test_cli_reads_the_u4_projection_when_present(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    runner = CliRunner()
    assert (
        runner.invoke(app, ["corroboration", "rebuild", "--root", str(repository.root)]).exit_code
        == 0
    )
    out = tmp_path / "board"
    result = runner.invoke(
        app, ["project", "obsidian", "--root", str(repository.root), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    front = yaml.safe_load((out / "memories" / "m-pub.md").read_text().split("---\n")[1])
    assert front["corroboration"] == {"count": 0}


# --- shape ------------------------------------------------------------------------


def test_pages_and_bases_shape(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    out = tmp_path / "board"
    ObsidianProjector().build(repository.root, out)
    text = (out / "memories" / "m-pub.md").read_text()
    front = yaml.safe_load(text.split("---\n")[1])
    assert front["generated"] == "memfw-obsidian"
    for key in (
        "id",
        "type",
        "epistemic",
        "status",
        "privacy",
        "tenant",
        "valid_from",
        "valid_to",
        "tags",
    ):
        assert key in front
    assert front["privacy"] == "public" and front["epistemic"] == "fact"
    assert front["tags"] == ["ops/backup"]  # join and privacy markers left out
    assert "will be overwritten" in text and "nightly backup to the NAS" in text
    new = yaml.safe_load((out / "memories" / "m-new.md").read_text().split("---\n")[1])
    old = yaml.safe_load((out / "memories" / "m-shared.md").read_text().split("---\n")[1])
    assert new["supersedes"] == ["[[memories/m-shared]]"]
    assert old["superseded_by"] == "[[memories/m-new]]"
    question = yaml.safe_load((out / "questions" / "q-1.md").read_text().split("---\n")[1])
    assert question["open"] is True and question["conflicts"] == ["[[memories/m-pub]]"]
    for name in ("memories", "questions", "disputed"):
        raw = (out / "boards" / f"{name}.base").read_text()
        assert raw.startswith("# generated: memfw-obsidian")
        base = yaml.safe_load(raw)
        assert set(base) == {"filters", "views"}
        assert all(view["type"] == "table" for view in base["views"])


# --- default off --------------------------------------------------------------------


def test_default_off(tmp_path: Path) -> None:
    from memfw.ports import default_registry
    from memfw.ports.reference import reference_specs

    assert "obsidian" not in {spec.name for spec in reference_specs()}
    assert default_registry().get("obsidian") is None
    result = CliRunner().invoke(
        app, ["project", "obsidian", "--out", str(tmp_path / "o"), "--clearance", "nope"]
    )
    assert result.exit_code == 2
    assert not (tmp_path / "o").exists()
