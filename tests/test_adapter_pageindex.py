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
    allowed_roots = set(sys.stdlib_module_names) | {"memfw_adapter_pageindex"}
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
    assert project["project"]["dependencies"] == ["memfw-ports==0.3.0"]
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
        {"index_dir": str(index), "mode": "titles", "vault": str(tmp_path), "embedder": None}
    )
    assert provider.available().available


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


FRONTMATTER_CASES = [
    ("privacy: shared\n", "shared"),
    ("tags: [privacy:local-private]\n", "local-private"),
    ("tags:\n  - project:x\n  - privacy:local-private\n", "local-private"),
    ("tags: ['sensitivity:private']\n", "local-private"),
    ("sensitivity: private\n", "local-private"),
    ("sensitivity: [private]\n", "local-private"),
    ("sensitivity: confidential\n", "local-private"),  # unknown → fail closed
    ("tags: [privacy:fleet]\n", "shared"),
    ("tags: [privacy:none, note]\n", "shared"),
    ("privacy: public\n", "shared"),  # the note cannot loosen the operator's level
    ("privacy: Local-Private\n", "local-private"),
    ("privacy: secret\n", "local-private"),
    ("title: x\nprivacy:\n", "shared"),  # no value reads as absent, like the kernel
]


@pytest.mark.parametrize(("frontmatter", "expected"), FRONTMATTER_CASES)
def test_note_privacy_matches_the_kernel_reader(
    tmp_path: Path, frontmatter: str, expected: str
) -> None:
    import yaml

    from memfw.ports.privacy import metadata_level, strictest

    root = tmp_path / "kh"
    root.mkdir()
    (root / "n.md").write_text(f"---\n{frontmatter}---\n# Heading\n", encoding="utf-8")
    tree = build_markdown_tree(root, "kh:n.md", privacy="shared")
    assert tree["privacy"] == expected
    kernel = strictest(["shared", metadata_level(yaml.safe_load(frontmatter), legacy="shared")])
    assert kernel.replace("unknown", "local-private") == expected


@pytest.mark.parametrize(
    "frontmatter",
    [
        "tags: {privacy: shared}\n",
        "tags: privacy:shared\n",  # a scalar the kernel would ignore: fail closed
        "tags: &a [x]\n",
        "tags: [x, [y]]\n",
        "privacy: shared\nprivacy: public\n",
        "tags:\n  - x\n  other: y\n",
        "privacy: >\n  shared\n",
        "privacy: 'shared\n",
    ],
)
def test_unreadable_privacy_keys_fail_closed(tmp_path: Path, frontmatter: str) -> None:
    root = tmp_path / "kh"
    root.mkdir()
    (root / "n.md").write_text(f"---\n{frontmatter}---\n# Heading\n", encoding="utf-8")
    tree = build_markdown_tree(root, "kh:n.md", privacy="public")
    assert tree["privacy"] == "local-private"
    assert [n["title"] for n in tree["structure"]] == ["Heading"]


def test_tagged_private_note_never_reaches_a_cloud_backend(tmp_path: Path) -> None:
    root = tmp_path / "kh"
    root.mkdir()
    (root / "t.md").write_text(
        "---\ntags: [privacy:local-private]\n---\n# Drift secret plan\n", encoding="utf-8"
    )
    (root / "s.md").write_text("---\nsensitivity: private\n---\n# Drift other\n", encoding="utf-8")
    index = tmp_path / "index"
    for ref in ("kh:t.md", "kh:s.md"):
        write_tree(index, build_markdown_tree(root, ref, privacy="shared"))
    cloud = RecordedBackend('{"node_ids": []}', local=False)
    provider = _provider(index, cloud, cloud=True)
    assert provider.search("drift", limit=3, budget=RecallBudget(), tenant="", as_of=None) == []
    assert cloud.calls == []  # nothing sendable: no request at all


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
