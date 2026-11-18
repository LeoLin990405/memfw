"""The LangExtract Deriver adapter (``packages/adapters/memfw-adapter-langextract``).

No test here reaches the network or a model: LangExtract's own output is
replayed from ``tests/fixtures/langextract_contract/recorded.json``, which was
recorded by running ``run_langextract`` under the pinned LangExtract release
against a canned model (the prompts LangExtract built and the annotated
documents it returned). The backend is an in-process fake.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from memfw.ports import AdapterRegistry, AdaptersConfig, DerivedView, DeriveRequest
from memfw.ports.models import BackendBudgetExhausted, BackendUnavailable, ModelReply
from memfw.ports.staging import stage_candidates
from memfw.ports.testing import check_deriver, tree_digest

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "packages" / "adapters" / "memfw-adapter-langextract"
ADAPTER_SRC = ADAPTER / "src"
if str(ADAPTER_SRC) not in sys.path:  # the adapter is a workspace member, not installed here
    sys.path.insert(0, str(ADAPTER_SRC))

from memfw_adapter_langextract import (  # noqa: E402
    EXTERNAL_VERSION,
    SPEC,
    TASK,
    LangExtractDeriver,
    Settings,
    candidates,
    factory,
)
from memfw_adapter_langextract.deriver import (  # noqa: E402
    load_backend,
    rows_from_documents,
)
from memfw_adapter_langextract.ollama import (  # noqa: E402
    OllamaBackend,
    OllamaSettings,
    check_loopback_url,
    loopback_opener,
)

FIXTURE = ROOT / "tests" / "fixtures" / "langextract_contract" / "recorded.json"
RECORDED: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
TEXTS = dict((memory_id, text) for memory_id, text in RECORDED["documents"])
PRIVATE_MARKER = "walrus-private-marker-17"


def _memory(memory_id: str, body: str, *, privacy: str | None, tags: Sequence[str] = ()) -> str:
    lines = ["---", f"id: {memory_id}", f"title: {memory_id} title", "version: 3"]
    if privacy is not None:
        lines.append(f"privacy: {privacy}")
    lines += ["tags:", *[f"- {tag}" for tag in tags]] if tags else ["tags: []"]
    return "\n".join([*lines, "---", body, ""])


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "memories").mkdir(parents=True)
    (root / "vault.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    files = {
        "m-backup": _memory("m-backup", TEXTS["m-backup"], privacy="public"),
        "m-release": _memory("m-release", TEXTS["m-release"], privacy="shared"),
        "m-private": _memory("m-private", PRIVATE_MARKER, privacy="local-private"),
        "m-legacy": _memory("m-legacy", PRIVATE_MARKER, privacy=None),
        "m-tagged": _memory(
            "m-tagged", PRIVATE_MARKER, privacy="public", tags=["sensitivity:private"]
        ),
        "m-secret": _memory("m-secret", PRIVATE_MARKER, privacy="public", tags=["privacy:secret"]),
    }
    for memory_id, text in files.items():
        (root / "memories" / f"{memory_id}.md").write_text(text, encoding="utf-8")
    return root


ALL_IDS = ("m-backup", "m-private", "m-release", "m-legacy", "m-tagged", "m-secret", "m-none")


class FakeBackend:
    """A ``ModelBackend`` that answers each recorded prompt with its recorded reply."""

    tasks = (TASK,)

    def __init__(self, *, local: bool = True, tokens: int = 100) -> None:
        self.name = "fake:langextract"
        self.local = local
        self.tokens = tokens
        self.calls: list[tuple[str, dict[str, Any], int]] = []
        self.replies = {call["prompt"]: call["reply"] for call in RECORDED["calls"]}

    def run(self, task: str, data: dict[str, Any], *, max_tokens: int) -> ModelReply:
        self.calls.append((task, dict(data), max_tokens))
        return ModelReply(self.replies[data["prompt"]], self.tokens, self.tokens)


def replay_runner(
    seen: list[Sequence[tuple[str, str]]] | None = None,
) -> Callable[..., list[dict[str, Any]]]:
    """Stand-in for ``run_langextract``: sends the recorded prompts, returns the recording."""

    def run(
        documents: Sequence[tuple[str, str]], call: Callable[[str], str], buffer: int
    ) -> list[dict[str, Any]]:
        if seen is not None:
            seen.append(list(documents))
        assert buffer == 1000
        for recorded in RECORDED["calls"]:
            assert call(recorded["prompt"]) == recorded["reply"]
        return json.loads(json.dumps(RECORDED["annotated"]))

    return run


def make(**settings: Any) -> tuple[LangExtractDeriver, FakeBackend]:
    backend = FakeBackend()
    deriver = LangExtractDeriver(
        Settings.from_mapping(settings), backend=backend, runner=replay_runner()
    )
    return deriver, backend


def fake_backend_factory(options: dict[str, Any] | None = None) -> FakeBackend:
    return FakeBackend(**(options or {}))


def fake_untasked_factory() -> Any:
    class Untasked:
        name, local = "untasked", True

        def run(self, task: str, data: dict[str, Any], *, max_tokens: int) -> ModelReply:
            raise AssertionError("never called")

    return Untasked()


def fake_unavailable_factory() -> Any:
    raise BackendUnavailable("missing_key")


# -- the contract -----------------------------------------------------------------------------


def test_fixture_was_recorded_against_the_pinned_release() -> None:
    assert RECORDED["external"] == "langextract"
    assert RECORDED["external_version"] == EXTERNAL_VERSION == SPEC.version == "1.7.2"
    assert SPEC.kind == "deriver" and SPEC.license == "Apache-2.0"
    assert SPEC.requires == ("langextract",)


def test_passes_the_deriver_contract_and_cites_only_shareable_memories(vault: Path) -> None:
    deriver, backend = make()
    view = check_deriver(deriver, vault=vault, request=DeriveRequest("entities", ALL_IDS))
    assert view.cited_ids() == ("m-backup", "m-release")
    assert [c.version for c in view.citations] == [3, 3]
    assert view.epistemic_cap == "inference" and view.causal_cap == "none"
    assert view.read_only
    assert len(backend.calls) == 2 and {call[0] for call in backend.calls} == {TASK}


def test_view_rows_are_grounded_spans_of_the_memory_bodies(vault: Path) -> None:
    deriver, _ = make()
    view = deriver.derive(vault, DeriveRequest("entities", ("m-backup", "m-release")))
    payload = json.loads(view.content)
    assert payload["format"] == "memfw-langextract-v1"
    assert payload["external_version"] == EXTERNAL_VERSION
    rows = payload["extractions"]
    texts = [row["text"] for row in rows]
    assert "Kubernetes" not in texts  # not in the body: LangExtract could not ground it
    assert texts[:3] == ["backup job", "vault", "NAS"]
    for row in rows:
        span = TEXTS[row["memory_id"]][row["start"] : row["end"]]
        assert span.lower() == row["text"].lower() or row["alignment"] == "match_fuzzy"
    relation = next(row for row in rows if row["class"] == "relation")
    assert relation["attributes"] == {
        "subject": "backup job",
        "predicate": "depends on",
        "object": "cron",
    }


def test_view_is_deterministic(vault: Path) -> None:
    request = DeriveRequest("entities", ALL_IDS)
    first = make()[0].derive(vault, request)
    second = make()[0].derive(vault, request)
    assert first == second


@pytest.mark.parametrize("options", [{}, {"include_private": True}])
def test_private_legacy_and_unknown_memories_never_leave_the_vault(
    vault: Path, options: dict[str, Any]
) -> None:
    seen: list[Sequence[tuple[str, str]]] = []
    backend = FakeBackend()
    deriver = LangExtractDeriver(Settings(), backend=backend, runner=replay_runner(seen))
    deriver.derive(vault, DeriveRequest("entities", ALL_IDS, options))
    assert [memory_id for memory_id, _ in seen[0]] == ["m-backup", "m-release"]
    assert all(PRIVATE_MARKER not in json.dumps(call) for call in backend.calls)


def test_only_private_memories_is_an_error_and_sends_nothing(vault: Path) -> None:
    deriver, backend = make()
    with pytest.raises(ValueError, match="may leave the vault"):
        deriver.derive(vault, DeriveRequest("entities", ("m-private", "m-legacy", "m-tagged")))
    assert backend.calls == []


def test_unknown_kind_is_refused(vault: Path) -> None:
    deriver, backend = make()
    with pytest.raises(ValueError, match="kind"):
        deriver.derive(vault, DeriveRequest("timeline", ("m-backup",)))
    assert backend.calls == []


def test_body_cut_and_memory_cap_apply_before_anything_is_sent(vault: Path) -> None:
    seen: list[Sequence[tuple[str, str]]] = []
    deriver = LangExtractDeriver(
        Settings(max_chars=20, max_memories=1), backend=FakeBackend(), runner=replay_runner(seen)
    )
    with pytest.raises(ValueError, match="no grounded extraction"):
        # The recorded spans no longer fit a 20-character body: every row is dropped.
        deriver.derive(vault, DeriveRequest("entities", ALL_IDS))
    assert seen == [[("m-backup", TEXTS["m-backup"][:20])]]


# -- budget -------------------------------------------------------------------------------------


def test_budget_stops_before_the_first_call_that_could_overrun(vault: Path) -> None:
    deriver, backend = make(max_run_tokens=50)
    with pytest.raises(BackendBudgetExhausted):
        deriver.derive(vault, DeriveRequest("entities", ALL_IDS))
    assert backend.calls == []
    assert deriver.last_budget is not None and deriver.last_budget.stopped == "backend_tokens"


def test_budget_is_charged_with_reported_usage_and_resets_per_run(vault: Path) -> None:
    prompt = RECORDED["calls"][0]["prompt"]
    from memfw_adapter_langextract.deriver import estimate_tokens

    worst = estimate_tokens(prompt) + 1024
    # Room for the first call's worst case but not, after its charge, the second's.
    deriver, backend = make(max_run_tokens=worst + 150)
    with pytest.raises(BackendBudgetExhausted):
        deriver.derive(vault, DeriveRequest("entities", ALL_IDS))
    assert len(backend.calls) == 1
    assert deriver.last_budget is not None and deriver.last_budget.used == 200
    roomy, _ = make(max_run_tokens=100_000)
    roomy.derive(vault, DeriveRequest("entities", ALL_IDS))
    roomy.derive(vault, DeriveRequest("entities", ALL_IDS))
    assert roomy.last_budget is not None and roomy.last_budget.used == 400  # this run only


def test_a_spent_budget_is_never_a_partial_view(vault: Path) -> None:
    """LangExtract may swallow a model error per chunk; the run still fails."""

    def swallowing(
        documents: Sequence[tuple[str, str]], call: Callable[[str], str], buffer: int
    ) -> list[dict[str, Any]]:
        for recorded in RECORDED["calls"]:
            try:
                call(recorded["prompt"])
            except BackendBudgetExhausted:
                pass
        return json.loads(json.dumps(RECORDED["annotated"]))

    deriver = LangExtractDeriver(
        Settings(max_run_tokens=50), backend=FakeBackend(), runner=swallowing
    )
    with pytest.raises(BackendBudgetExhausted):
        deriver.derive(vault, DeriveRequest("entities", ALL_IDS))


# -- configuration, gate and registry --------------------------------------------------------------

MODULE = "tests.test_adapter_langextract"


def test_factory_loads_the_backend_named_in_settings() -> None:
    deriver = factory({"backend": f"{MODULE}:fake_backend_factory"})
    # Imported again under its module path, so compare by name, not class identity.
    assert deriver.backend.name == "fake:langextract" and deriver.refused == ""


@pytest.mark.parametrize(
    ("settings", "code"),
    [
        ({}, "no_backend"),
        ({"backend": "nocolon"}, "bad_backend_spec"),
        ({"backend": "no_such_module_xyz:factory"}, "backend_not_importable"),
        ({"backend": f"{MODULE}:fake_untasked_factory"}, "backend_task"),
        ({"backend": f"{MODULE}:fake_unavailable_factory"}, "backend_unavailable:missing_key"),
        (
            {"backend": f"{MODULE}:fake_backend_factory", "backend_options": {"local": False}},
            "cloud_not_allowed",
        ),
        ({"backend": f"{MODULE}:fake_backend_factory", "api_key": "x"}, "key_in_config"),
        (
            {"backend": f"{MODULE}:fake_backend_factory", "backend_options": {"token": "x"}},
            "key_in_config",
        ),
        ({"backend": f"{MODULE}:fake_backend_factory", "surprise": 1}, "bad_option"),
        ({"backend": f"{MODULE}:fake_backend_factory", "max_run_tokens": 0}, "bad_option"),
        ({"backend": f"{MODULE}:fake_backend_factory", "cloud": "yes"}, "bad_option"),
    ],
)
def test_refusals_are_unavailable_with_a_code(settings: dict[str, Any], code: str) -> None:
    deriver = factory(settings)
    assert deriver.backend is None
    assert deriver.available().available is False
    assert deriver.available().reason == code


def test_a_non_local_backend_needs_cloud_true() -> None:
    settings = Settings(
        backend=f"{MODULE}:fake_backend_factory", backend_options={"local": False}, cloud=True
    )
    backend, refused = load_backend(settings)
    assert refused == "" and backend.local is False


def test_unavailable_deriver_refuses_to_derive(vault: Path) -> None:
    deriver = factory({})
    with pytest.raises(BackendUnavailable):
        deriver.derive(vault, DeriveRequest("entities", ("m-backup",)))


@pytest.mark.skipif(
    importlib.util.find_spec("langextract") is not None, reason="langextract is installed here"
)
def test_without_langextract_the_adapter_is_unavailable_not_broken() -> None:
    deriver = LangExtractDeriver(Settings(), backend=FakeBackend())
    assert deriver.available().reason == "missing_dependency"
    registry = AdapterRegistry()
    registry.register(SPEC)
    off = registry.load("langextract", AdaptersConfig())
    assert (off.enabled, off.reason) == (False, "disabled")
    on = registry.load(
        "langextract",
        AdaptersConfig.from_mapping(
            {
                "enabled": ["langextract"],
                "langextract": {"backend": f"{MODULE}:fake_backend_factory"},
            }
        ),
    )
    assert (on.available, on.reason, on.adapter) == (False, "missing_dependency", None)


def test_registry_builds_it_when_the_dependency_is_present(monkeypatch: pytest.MonkeyPatch) -> None:
    import memfw_adapter_langextract.deriver as module

    monkeypatch.setattr(module, "_external_version", lambda: EXTERNAL_VERSION)
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda name: object())
    registry = AdapterRegistry()
    registry.register(
        type(SPEC)(name=SPEC.name, kind=SPEC.kind, factory=SPEC.factory, version=SPEC.version)
    )
    config = {
        "enabled": ["langextract"],
        "langextract": {"backend": f"{MODULE}:fake_backend_factory"},
    }
    status = registry.load("langextract", AdaptersConfig.from_mapping(config))
    assert status.available, status.reason
    monkeypatch.setattr(module, "_external_version", lambda: "1.8.0")
    assert status.adapter.available().reason == "version_mismatch"  # type: ignore[union-attr]


# -- the output is data -------------------------------------------------------------------------


def test_hostile_or_ungrounded_output_is_dropped() -> None:
    text = "The backup job runs nightly."
    bad = [
        {"document_id": "m-other", "extractions": [_ex("entity", "backup job", 4, 14)]},
        "not a document",
        {
            "document_id": "m-a",
            "extractions": [
                _ex("entity", "backup job", 4, 14),  # kept
                _ex("entity", "backup job", 4, 14),  # duplicate
                _ex("entity", "nightly", 4, 14),  # exact span does not hold the text
                _ex("entity", "job", 4, 99),  # outside the body
                _ex("entity", "job", 14, 4),  # empty / reversed
                _ex("person", "backup job", 4, 14),  # class outside the prompt
                _ex("entity", "backup job", 4, 14, alignment=None),  # never aligned
                {
                    **_ex("entity", "job", 11, 14),
                    "char_interval": {"start_pos": 11.0, "end_pos": 14},
                },
                _ex("entity", "   ", 0, 3),
                "not an extraction",
            ],
        },
    ]
    rows = rows_from_documents(bad, {"m-a": text})
    assert [(row["text"], row["start"], row["end"]) for row in rows] == [("backup job", 4, 14)]


def test_attributes_are_short_scalars_from_the_prompt_only() -> None:
    long = "x" * 500
    item = _ex("entity", "backup job", 4, 14)
    item["attributes"] = {"type": "Spaceship", "secret": "drop me", "note": long}
    rel = _ex("relation", "runs nightly", 15, 27)
    rel["attributes"] = {"subject": long, "predicate": ["a", "b"], "object": "night"}
    rows = rows_from_documents(
        [{"document_id": "m-a", "extractions": [item, rel]}],
        {"m-a": "The backup job runs nightly."},
    )
    assert rows[0]["attributes"] == {"type": "other"}
    assert rows[1]["attributes"] == {"subject": "x" * 80, "object": "night"}


def _ex(
    cls: str, text: str, start: int, end: int, *, alignment: str | None = "match_exact"
) -> dict[str, Any]:
    return {
        "extraction_class": cls,
        "extraction_text": text,
        "char_interval": {"start_pos": start, "end_pos": end},
        "alignment_status": alignment,
        "attributes": None,
    }


# -- candidates through memfw's staging ------------------------------------------------------


def test_candidates_stage_only_under_candidates_and_stay_capped(vault: Path) -> None:
    deriver, _ = make()
    view = deriver.derive(vault, DeriveRequest("entities", ALL_IDS))
    items = candidates(view)
    assert len(items) == len(json.loads(view.content)["extractions"])
    assert {item.metadata["causal"] for item in items} == {"none"}
    assert {item.metadata["epistemic"] for item in items} == {"inference"}
    assert {item.metadata["memory_id"] for item in items} == {"m-backup", "m-release"}
    before = tree_digest(vault, exclude=("_candidates",))
    first = stage_candidates(vault, "langextract", items, proposal_id="p-1", source="langextract")
    again = stage_candidates(vault, "langextract", items, proposal_id="p-1", source="langextract")
    assert len(first.staged) == len(items) and again.staged == ()
    assert tree_digest(vault, exclude=("_candidates",)) == before
    staged = sorted((vault / "_candidates" / "langextract").glob("*.json"))
    assert len(staged) == len(items)


def test_candidates_refuse_a_foreign_view() -> None:
    from memfw.ports import Citation

    other = DerivedView("concat", "entities", "- x [m-a]", (Citation("m-a"),))
    with pytest.raises(ValueError):
        candidates(other)


# -- the bundled Ollama backend -------------------------------------------------------------------


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def test_ollama_backend_request_and_reply_shape() -> None:
    sent: list[Any] = []

    def opener(request: Any, timeout: float) -> _Response:
        sent.append((request.full_url, json.loads(request.data), timeout))
        reply = {"response": "```json\n{}\n```", "prompt_eval_count": 12, "eval_count": 3}
        return _Response(json.dumps(reply).encode())

    backend = OllamaBackend(OllamaSettings(model="m1"), opener=opener)
    assert backend.local is True and backend.tasks == (TASK,)
    reply = backend.run(TASK, {"prompt": "hello"}, max_tokens=64)
    assert (reply.text, reply.tokens_in, reply.tokens_out) == ("```json\n{}\n```", 12, 3)
    url, body, timeout = sent[0]
    assert url == "http://127.0.0.1:11434/api/generate" and timeout == 120.0
    assert body == {
        "model": "m1",
        "prompt": "hello",
        "stream": False,
        "options": {"temperature": 0, "num_predict": 64},
    }
    with pytest.raises(ValueError):
        backend.run("chunk_summary", {"prompt": "x"}, max_tokens=8)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:11434",
        "http://ollama.example.com:11434",
        "http://10.0.0.5:11434",
        "http://user:pw@127.0.0.1:11434",
        "http://127.0.0.1:11434/?x=1",
        "file:///tmp/x",
    ],
)
def test_ollama_backend_is_loopback_only(url: str) -> None:
    with pytest.raises(BackendUnavailable, match="bad_base_url"):
        check_loopback_url(url)
    with pytest.raises(BackendUnavailable):
        OllamaSettings(base_url=url)


def test_ollama_bad_replies_are_codes() -> None:
    assert callable(loopback_opener())
    backend = OllamaBackend(OllamaSettings(), opener=lambda r, t: _Response(b"not json"))
    with pytest.raises(BackendUnavailable, match="bad_response"):
        backend.run(TASK, {"prompt": "x"}, max_tokens=8)
    backend = OllamaBackend(OllamaSettings(), opener=lambda r, t: _Response(b'{"response": 3}'))
    with pytest.raises(BackendUnavailable, match="bad_response"):
        backend.run(TASK, {"prompt": "x"}, max_tokens=8)


def test_ollama_opener_has_an_empty_proxy_handler() -> None:
    import urllib.request

    from memfw_adapter_langextract import ollama

    captured: list[Any] = []
    real = urllib.request.build_opener

    def spy(*handlers: Any) -> Any:
        captured.extend(handlers)
        return real(*handlers)

    original = ollama.urllib.request.build_opener
    ollama.urllib.request.build_opener = spy  # type: ignore[assignment]
    try:
        ollama.loopback_opener()
    finally:
        ollama.urllib.request.build_opener = original  # type: ignore[assignment]
    proxies = [h for h in captured if isinstance(h, urllib.request.ProxyHandler)]
    assert len(proxies) == 1 and proxies[0].proxies == {}


# -- layering ----------------------------------------------------------------------------------


def test_adapter_imports_only_stdlib_the_ports_and_lazily_langextract() -> None:
    allowed = set(sys.stdlib_module_names) | {"memfw_adapter_langextract", "memfw_ports"}
    for path in sorted((ADAPTER_SRC / "memfw_adapter_langextract").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        top_level = {id(node) for node in tree.body}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                if name.split(".")[0] == "langextract":
                    assert id(node) not in top_level, (path.name, name)  # imported lazily only
                    continue
                assert name.split(".")[0] in allowed or name.startswith("memfw.ports"), name
    for path in (ROOT / "src" / "memfw").rglob("*.py"):
        assert "memfw_adapter_langextract" not in path.read_text(encoding="utf-8"), path


def test_importing_the_adapter_does_not_import_langextract() -> None:
    import subprocess

    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import memfw_adapter_langextract; "
        "assert 'langextract' not in sys.modules"
    )
    done = subprocess.run(
        [sys.executable, "-c", code, str(ADAPTER_SRC)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
