"""The PageIndex recall adapter (``packages/adapters/memfw-adapter-pageindex``).

No network and no model: the PDF tree is a RECORDED PageIndex 0.7.0 output
(``tests/fixtures/pageindex/pdf_tree_v0.7.0.json``, synthetic content) and the
model is a recorded reply (``tree_search_reply.json``) served by a fake
backend. The ``BODY-MARKER-*`` strings in the tree stand for body text: they
must never reach a stored tree, a model request or a candidate.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from memfw.ports import (
    AdapterRegistry,
    AdaptersConfig,
    BackendBudgetExhausted,
    ModelReply,
    ProviderUnavailable,
    RecallBudget,
)
from memfw.ports.testing import check_recall_provider

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "packages" / "adapters" / "memfw-adapter-pageindex"
ADAPTER_SRC = ADAPTER / "src"
if str(ADAPTER_SRC) not in sys.path:  # a workspace member, not installed here
    sys.path.insert(0, str(ADAPTER_SRC))

from memfw_adapter_pageindex import (  # noqa: E402
    TASK,
    PageIndexProvider,
    Settings,
    SettingsError,
    TreeError,
    adapter_spec,
    build_markdown_tree,
    factory,
    import_pageindex_tree,
    load_index,
    write_tree,
)
from memfw_adapter_pageindex.cli import main as cli_main  # noqa: E402
from memfw_adapter_pageindex.tree import encode_part, parse_source  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "pageindex"
PDF_TREE = json.loads((FIXTURES / "pdf_tree_v0.7.0.json").read_text(encoding="utf-8"))
REPLY = (FIXTURES / "tree_search_reply.json").read_text(encoding="utf-8")
SHA = "ab" * 32
ATT = "att-01a1e931-1818-772b-8713-76e6bea4a716"
NOTE = """---
title: Field notes
privacy: shared
---
# Field notes

Intro BODY-MARKER-intro text.

## Sensor drift checks

BODY-MARKER-drift-note body.

```
# not a heading inside code
```

## Zero offset and calibration

### Sub point
"""


class RecordedBackend:
    """A ModelBackend that answers every call with one recorded reply."""

    def __init__(self, reply: str = REPLY, *, local: bool = True, tasks: Any = None) -> None:
        self.name = "recorded"
        self.local = local
        if tasks is not None:
            self.tasks = tasks
        self.reply = reply
        self.calls: list[tuple[str, dict[str, Any], int]] = []
        self.raise_exc: BaseException | None = None

    def run(self, task: str, data: dict[str, Any], *, max_tokens: int) -> ModelReply:
        self.calls.append((task, json.loads(json.dumps(data)), max_tokens))
        if self.raise_exc is not None:
            raise self.raise_exc
        return ModelReply(text=self.reply, tokens_in=100, tokens_out=20)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _kh(tmp_path: Path) -> Path:
    root = tmp_path / "kh"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "field notes.md").write_text(NOTE, encoding="utf-8")
    return root


def _index(tmp_path: Path, *, pdf_privacy: str = "shared", md_privacy: str = "shared") -> Path:
    index = tmp_path / "index"
    write_tree(
        index,
        import_pageindex_tree(
            PDF_TREE,
            f"att:{ATT}",
            pages=6,
            source_sha256=SHA,
            privacy=pdf_privacy,
            keep_summaries=True,
            pageindex_version="0.7.0",
        ),
    )
    write_tree(
        index,
        build_markdown_tree(_kh(tmp_path), "kh:notes/field notes.md", privacy=md_privacy),
    )
    return index


def _provider(index: Path, backend: Any = None, **settings: Any) -> PageIndexProvider:
    return PageIndexProvider(Settings(index_dir=str(index), **settings), backend=backend)


def _all_text(tree: Any) -> str:
    return json.dumps(tree, ensure_ascii=False)


# -- packaging and isolation ------------------------------------------------------------


def test_adapter_imports_only_ports_and_stdlib() -> None:
    allowed_roots = set(sys.stdlib_module_names) | {"memfw_adapter_pageindex", "yaml"}
    for path in (ADAPTER_SRC / "memfw_adapter_pageindex").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                if name.startswith(("memfw.", "memfw_")):
                    assert name.startswith(("memfw.ports", "memfw_ports")), (path.name, name)
                else:
                    assert name.split(".")[0] in allowed_roots, (path.name, name)


def test_memfw_never_imports_the_adapter_and_pins_the_entry_point() -> None:
    for path in (ROOT / "src").rglob("*.py"):
        assert "memfw_adapter_pageindex" not in path.read_text(encoding="utf-8"), path
    project = tomllib.loads((ADAPTER / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["dependencies"] == ["memfw-ports==0.3.0", "PyYAML==6.0.3"]
    points = project["project"]["entry-points"]["memfw.adapters"]
    assert points == {"pageindex": "memfw_adapter_pageindex:adapter_spec"}
    readme = (ADAPTER / "README.md").read_text(encoding="utf-8")
    assert "MIT" in readme and "0.7.0" in readme


def test_registry_keeps_it_off_until_enabled(tmp_path: Path) -> None:
    index = _index(tmp_path)
    registry = AdapterRegistry()
    registry.register(adapter_spec())
    assert registry.load("pageindex", AdaptersConfig()).reason == "disabled"
    config = AdaptersConfig.from_mapping(
        {"enabled": ["pageindex"], "pageindex": {"index_dir": str(index), "mode": "titles"}}
    )
    status = registry.load("pageindex", config)
    assert status.available and isinstance(status.adapter, PageIndexProvider)
    # reasoning without a backend: enabled but unavailable, never a crash
    config = AdaptersConfig.from_mapping(
        {"enabled": ["pageindex"], "pageindex": {"index_dir": str(index)}}
    )
    assert registry.load("pageindex", config).reason == "no_backend"


def test_factory_accepts_the_runtime_loader_keys(tmp_path: Path) -> None:
    # memfw's runtime recall loader (runtime.recall.adapters) adds vault and embedder
    index = _index(tmp_path)
    provider = factory(
        {
            "index_dir": str(index),
            "mode": "titles",
            "vault": str(tmp_path / "vault"),
            "embedder": None,
        }
    )
    assert provider.available().available


def test_index_dir_may_not_be_inside_the_vault_and_is_kept_private(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    for inside in (vault / "pageindex", vault, tmp_path):
        with pytest.raises(SettingsError, match="index_dir_in_vault"):
            factory({"index_dir": str(inside), "mode": "titles", "vault": str(vault)})
    index = _index(tmp_path)
    os.chmod(index, 0o755)
    assert load_index(index).trees
    assert os.stat(index).st_mode & 0o077 == 0


def test_settings_refuse_keys_relative_dirs_and_unknown_options(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="key_in_config"):
        factory({"index_dir": str(tmp_path), "api_key": "x"})
    with pytest.raises(SettingsError, match="key_in_config"):
        factory({"index_dir": str(tmp_path), "backend_options": {"token": "x"}})
    with pytest.raises(SettingsError, match="index_dir_not_absolute"):
        factory({"index_dir": "relative/dir"})
    with pytest.raises(SettingsError, match="unknown_setting"):
        factory({"index_dir": str(tmp_path), "endpoint": "http://example.invalid"})
    with pytest.raises(SettingsError, match="bad_backend_spec"):
        factory({"index_dir": str(tmp_path), "backend": "not a spec"})


def test_factory_loads_a_backend_from_module_factory(tmp_path: Path) -> None:
    index = _index(tmp_path)
    module = tmp_path / "pi_test_backend.py"
    module.write_text(
        "from memfw.ports import ModelReply\n"
        "class B:\n"
        "    name = 'b'\n"
        "    local = True\n"
        "    def __init__(self, options): self.options = options\n"
        "    def run(self, task, data, *, max_tokens):\n"
        '        return ModelReply(text=\'{"node_ids": ["d1:0002"]}\')\n'
        "def factory(options): return B(options)\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        provider = factory(
            {"index_dir": str(index), "backend": "pi_test_backend:factory", "backend_options": {}}
        )
    finally:
        sys.path.remove(str(tmp_path))
    assert provider.available().available
    (hit,) = provider.search("temperature", limit=3, budget=RecallBudget(), tenant="", as_of=None)
    assert hit.ref == f"att:{ATT}#page=3"


# -- trees --------------------------------------------------------------------------


def test_markdown_tree_has_headings_only_and_strictest_privacy(tmp_path: Path) -> None:
    root = _kh(tmp_path)
    tree = build_markdown_tree(root, "kh:notes/field notes.md", privacy="public")
    assert tree["privacy"] == "shared"  # the note's frontmatter is stricter
    titles = [n["title"] for n in tree["structure"][0]["nodes"]]
    assert titles == ["Sensor drift checks", "Zero offset and calibration"]
    assert tree["structure"][0]["nodes"][1]["nodes"][0]["title"] == "Sub point"
    assert "BODY-MARKER" not in _all_text(tree)
    assert "not a heading" not in _all_text(tree)
    assert build_markdown_tree(root, "kh:notes/field notes.md")["privacy"] == "local-private"


#: Frontmatter forms a private note may take. Each must come out local-private,
#: exactly as the kernel's yaml.safe_load + metadata_level reads it.
PRIVATE_FORMS = [
    "tags: [privacy:local-private]\n",
    "tags:\n  - project:x\n  - privacy:local-private\n",
    "tags:\n- privacy:local-private\n",  # unindented block list (yaml.safe_dump's style)
    "tags: ['sensitivity:private']\n",
    "sensitivity: private\n",
    "sensitivity: [private]\n",
    "sensitivity: confidential\n",  # unknown: fail closed
    '"sensitivity": private\n',
    '"tags": [privacy:local-private]\n',
    "'privacy': local-private\n",
    "{privacy: local-private}\n",  # a flow mapping as the whole frontmatter
    "? privacy\n: local-private\n",  # a complex key
    "base: &b {privacy: local-private}\n<<: *b\n",  # a merge key
    "<<: {sensitivity: private}\n",
    "privacy: Local-Private\n",
    "privacy: secret\n",
    "privacy: !!str local-private\n",
    "privacy: |\n  local-private\n",
    "privacy: shared\nprivacy: local-private\n",  # a repeated key: the last one wins
]
#: Variants of one key (after the bm25s adapter's legacy_privacy_default cases).
VARIANT_FORMS = [
    "privacy: public\n",
    "privacy: 'shared'\n",
    "privacy: public\n  local-private\n",  # a multi-line plain scalar
    "privacy: public\nprivacy: local-private\n",
    "x: [a,\nprivacy: public]\n",  # inside a flow sequence
    "privacy: |\n  public\n",
    "privacy: public # note\n",
    "privacy: PUBLIC\n",
    "privacy: [public]\n",
    "privacy: !!str public\n",
    "privacy: public\n---\nother: 1\n",  # a second document ends the frontmatter
    "{privacy: public}\n",
    "",
]
#: Not readable as a mapping with list tags: local-private, whatever the kernel would do.
UNREADABLE_FORMS = [
    "tags: {privacy: shared}\n",
    "tags: privacy:shared\n",
    "privacy: 'shared\n",
    "privacy: [unclosed\n",
    "- just\n- a list\n",
    "just a string\n",
    "1: x\n",
    "a: *undefined\n",
]


def _kernel_level(frontmatter: str, operator: str) -> str:
    """What memfw itself makes of this frontmatter (yaml.safe_load + metadata_level)."""

    import yaml

    from memfw.ports.privacy import metadata_level, strictest

    block = frontmatter.split("\n---\n", 1)[0]
    meta = yaml.safe_load(block) if block.strip() else None
    level = strictest([operator, metadata_level(meta or {}, legacy=operator)])
    return level.replace("unknown", "local-private")


def _note(tmp_path: Path, frontmatter: str, name: str = "n.md") -> Path:
    root = tmp_path / "kh"
    root.mkdir(exist_ok=True)
    (root / name).write_text(f"---\n{frontmatter}---\n# Heading\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("frontmatter", PRIVATE_FORMS)
def test_private_forms_are_local_private_like_the_kernel(tmp_path: Path, frontmatter: str) -> None:
    tree = build_markdown_tree(_note(tmp_path, frontmatter), "kh:n.md", privacy="public")
    assert tree["privacy"] == "local-private"
    assert _kernel_level(frontmatter, "public") == "local-private"


@pytest.mark.parametrize("frontmatter", VARIANT_FORMS)
def test_variants_match_the_kernel_reader(tmp_path: Path, frontmatter: str) -> None:
    import yaml

    root = tmp_path / "kh"
    root.mkdir()
    (root / "n.md").write_text(f"---\n{frontmatter}---\n# Heading\n", encoding="utf-8")
    try:
        expected = _kernel_level(frontmatter, "shared")
    except yaml.YAMLError:
        expected = "local-private"
    tree = build_markdown_tree(root, "kh:n.md", privacy="shared")
    assert tree["privacy"] == expected
    # the note can only tighten the operator's level, never loosen it
    assert tree["privacy"] in ("shared", "local-private")


@pytest.mark.parametrize("frontmatter", UNREADABLE_FORMS)
def test_unreadable_frontmatter_fails_closed(tmp_path: Path, frontmatter: str) -> None:
    tree = build_markdown_tree(_note(tmp_path, frontmatter), "kh:n.md", privacy="public")
    assert tree["privacy"] == "local-private"
    assert [n["title"] for n in tree["structure"]] == ["Heading"]


def test_unclosed_frontmatter_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "kh"
    root.mkdir()
    (root / "n.md").write_text("---\ntitle: x\n# Heading\n", encoding="utf-8")
    assert build_markdown_tree(root, "kh:n.md", privacy="public")["privacy"] == "local-private"


def test_notes_without_frontmatter_take_the_operators_level(tmp_path: Path) -> None:
    root = tmp_path / "kh"
    root.mkdir()
    (root / "n.md").write_text("# Heading\n", encoding="utf-8")
    assert build_markdown_tree(root, "kh:n.md", privacy="shared")["privacy"] == "shared"


def test_private_forms_never_reach_a_cloud_backend(tmp_path: Path) -> None:
    index = tmp_path / "index"
    root = tmp_path / "kh"
    root.mkdir()
    for number, frontmatter in enumerate(PRIVATE_FORMS + UNREADABLE_FORMS):
        (root / f"p{number}.md").write_text(
            f"---\n{frontmatter}---\n# Drift secret {number}\n", encoding="utf-8"
        )
        write_tree(index, build_markdown_tree(root, f"kh:p{number}.md", privacy="shared"))
    cloud = RecordedBackend('{"node_ids": []}', local=False)
    provider = _provider(index, cloud, cloud=True)
    assert provider.search("drift", limit=3, budget=RecallBudget(), tenant="", as_of=None) == []
    assert cloud.calls == []  # nothing sendable: no request at all
    (root / "open.md").write_text("---\nprivacy: shared\n---\n# Drift open\n", encoding="utf-8")
    write_tree(index, build_markdown_tree(root, "kh:open.md", privacy="shared"))
    provider.search("drift", limit=3, budget=RecallBudget(), tenant="", as_of=None)
    (call,) = cloud.calls
    sent = json.dumps(call[1], ensure_ascii=False)
    assert "Drift open" in sent and "secret" not in sent


# The reviewer's frontmatter sweep (re-review of #85): every form is checked
# against memfw's own notes split (memfw.notes.index._split_frontmatter) plus
# metadata_level; the adapter must never come out less private.
_LP = "privacy: local-private\n"
_SWEEP_FM = {
    "noindent_list": "tags:\n- privacy:local-private\n",
    "dq_key": '"privacy": local-private\n',
    "sq_key": "'privacy': local-private\n",
    "esc_x79": '"tags": ["privac\\x79:local-private"]\n',
    "esc_key_x79": '"privac\\x79": local-private\n',
    "esc_key_u": '"sensitivit\\u0079": private\n',
    "flow_map": "{privacy: local-private}\n",
    "flow_map_ml": "{tags: [a,\n privacy:local-private]}\n",
    "qkey": "? privacy\n: local-private\n",
    "merge_inline": "<<: {privacy: local-private}\n",
    "merge_anchor": "base: &b {privacy: local-private}\n<<: *b\n",
    "merge_list": "a: &a {x: 1}\nb: &b {sensitivity: private}\n<<: [*a, *b]\n",
    "str_tag_key": "!!str privacy: local-private\n",
    "str_tag_val": "privacy: !!str local-private\n",
    "sens_str": "sensitivity: private\n",
    "sens_upper": "sensitivity: PRIVATE\n",
    "sens_list": "sensitivity: [public, private]\n",
    "sens_map": "sensitivity: {a: b}\n",
    "sens_int": "sensitivity: 3\n",
    "tag_upper_priv": "tags: [PRIVACY:LOCAL-PRIVATE]\n",
    "tag_mixed_sens": "tags: [Sensitivity:Private]\n",
    "tag_priv_unknown": "tags: [privacy:secret]\n",
    "privacy_upper": "privacy: LOCAL-PRIVATE\n",
    "privacy_bogus": "privacy: secret\n",
    "privacy_int": "privacy: 1\n",
    "privacy_list": "privacy: [public]\n",
    "dup_keys": "privacy: local-private\nprivacy: public\n",
    "dup_keys2": "privacy: public\nprivacy: local-private\n",
    "dup_tags": "tags: [privacy:local-private]\ntags: [x]\n",
    "empty_privacy": "privacy:\n",
    "null_privacy": "privacy: null\n",
    "tilde_privacy": "privacy: ~\n",
    "empty_tags": "tags:\n",
    "null_sens": "sensitivity: null\n",
    "tags_scalar": "tags: privacy:local-private\n",
    "tags_map": "tags: {privacy: local-private}\n",
    "tags_nested": "tags: [[privacy:local-private]]\n",
    "tags_int_item": "tags: [1, privacy:local-private]\n",
    "non_mapping_list": "- privacy: local-private\n",
    "non_mapping_scalar": "local-private\n",
    "yaml_error": "privacy: [local-private\n",
    "int_key": "1: x\nprivacy: local-private\n",
    "multi_doc_dashes_inner": "privacy: public\n--- \nprivacy: local-private\n",
    "block_scalar_dots": "desc: |\n  ...\nprivacy: local-private\n",
    "block_scalar_dots2": "desc: >\n  ...\nsensitivity: private\n",
    "quoted_dots_ml": 'desc: "a\n ..."\nprivacy: local-private\n',
    "doc_end_then_more": "privacy: public\n...\ntags: [privacy:local-private]\n",
    "deep_nest": "x: " + "[" * 200 + "]" * 200 + "\n" + _LP,
}

SWEEP_CASES = {k: f"---\n{v}---\n# Secret heading\n" for k, v in _SWEEP_FM.items()}
SWEEP_CASES.update(
    {
        "bom": "﻿---\n" + _LP + "---\n# Secret heading\n",
        "crlf": "---\r\n" + _LP.replace("\n", "\r\n") + "---\r\n# Secret heading\r\n",
        "crlf_list": "---\r\ntags:\r\n- privacy:local-private\r\n---\r\n# Secret\r\n",
        "cr_only": "---\r" + _LP.replace("\n", "\r") + "---\r# Secret heading\r",
        "unterminated": "---\n" + _LP + "# Secret heading\n",
        "dots_term": "---\n" + _LP + "...\n# Secret heading\n",
        "dots_term_public_then": "---\nprivacy: public\n...\n" + _LP + "---\n# Secret\n",
        "open_trailing_ws": "--- \n" + _LP + "---\n# Secret\n",
        "open_with_comment": "--- # c\n" + _LP + "---\n# Secret\n",
        "leading_blank": "\n---\n" + _LP + "---\n# Secret\n",
        "empty_block": "---\n---\n# Secret\n",
        "ls_sep": "--- " + _LP + "---\n# Secret\n",
        "nel_sep": "---\x85privacy: local-private\x85---\n# Secret\n",
        "vt_ff": "---\x0c\n" + _LP + "---\n# Secret\n",
        "close_indented": "---\n" + _LP + "  ---\n# Secret\n",
    }
)


@pytest.mark.parametrize("operator", ["public", "shared"])
@pytest.mark.parametrize("name", sorted(SWEEP_CASES))
def test_sweep_matches_the_notes_split_and_is_never_less_private(operator: str, name: str) -> None:
    from memfw_adapter_pageindex.tree import _frontmatter_block, note_level

    from memfw.notes.index import NoteParseError, _split_frontmatter
    from memfw.ports.privacy import metadata_level, strictest

    text = SWEEP_CASES[name]
    rank = {"public": 0, "shared": 1, "local-private": 2}
    try:
        meta, _, body_line = _split_frontmatter(text)
        kernel = strictest([operator, metadata_level(meta, legacy=operator)])
    except (NoteParseError, RecursionError):
        kernel, body_line = "local-private", None
    kernel = kernel if kernel in rank else "local-private"
    assert rank[note_level(text, operator)] >= rank[kernel], (name, operator)
    _, start, malformed = _frontmatter_block(text)
    if body_line is not None and not malformed:
        assert start == body_line, name  # the same block as the notes index


DOTS_LEAKS = {
    "blockdots.md": "---\ndesc: |\n  ...\nprivacy: local-private\n---\n# S-blockdots LEAK\n",
    "blockdots2.md": "---\ndesc: >\n  ...\nsensitivity: private\n---\n# S-blockdots2 LEAK\n",
    "docend.md": "---\nprivacy: public\n...\ntags: [privacy:local-private]\n---\n# S-docend LEAK\n",
}


@pytest.mark.parametrize("operator", ["public", "shared"])
def test_dot_lines_do_not_end_the_frontmatter(tmp_path: Path, operator: str) -> None:
    root = tmp_path / "kh"
    root.mkdir()
    index = tmp_path / "index"
    for name, text in DOTS_LEAKS.items():
        (root / name).write_text(text, encoding="utf-8")
        tree = build_markdown_tree(root, f"kh:{name}", privacy=operator)
        assert tree["privacy"] == "local-private", name
        write_tree(index, tree)
    (root / "open.md").write_text("---\nprivacy: public\n---\n# S-control\n", encoding="utf-8")
    write_tree(index, build_markdown_tree(root, "kh:open.md", privacy=operator))
    cloud = RecordedBackend('{"node_ids": []}', local=False)
    provider = _provider(index, cloud, cloud=True)
    provider.search("s", limit=5, budget=RecallBudget(), tenant="", as_of=None)
    sent = [n["title"] for c in cloud.calls for d in c[1]["documents"] for n in d["nodes"]]
    assert sent == ["S-control"]


def test_markdown_paths_are_confined(tmp_path: Path) -> None:
    root = _kh(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("# Secret\n", encoding="utf-8")
    (root / "notes" / "link.md").symlink_to(outside)
    for ref, code in (
        ("kh:notes/link.md", "symlink_refused"),
        ("kh:../outside.md", "bad_source"),
        ("kh:/etc/x.md", "bad_source"),
        ("kh:notes/missing.md", "source_missing"),
        ("file:notes/field notes.md", "bad_source"),
    ):
        with pytest.raises(TreeError) as raised:
            build_markdown_tree(root, ref)
        assert raised.value.code == code, ref


def test_import_drops_text_and_keeps_summaries_only_on_request() -> None:
    plain = import_pageindex_tree(PDF_TREE, f"att:{ATT}", pages=6, source_sha256=SHA)
    assert "BODY-MARKER" not in _all_text(plain)
    assert "summary" not in _all_text(plain)
    assert "doc_description" not in plain and "synthetic-handbook" not in _all_text(plain)
    kept = import_pageindex_tree(
        PDF_TREE, f"att:{ATT}", pages=6, source_sha256=SHA, keep_summaries=True
    )
    assert "Drift grows" in _all_text(kept) and "BODY-MARKER" not in _all_text(kept)
    assert kept["privacy"] == "local-private"  # the default


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda t: t["structure"][0].update(end_index=9), "page_out_of_range"),
        (lambda t: t["structure"][0].update(start_index=0), "page_out_of_range"),
        (lambda t: t["structure"][1].update(node_id="0000"), "bad_node_id"),
        (lambda t: t["structure"][0].update(title=""), "bad_node"),
        (lambda t: t["structure"][0].update(start_index="2"), "bad_page"),
    ],
)
def test_import_refuses_a_bad_tree(mutate: Any, code: str) -> None:
    tree = json.loads(json.dumps(PDF_TREE))
    mutate(tree)
    with pytest.raises(TreeError) as raised:
        import_pageindex_tree(tree, f"att:{ATT}", pages=6, source_sha256=SHA)
    assert raised.value.code == code


def test_index_skips_tampered_files(tmp_path: Path) -> None:
    index = _index(tmp_path)
    assert len(load_index(index).trees) == 2
    for path in index.glob("*.json"):
        assert os.stat(path).st_mode & 0o077 == 0
    bad = json.loads(next(index.glob("*.json")).read_text(encoding="utf-8"))
    (index / "junk.json").write_text("{}", encoding="utf-8")
    bad["structure"][0]["text"] = "BODY"
    (index / ("0" * 32 + ".json")).write_text(json.dumps(bad), encoding="utf-8")
    loaded = load_index(index)
    assert len(loaded.trees) == 2
    assert dict(loaded.skipped) == {"bad_schema": 1, "bad_node": 1}


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda t: t.update(extra="x"), "unknown_key"),
        (lambda t: t["structure"][0].update(text="BODY"), "bad_node"),
        (lambda t: t["structure"][0].update(node_id="bad id!"), "bad_node_id"),
        (lambda t: t["structure"][1].update(node_id=t["structure"][0]["node_id"]), "bad_node_id"),
        (lambda t: t["structure"][0].update(summary="x" * 301), "bad_summary"),
        (lambda t: t["structure"][0].update(title="  padded  "), "bad_node"),
        (lambda t: t["structure"][0].update(end_index=7), "page_out_of_range"),
        (lambda t: t.update(producer={"tool": "x"}), "bad_producer"),
        (lambda t: t.update(pages=True), "bad_pages"),
    ],
)
def test_load_applies_the_import_rules(mutate: Any, code: str) -> None:
    from memfw_adapter_pageindex.tree import validate_tree

    tree = import_pageindex_tree(
        PDF_TREE, f"att:{ATT}", pages=6, source_sha256=SHA, keep_summaries=True
    )
    validate_tree(json.loads(json.dumps(tree)))
    bad = json.loads(json.dumps(tree))
    mutate(bad)
    with pytest.raises(TreeError) as raised:
        validate_tree(bad)
    assert raised.value.code == code


def test_load_checks_markdown_line_numbers(tmp_path: Path) -> None:
    from memfw_adapter_pageindex.tree import validate_tree

    tree = build_markdown_tree(_kh(tmp_path), "kh:notes/field notes.md", privacy="shared")
    tree["structure"][0]["line_num"] = 0
    with pytest.raises(TreeError, match="bad_line"):
        validate_tree(tree)


def test_deeply_nested_json_is_skipped_not_raised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    index = _index(tmp_path)
    deep = "[" * 100_000 + "]" * 100_000
    (index / ("1" * 32 + ".json")).write_text(deep, encoding="utf-8")
    loaded = load_index(index)
    assert len(loaded.trees) == 2 and loaded.skipped == {"unreadable": 1}
    tree_file = tmp_path / "deep.json"
    tree_file.write_text('{"structure": ' + deep + "}", encoding="utf-8")
    argv = ["import-tree", "--source", f"att:{ATT}", "--tree", str(tree_file), "--pages", "6"]
    assert cli_main([*argv, "--sha256", SHA, "--index-dir", str(index)]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "unreadable"}


def test_long_titles_are_cut_on_character_boundaries() -> None:
    from urllib.parse import unquote

    from memfw_adapter_pageindex.tree import node_ref

    from memfw.ports.types import PATH_REF_RE

    source = parse_source("kh:" + "d" * 300 + "/note.md")
    title = "漂 移" * 100  # every character encodes to 3 or 9 bytes of %XX
    ref = node_ref(source, {"title": title})
    assert ref is not None and len(ref) <= 512 and PATH_REF_RE.fullmatch(ref)
    fragment = ref.split("#", 1)[1]
    decoded = unquote(fragment, errors="strict")
    assert decoded and title.startswith(decoded)
    assert node_ref(source, {"title": ""}) is None


def test_refs_are_valid_path_refs_and_encode_unsafe_characters() -> None:
    assert encode_part('a b"#%<>') == "a%20b%22%23%25%3C%3E"
    assert encode_part("中文 标题") == "中文%20标题"
    assert parse_source("kh:notes/a.pdf").media == "pdf"


# -- search -------------------------------------------------------------------------


def test_titles_mode_passes_the_contract_kit(tmp_path: Path) -> None:
    index = _index(tmp_path)
    provider = _provider(index, mode="titles")
    seen = check_recall_provider(
        provider,
        queries=["sensor drift", "zero offset", "nothing matches here"],
        limit=5,
        vault=index,
        tenant_of=lambda c: "",
    )
    refs = [c.ref for c in seen["sensor drift"]]
    assert refs[0] == "kh:notes/field%20notes.md#Sensor%20drift%20checks"
    assert f"att:{ATT}#page=3" in refs
    assert seen["nothing matches here"] == []
    for candidate in seen["sensor drift"]:
        assert set(candidate.extra) <= {"kind", "page", "why"}
        assert "BODY" not in candidate.ref


def test_reasoning_mode_follows_the_recorded_reply(tmp_path: Path) -> None:
    index = _index(tmp_path)
    backend = RecordedBackend()
    provider = _provider(index, backend)
    seen = check_recall_provider(provider, queries=["temperature drift"], limit=5)
    refs = [(c.ref, c.extra.get("page")) for c in seen["temperature drift"]]
    # the unknown id and the repeat are dropped; the model's order is kept
    assert refs == [(f"att:{ATT}#page=3", 3), (f"att:{ATT}#page=2", 2)]
    task, data, max_tokens = backend.calls[0]
    assert task == TASK and max_tokens == 256
    request = json.dumps(data, ensure_ascii=False)
    assert "BODY-MARKER" not in request
    assert data["query"] == "temperature drift" and data["max_nodes"] == 5
    assert [d["doc"] for d in data["documents"]] == ["d1", "d2"]
    assert data["documents"][0]["nodes"][2] == {
        "id": "d1:0002",
        "depth": 1,
        "title": "Temperature drift",
        "pages": "3-3",
        "summary": "Drift grows with ambient temperature.",
    }


def test_prompt_cap_holds_for_cjk_titles(tmp_path: Path) -> None:
    from memfw_adapter_pageindex import render_tree_search_prompt
    from memfw_adapter_pageindex.provider import estimate_tokens

    root = tmp_path / "kh"
    root.mkdir()
    headings = "".join(f"## 传感器漂移校准第{i}节的详细说明与注意事项<>\n" for i in range(200))
    (root / "cjk.md").write_text("# 总览\n" + headings, encoding="utf-8")
    index = tmp_path / "index"
    write_tree(index, build_markdown_tree(root, "kh:cjk.md", privacy="shared"))
    backend = RecordedBackend('{"node_ids": ["d1:0002"]}')
    provider = _provider(index, backend, max_prompt_tokens=1000)
    hits = provider.search("漂移", limit=3, budget=RecallBudget(), tenant="", as_of=None)
    assert [h.extra.get("why") for h in hits] == ["tree_search"]
    (_, data, _) = backend.calls[0]
    system, user = render_tree_search_prompt(data)
    assert estimate_tokens(system + user) <= 1000
    assert 0 < len(data["documents"][0]["nodes"]) < 201
    tiny = _provider(index, RecordedBackend(), max_prompt_tokens=10)
    with pytest.raises(ProviderUnavailable):
        tiny.search("漂移", limit=3, budget=RecallBudget(), tenant="", as_of=None)


def test_heading_refs_parse_with_the_kernel_kh_grammar(tmp_path: Path) -> None:
    from memfw.recall.resolve import parse_kh_ref

    tree = build_markdown_tree(_kh(tmp_path), "kh:notes/field notes.md", privacy="shared")
    provider = _provider(tmp_path / "index", mode="titles")
    write_tree(tmp_path / "index", tree)
    (hit,) = provider.search(
        "sensor drift checks", limit=1, budget=RecallBudget(), tenant="", as_of=None
    )
    assert parse_kh_ref(hit.ref) == ("notes/field notes.md", "Sensor drift checks")


def test_coordinator_drops_every_candidate_as_unresolved_today(tmp_path: Path) -> None:
    # The default resolvers register no scheme resolver (kh: needs a registered
    # NotesSegmentResolver, att: waits for MM2b), so every PageIndex candidate is
    # dropped as unresolved, without error.
    from memfw.core.repository import Repository
    from memfw.recall import Coordinator, FilterPolicy, ProviderConfig
    from memfw.recall.builtin import default_resolvers

    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Public", "public body", memory_id="pub")
    provider = _provider(_index(tmp_path), mode="titles")
    coordinator = Coordinator(
        [provider],
        resolvers=default_resolvers(repository.root),
        configs={"pageindex": ProviderConfig(budget=RecallBudget(timeout_seconds=5))},
    )
    result = coordinator.search("sensor drift", limit=5, policy=FilterPolicy())
    assert result.ids == []
    assert result.metrics.dropped == {"unresolved": 3}
    assert dict(result.labels.degraded) == {} and "pageindex" in result.labels.providers


def test_tenant_is_exact(tmp_path: Path) -> None:
    index = _index(tmp_path)
    provider = _provider(index, RecordedBackend())
    assert provider.search("drift", limit=5, budget=RecallBudget(), tenant="t2", as_of=None) == []
    assert RecordedBackend().calls == []


def test_cloud_backend_needs_cloud_and_never_sees_local_private(tmp_path: Path) -> None:
    index = _index(tmp_path, pdf_privacy="local-private", md_privacy="shared")
    cloud = RecordedBackend('{"node_ids": ["d1:0002"]}', local=False)
    assert _provider(index, cloud).available().reason == "cloud_not_allowed"
    with pytest.raises(ProviderUnavailable):
        _provider(index, cloud).search("x", limit=3, budget=RecallBudget(), tenant="", as_of=None)
    assert cloud.calls == []
    provider = _provider(index, cloud, cloud=True)
    assert provider.available().available
    hits = provider.search("drift", limit=3, budget=RecallBudget(), tenant="", as_of=None)
    (call,) = cloud.calls
    sent = json.dumps(call[1], ensure_ascii=False)
    assert "Temperature drift" not in sent and "Calibration" not in sent  # the PDF stayed home
    assert "Sensor drift checks" in sent
    assert [h.ref for h in hits] == ["kh:notes/field%20notes.md#Sensor%20drift%20checks"]
    # a local backend may see local-private trees
    local = RecordedBackend()
    _provider(index, local).search("drift", limit=3, budget=RecallBudget(), tenant="", as_of=None)
    assert "Temperature drift" in json.dumps(local.calls[0][1])


def test_backend_must_offer_the_task(tmp_path: Path) -> None:
    index = _index(tmp_path)
    tagger = RecordedBackend(tasks=("chunk_summary", "session_summary"))
    assert _provider(index, tagger).available().reason == "backend_task_unsupported"
    assert _provider(index, RecordedBackend(tasks=(TASK,))).available().available


def test_per_run_budget_stops_the_provider(tmp_path: Path) -> None:
    index = _index(tmp_path)
    backend = RecordedBackend()
    provider = _provider(index, backend, max_calls=1)
    budget = RecallBudget()
    assert provider.search("drift", limit=3, budget=budget, tenant="", as_of=None)
    with pytest.raises(ProviderUnavailable):
        provider.search("drift", limit=3, budget=budget, tenant="", as_of=None)
    assert len(backend.calls) == 1
    assert provider.available().reason == "budget_spent"
    tiny = _provider(index, RecordedBackend(), max_run_tokens=10)
    with pytest.raises(ProviderUnavailable):
        tiny.search("drift", limit=3, budget=budget, tenant="", as_of=None)
    spent = RecordedBackend()
    spent.raise_exc = BackendBudgetExhausted()
    exhausted = _provider(index, spent)
    with pytest.raises(ProviderUnavailable):
        exhausted.search("drift", limit=3, budget=budget, tenant="", as_of=None)
    assert exhausted.available().reason == "budget_spent"


def test_transient_backend_failure_degrades_one_search_only(tmp_path: Path) -> None:
    from memfw.ports import BackendUnavailable

    index = _index(tmp_path)
    backend = RecordedBackend()
    backend.raise_exc = BackendUnavailable("transient")
    provider = _provider(index, backend)
    budget = RecallBudget()
    with pytest.raises(ProviderUnavailable, match="backend_unavailable"):
        provider.search("drift", limit=3, budget=budget, tenant="", as_of=None)
    assert provider.available().available and provider.stopped == ""
    backend.raise_exc = None
    assert provider.search("drift", limit=3, budget=budget, tenant="", as_of=None)
    assert len(backend.calls) == 2


@pytest.mark.parametrize("reply", ["not json", '["d1:0002"]', '{"node_ids": "d1", "x": 1}'])
def test_malformed_reply_degrades(tmp_path: Path, reply: str) -> None:
    provider = _provider(_index(tmp_path), RecordedBackend(reply))
    with pytest.raises(ProviderUnavailable):
        provider.search("drift", limit=3, budget=RecallBudget(), tenant="", as_of=None)


def test_missing_index_is_unavailable(tmp_path: Path) -> None:
    provider = _provider(tmp_path / "nope", RecordedBackend())
    assert provider.available().reason == "no_index"


# -- CLI ----------------------------------------------------------------------------


def test_cli_builds_imports_and_lists_without_titles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _kh(tmp_path)
    index = tmp_path / "cli-index"
    tree_file = tmp_path / "tree.json"
    tree_file.write_text(json.dumps(PDF_TREE), encoding="utf-8")
    assert (
        cli_main(
            [
                "build-md",
                "--root",
                str(root),
                "--source",
                "kh:notes/field notes.md",
                "--index-dir",
                str(index),
                "--privacy",
                "shared",
            ]
        )
        == 0
    )
    assert (
        cli_main(
            [
                "import-tree",
                "--source",
                f"att:{ATT}",
                "--tree",
                str(tree_file),
                "--pages",
                "6",
                "--sha256",
                SHA,
                "--index-dir",
                str(index),
            ]
        )
        == 0
    )
    assert cli_main(["list", "--index-dir", str(index)]) == 0
    out = capsys.readouterr().out
    rows: list[Mapping[str, Any]] = [json.loads(line) for line in out.splitlines()]
    assert {row["source"] for row in rows} == {"kh:notes/field notes.md", f"att:{ATT}"}
    assert "Sensor" not in out and "Calibration" not in out and "BODY" not in out
    assert (
        cli_main(
            [
                "import-tree",
                "--source",
                f"att:{ATT}",
                "--tree",
                str(tree_file),
                "--pages",
                "2",
                "--sha256",
                SHA,
                "--index-dir",
                str(index),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out.strip()) == {"error": "page_out_of_range"}
