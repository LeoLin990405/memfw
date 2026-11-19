"""W1-1: ``Citation.ref``, the wiki page/job formats and ``EntailmentChecker``.

Synthetic fixtures only. The page is about an imaginary ``concept:example:
backup-plan``; ids, hashes and texts are made up.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import pickle
import re
from typing import Any

import pytest

import memfw.ports as ports
from memfw.ports import (
    PORT_VERSIONS,
    PORTS_API_VERSION,
    Citation,
    DerivedView,
    EntailmentChecker,
    WikiFormatError,
    check_view_citations,
)
from memfw.ports.refs import CITATION_REF_KINDS, parse_note_ref
from memfw.ports.testing import (
    ContractError,
    check_entailment_checker,
    check_wiki_page,
)
from memfw.ports.types import MEMORY_ID_RE, Availability
from memfw.ports.wiki import (
    WIKI_ERROR_CODES,
    WIKI_JOB_SCHEMA,
    WIKI_PAGE_SCHEMA,
    WikiConcept,
    WikiJob,
    WikiJobSource,
    WikiOpenQuestion,
    WikiPage,
    WikiSource,
    parse_wiki_job,
    parse_wiki_page,
    wiki_job_digest,
    wiki_page_key,
    wiki_page_schema,
    wiki_sources_digest,
)

PRX = "prx-01a1e931-1818-772b-8713-76e6bea4a716"


# --- Citation: the old usage is unchanged ---------------------------------------------


def test_old_citation_construction_and_repr_are_byte_identical() -> None:
    # Snapshot of what 0.3.0 printed; the strings are the dataclass repr.
    cases = {
        Citation("mem-1"): "Citation(memory_id='mem-1', version=None, note='')",
        Citation("mem-2", 2): "Citation(memory_id='mem-2', version=2, note='')",
        Citation("m-a", version=3, note="2 extractions"): (
            "Citation(memory_id='m-a', version=3, note='2 extractions')"
        ),
        Citation("att-1", None, ""): "Citation(memory_id='att-1', version=None, note='')",
    }
    for citation, expected in cases.items():
        assert repr(citation) == expected
        assert citation.ref is None and citation.ref_kind is None
        assert citation.to_json() == {
            "memory_id": citation.memory_id,
            "version": citation.version,
            "note": citation.note,
        }
    snapshot = json.dumps([c.to_json() for c in cases], sort_keys=True).encode()
    assert (
        hashlib.sha256(snapshot).hexdigest()
        == hashlib.sha256(
            b'[{"memory_id": "mem-1", "note": "", "version": null}, '
            b'{"memory_id": "mem-2", "note": "", "version": 2}, '
            b'{"memory_id": "m-a", "note": "2 extractions", "version": 3}, '
            b'{"memory_id": "att-1", "note": "", "version": null}]'
        ).hexdigest()
    )


def test_old_citation_equality_hash_pickle_and_errors_are_unchanged() -> None:
    assert Citation("m", 1) == Citation("m", version=1)
    assert hash(Citation("m", 1)) == hash(Citation("m", 1))
    assert {Citation("m"), Citation("m")} == {Citation("m")}
    assert pickle.loads(pickle.dumps(Citation("m", 2, "n"))) == Citation("m", 2, "n")
    assert dataclasses.astuple(Citation("m", 2, "n"))[:3] == ("m", 2, "n")
    with pytest.raises(ValueError, match="citation memory_id required"):
        Citation(" ")
    with pytest.raises(ValueError, match="citation memory_id required"):
        Citation("")
    with pytest.raises(TypeError):
        Citation()  # type: ignore[call-arg]
    view = DerivedView("concat", "list", "x", (Citation("m-1"), Citation("m-2", 2)))
    assert view.cited_ids() == ("m-1", "m-2")


def test_citation_ref_forms() -> None:
    memory = Citation.from_ref("mem:mem-20261108-x@v3")
    assert (memory.memory_id, memory.version, memory.ref_kind) == ("mem-20261108-x", 3, "memory")
    assert Citation.from_ref("mem:m-1").version is None
    assert Citation("m-1", 3, ref="mem:m-1@v3") == Citation.from_ref("mem:m-1@v3")
    note = Citation.from_ref("kh:Projects/Backup%20Plan.md#Schedule")
    assert (note.memory_id, note.version, note.ref_kind) == ("", None, "note_segment")
    assert Citation.from_ref(f"att:{PRX}:12").ref_kind == "attachment_segment"
    assert Citation.from_ref("wiki:wiki-0123456789abcdef#facts").ref_kind == "wiki_page"
    assert set(CITATION_REF_KINDS) == {"memory", "note_segment", "attachment_segment", "wiki_page"}
    assert repr(note) == (
        "Citation(memory_id='', version=None, note='', "
        "ref='kh:Projects/Backup%20Plan.md#Schedule', ref_kind='note_segment')"
    )
    assert note.to_json()["ref_kind"] == "note_segment"
    explicit = Citation("", ref="kh:a.md", ref_kind="note_segment")
    assert explicit == Citation.from_ref("kh:a.md")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"memory_id": "", "ref": "conv:abc"},
        {"memory_id": "", "ref": "session:abc"},
        {"memory_id": "", "ref": "mem:bad id@v1"},
        {"memory_id": "", "ref": "mem:m@v0"},
        {"memory_id": "", "ref": "kh:../escape.md"},
        {"memory_id": "", "ref": "kh:/abs.md"},
        {"memory_id": "", "ref": "att:prx-nope:1"},
        {"memory_id": "", "ref": f"att:{PRX}:0"},
        {"memory_id": "", "ref": "wiki:wiki-123"},
        {"memory_id": "", "ref": "wiki:wiki-0123456789abcdef#sources"},
        {"memory_id": "", "ref": "x" * 600},
        {"memory_id": "other", "ref": "mem:m-1@v3"},
        {"memory_id": "m-1", "version": 2, "ref": "mem:m-1@v3"},
        {"memory_id": "m-1", "ref": "kh:a.md"},
        {"memory_id": "", "version": 1, "ref": "kh:a.md"},
        {"memory_id": "", "ref": "kh:a.md", "ref_kind": "memory"},
        {"memory_id": "m-1", "ref_kind": "memory"},
    ],
)
def test_citation_refuses_bad_refs(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Citation(**kwargs)


def test_non_memory_citation_fails_view_citation_check_closed() -> None:
    view = DerivedView("concat", "list", "x", (Citation("m-1"), Citation.from_ref("kh:a.md")))
    assert view.cited_ids() == ("m-1", "")
    with pytest.raises(ValueError, match="not a memory"):
        check_view_citations(view, lambda memory_id: memory_id == "m-1")


def test_note_grammar_matches_recall_parse_kh_ref() -> None:
    from memfw.recall import parse_kh_ref

    corpus = [
        "kh:a.md",
        "kh:a.md#",
        "kh:a.md# Heading ",
        "kh:dir/sub/note.md#Sec%20One",
        "kh:%E5%A4%87%E4%BB%BD.md#%E8%AE%A1%E5%88%92",
        "kh:%FF.md",
        "kh:/abs.md",
        "kh:a/../b.md",
        "kh:a//b.md",
        "kh:./a.md",
        "kh:a\\b.md",
        "kh:a%0Ab.md",
        "kh:a.md#x%07",
        "kh:",
        "kh:#h",
        "khx:a.md",
        "mem:x",
    ]
    for ref in corpus:
        assert parse_note_ref(ref) == parse_kh_ref(ref), ref


def test_memory_id_rule_copy_matches() -> None:
    from memfw.ports import refs

    assert refs._MEMORY_ID_RE.pattern == MEMORY_ID_RE.pattern


# --- page fixtures --------------------------------------------------------------------

CONCEPT = "concept:example:backup-plan"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _sources() -> list[dict[str, Any]]:
    return [
        {
            "ref": "mem:mem-20261101-backup@v3",
            "kind": "memory",
            "sha256": _sha("a"),
            "privacy": "shared",
            "causal": "association",
            "status": "current",
        },
        {
            "ref": "kh:Infra/Backup.md#Schedule",
            "kind": "note_segment",
            "sha256": _sha("b"),
            "privacy": "public",
            "causal": "none",
            "status": "current",
        },
        {
            "ref": f"att:{PRX}:2",
            "kind": "attachment_segment",
            "sha256": _sha("c"),
            "privacy": "shared",
            "causal": "none",
            "status": "current",
        },
        {
            "ref": "mem:mem-20261001-old-backup@v1",
            "kind": "memory",
            "sha256": _sha("d"),
            "privacy": "public",
            "causal": "none",
            "status": "superseded",
            "superseded_by": "mem:mem-20261101-backup@v3",
        },
        {
            "ref": "mem:mem-20261105-dispute@v2",
            "kind": "memory",
            "sha256": _sha("e"),
            "privacy": "shared",
            "causal": "none",
            "status": "disputed",
        },
    ]


def _claim(cid: str, text: str, cites: list[str], **extra: Any) -> dict[str, Any]:
    claim: dict[str, Any] = {
        "cid": cid,
        "text": text,
        "cites": cites,
        "causal": "none",
        "kind": "statement",
        "epistemic": "inference",
        "checks": {"tokens": "pass", "nli": {"min": 0.91}},
    }
    claim.update(extra)
    return claim


def _build(job_digest: str = "sha256:" + "1" * 64) -> dict[str, str]:
    return {
        "generator": "wikigen",
        "generator_version": "0.1.0",
        "model": "example-7b-q4",
        "model_digest": "sha256:" + "a" * 64,
        "prompt_version": "wiki-v1",
        "checker_version": "wiki-gate-0.1.0",
        "nli_model": "example-nli@sha256:" + "b" * 12,
        "view_version": "view-20261119",
        "job_digest": job_digest,
    }


def _page(**overrides: Any) -> dict[str, Any]:
    sources = overrides.pop("sources", _sources())
    tenant = overrides.pop("tenant", "")
    concept = overrides.pop("concept", CONCEPT)
    m1, kh, att, old, disputed = (s["ref"] for s in _sources())
    doc: dict[str, Any] = {
        "format": "memfw-wiki-page/1",
        "page_key": wiki_page_key(tenant, concept),
        "concept": concept,
        "tenant": tenant,
        "privacy": "shared",
        "build": _build(),
        "sources": sources,
        "sections": [
            {
                "id": "summary",
                "claims": [_claim("c1", "备份每晚 02:00 运行（截至 11-01）。", [m1, kh])],
            },
            {
                "id": "facts",
                "claims": [_claim("c2", "The schedule screenshot shows 02:00.", [att])],
            },
            {
                "id": "timeline",
                "claims": [
                    _claim(
                        "c3",
                        "An older plan ran weekly; it was replaced.",
                        [old, m1],
                        kind="superseded",
                    ),
                ],
            },
            {
                "id": "relations",
                "claims": [
                    _claim(
                        "c4",
                        "Night runs may be associated with fewer conflicts.",
                        [m1],
                        causal="association",
                    ),
                ],
            },
            {
                "id": "open",
                "claims": [
                    _claim("c5", "One record says 03:00 instead.", [disputed], kind="conflict"),
                    _claim(
                        "c6", "A run log would settle it.", [disputed, m1], kind="missing_evidence"
                    ),
                ],
            },
        ],
        "open_questions": ["oq-20261105-backup-time"],
        "built_from_digest": "",
    }
    doc.update(overrides)
    if "built_from_digest" not in overrides:
        doc["built_from_digest"] = wiki_sources_digest([WikiSource(**s) for s in doc["sources"]])
    return doc


def _code(doc: Any) -> str:
    with pytest.raises(WikiFormatError) as info:
        parse_wiki_page(doc)
    assert info.value.code in WIKI_ERROR_CODES
    return info.value.code


def test_valid_page_parses_and_round_trips() -> None:
    doc = _page()
    page = parse_wiki_page(doc)
    assert page.section_ids() == ("summary", "facts", "timeline", "relations", "open")
    assert page.to_json() == doc
    assert parse_wiki_page(page.canonical_bytes()) == page
    assert parse_wiki_page(json.dumps(doc)) == page
    assert page.claims[0].citations[0] == Citation.from_ref("mem:mem-20261101-backup@v3")
    assert page.page_digest().startswith("sha256:")
    assert page.privacy == "shared"


def _mutate(path: list[Any], value: Any, doc: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = copy.deepcopy(_page() if doc is None else doc)
    node: Any = doc
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return doc


def test_missing_citation_is_invalid() -> None:
    # A cite that is not one of the page's sources does not resolve.
    doc = _mutate(["sections", 0, "claims", 0, "cites"], ["mem:mem-unlisted@v1"])
    assert _code(doc) == "unknown_citation"


def test_uncited_sentence_is_invalid() -> None:
    assert _code(_mutate(["sections", 1, "claims", 0, "cites"], [])) == "uncited_claim"


def test_privacy_weaker_than_a_source_is_invalid() -> None:
    # Sources include shared; a public page would be looser than its strictest source.
    assert _code(_mutate(["privacy"], "public")) == "privacy_not_strictest"


def test_privacy_stricter_than_sources_or_private_source_is_invalid() -> None:
    assert _code(_mutate(["privacy"], "local-private")) == "privacy_not_strictest"
    sources = _sources()
    sources[1]["privacy"] = "local-private"
    doc = _page(sources=sources, privacy="local-private")
    assert _code(doc) == "privacy_not_releasable"
    assert _code(_page(sources=sources)) == "privacy_not_strictest"
    assert _code(_mutate(["privacy"], "unknown")) == "field"


def test_causal_label_above_source_is_invalid() -> None:
    # c4 cites one association source; claiming hypothesis upgrades it.
    doc = _mutate(["sections", 3, "claims", 0, "causal"], "hypothesis")
    assert _code(doc) == "causal_above_source"
    # Two association-or-weaker sources together are still association.
    sources = _sources()
    sources[1]["causal"] = "association"
    doc = _page(sources=sources)
    doc["sections"][3]["claims"][0].update(
        cites=[sources[0]["ref"], sources[1]["ref"]], causal="hypothesis"
    )
    assert _code(doc) == "causal_above_source"


def test_causal_caps() -> None:
    sources = _sources()
    sources[0]["causal"] = "verified"
    ok = _page(sources=sources)
    ok["sections"][3]["claims"][0]["causal"] = "hypothesis"
    assert parse_wiki_page(ok).claims[3].causal == "hypothesis"
    bad = copy.deepcopy(ok)
    bad["sections"][3]["claims"][0]["causal"] = "verified"
    assert _code(bad) == "causal_cap"
    facts = _mutate(["sections", 1, "claims", 0, "causal"], "association")
    assert _code(facts) == "causal_outside_relations"


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (["sections", 0, "claims", 0, "epistemic"], "fact", "epistemic_cap"),
        (["sections", 0, "claims", 0, "cites"], ["wiki:wiki-0123456789abcdef"], "wiki_citation"),
        (["sections", 0, "claims", 0, "cites"], ["conv:sess-1"], "unknown_citation"),
        (["sections", 0, "claims", 0, "cites"], ["mem:mem-20261101-backup"], "unversioned_memory"),
        (
            ["sections", 0, "claims", 0, "cites"],
            ["mem:mem-20261001-old-backup@v1"],
            "inactive_source_section",
        ),
        (
            ["sections", 1, "claims", 0, "cites"],
            ["mem:mem-20261105-dispute@v2"],
            "inactive_source_section",
        ),
        (["sections", 0, "claims", 0, "kind"], "conflict", "claim_kind_section"),
        (["sections", 4, "claims", 0, "kind"], "statement", "claim_kind_section"),
        (["sections", 1, "claims", 0, "cid"], "c1", "duplicate"),
        (["sections", 0, "claims", 0, "text"], "  ", "field"),
        (["sections", 0, "claims", 0, "text"], "line\nbreak", "field"),
        (["sections", 0, "claims", 0, "checks", "nli", "min"], 1.5, "field"),
        (["sections", 0, "claims", 0, "checks", "tokens"], "fail", "field"),
        (["sections", 0, "claims", 0, "extra"], "smuggled", "field"),
        (["page_key"], "wiki-0000000000000000", "page_key"),
        (["built_from_digest"], "sha256:" + "0" * 64, "digest_mismatch"),
        (["format"], "memfw-wiki-page/2", "format"),
        (["open_questions"], ["oq-1", "oq-1"], "duplicate"),
        (["build", "model_digest"], "latest", "field"),
        (["build", "generator"], "Wiki Gen", "field"),
        (["sources", 0, "kind"], "note_segment", "field"),
        (["sources", 0, "sha256"], "XYZ", "field"),
    ],
)
def test_page_refusals(path: list[Any], value: Any, code: str) -> None:
    assert _code(_mutate(path, value)) == code


def test_page_structure_refusals() -> None:
    doc = _page()
    doc["sections"] = doc["sections"][1:]
    assert _code(doc) == "summary_missing"
    doc = _page()
    doc["sections"][0], doc["sections"][1] = doc["sections"][1], doc["sections"][0]
    assert _code(doc) == "section_order"
    doc = _page()
    doc["sections"].append(copy.deepcopy(doc["sections"][1]))
    assert _code(doc) == "duplicate"
    doc = _page()
    doc["sections"][1]["claims"] = []
    assert _code(doc) == "field"
    doc = _page()
    doc["sources"].append(copy.deepcopy(doc["sources"][0]))
    assert _code(_page(sources=doc["sources"])) == "duplicate"
    wiki_source = dict(_sources()[0], ref="wiki:wiki-0123456789abcdef", kind="memory")
    with_wiki = _page(built_from_digest="sha256:" + "0" * 64)
    with_wiki["sources"].append(wiki_source)
    assert _code(with_wiki) == "wiki_citation"
    assert _code("{not json") == "format"
    assert _code('{"format": NaN}') == "format"
    assert _code([]) == "format"
    assert _code(b"\xff") == "format"
    del doc["tenant"]
    assert _code(doc) == "field"


def test_page_size_limit() -> None:
    doc = _page()
    m1 = _sources()[0]["ref"]
    doc["sections"][1]["claims"] = [_claim(f"c{100 + n}", "x" * 990, [m1]) for n in range(20)]
    assert _code(doc) == "too_large"


def test_tenant_changes_page_key() -> None:
    page = parse_wiki_page(_page(tenant="alpha"))
    assert page.page_key == wiki_page_key("alpha", CONCEPT) != wiki_page_key("", CONCEPT)
    assert re.fullmatch(r"wiki-[0-9a-f]{16}", page.page_key)


def test_direct_construction_validates_too() -> None:
    page = parse_wiki_page(_page())
    with pytest.raises(WikiFormatError, match="privacy_not_strictest"):
        dataclasses.replace(page, privacy="public")
    with pytest.raises(WikiFormatError, match="summary_missing"):
        dataclasses.replace(page, claims=page.claims[1:])
    with pytest.raises(WikiFormatError, match="epistemic_cap"):
        dataclasses.replace(page.claims[0], epistemic="fact")


# --- JSON Schema ----------------------------------------------------------------------


def _schema_errors(schema: Any, value: Any, root: Any, path: str = "$") -> list[str]:
    """A tiny JSON Schema subset walker (the keywords the wiki schemas use)."""

    if "$ref" in schema:
        assert schema["$ref"].startswith("#/$defs/")
        return _schema_errors(root["$defs"][schema["$ref"][8:]], value, root, path)
    if "anyOf" in schema:
        options = [_schema_errors(option, value, root, path) for option in schema["anyOf"]]
        return [] if any(not errors for errors in options) else [f"{path}: anyOf"]
    errors: list[str] = []
    kind = schema.get("type")
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "number": (int, float),
        "null": type(None),
    }
    if kind is not None:
        if not isinstance(value, types[kind]) or isinstance(value, bool):
            return [f"{path}: type"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: const")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: enum")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: pattern")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 1e9):
            errors.append(f"{path}: length")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", -1e18) or value > schema.get("maximum", 1e18):
            errors.append(f"{path}: range")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 1e9):
            errors.append(f"{path}: items count")
        for index, item in enumerate(value):
            errors += _schema_errors(schema.get("items", {}), item, root, f"{path}[{index}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing {key}")
        if schema.get("additionalProperties") is False:
            errors += [f"{path}: extra {key}" for key in value if key not in props]
        for key, item in value.items():
            if key in props:
                errors += _schema_errors(props[key], item, root, f"{path}.{key}")
    return errors


def test_schemas_are_plain_json_and_copies() -> None:
    assert json.loads(json.dumps(WIKI_PAGE_SCHEMA)) == WIKI_PAGE_SCHEMA
    assert json.loads(json.dumps(WIKI_JOB_SCHEMA)) == WIKI_JOB_SCHEMA
    copy_ = wiki_page_schema()
    copy_["title"] = "changed"
    assert WIKI_PAGE_SCHEMA["title"] == "memfw-wiki-page/1"
    assert WIKI_PAGE_SCHEMA["$schema"].endswith("2020-12/schema")


def test_valid_page_passes_schema_and_structural_negatives_fail_both() -> None:
    doc = _page()
    assert _schema_errors(WIKI_PAGE_SCHEMA, doc, WIKI_PAGE_SCHEMA) == []
    structural = [
        _mutate(["sections", 1, "claims", 0, "cites"], []),
        _mutate(["sections", 0, "claims", 0, "epistemic"], "fact"),
        _mutate(["sections", 3, "claims", 0, "causal"], "verified"),
        _mutate(["privacy"], "local-private"),
        _mutate(["sections", 0, "claims", 0, "extra"], 1),
        _mutate(["sections", 0, "claims", 0, "cites"], ["conv:sess-1"]),
        _mutate(["format"], "memfw-wiki-page/2"),
    ]
    for bad in structural:
        assert _schema_errors(WIKI_PAGE_SCHEMA, bad, WIKI_PAGE_SCHEMA), bad
        with pytest.raises(WikiFormatError):
            parse_wiki_page(bad)


def test_cross_field_rules_are_parser_only() -> None:
    # The schema cannot see these; the parser is the reference.
    for bad in (
        _mutate(["privacy"], "public"),
        _mutate(["sections", 3, "claims", 0, "causal"], "hypothesis"),
        _mutate(["sections", 0, "claims", 0, "cites"], ["mem:mem-unlisted@v1"]),
    ):
        assert _schema_errors(WIKI_PAGE_SCHEMA, bad, WIKI_PAGE_SCHEMA) == []
        with pytest.raises(WikiFormatError):
            parse_wiki_page(bad)


# --- jobs -----------------------------------------------------------------------------


def _generator() -> dict[str, str]:
    build = _build()
    return {
        k: build[k]
        for k in ("generator", "generator_version", "model", "model_digest", "prompt_version")
    }


def _job(previous: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    sources = [dict(s, text=f"released text {n}") for n, s in enumerate(_sources())]
    questions = [
        {
            "id": "oq-20261105-backup-time",
            "question": "02:00 or 03:00?",
            "missing": "a run log",
            # The two sides: the disputed record and one that is not a page source.
            "conflicts": ["mem-20261105-dispute", "mem-20261104-other-time"],
        }
    ]
    doc: dict[str, Any] = {
        "format": "memfw-wiki-job/1",
        "job_id": "job-0001",
        "job_digest": "",
        "tenant": "",
        "page_key": wiki_page_key("", CONCEPT),
        "concept": {
            "oid": CONCEPT,
            "pref_label": "Backup plan",
            "alt_labels": ["备份方案"],
            "definition": "How backups run.",
        },
        "sources": sources,
        "open_questions": questions,
        "previous_page": previous,
        "generator": _generator(),
        "checker_version": "wiki-gate-0.1.0",
        "view_version": "view-20261119",
    }
    doc.update(overrides)
    if "job_digest" not in overrides:
        try:
            doc["job_digest"] = _fixture_job_digest(doc, previous)
        except WikiFormatError:  # an invalid fixture: the parser will say why
            doc["job_digest"] = "sha256:" + "0" * 64
    return doc


def _fixture_job_digest(doc: dict[str, Any], previous: dict[str, Any] | None) -> str:
    parsed_sources = [
        WikiJobSource(
            WikiSource(
                **{k: v for k, v in s.items() if k not in ("text", "truncated", "full_sha256")}
            ),
            s["text"],
            truncated="truncated" in s,
            full_sha256=s.get("full_sha256"),
        )
        for s in doc["sources"]
    ]
    return wiki_job_digest(
        concept=WikiConcept(
            doc["concept"]["oid"],
            doc["concept"]["pref_label"],
            tuple(doc["concept"]["alt_labels"]),
            doc["concept"]["definition"],
        ),
        tenant=doc["tenant"],
        sources=parsed_sources,
        open_questions=[
            WikiOpenQuestion(q["id"], q["question"], q["missing"], tuple(q["conflicts"]))
            for q in doc["open_questions"]
        ],
        previous_page=None if previous is None else parse_wiki_page(previous),
        generator=doc["generator"],
        checker_version=doc["checker_version"],
    )


def _job_code(doc: Any) -> str:
    with pytest.raises(WikiFormatError) as info:
        parse_wiki_job(doc)
    return info.value.code


def test_valid_job_parses_round_trips_and_matches_schema() -> None:
    doc = _job()
    job = parse_wiki_job(doc)
    assert job.to_json() == doc
    assert parse_wiki_job(json.dumps(doc)) == job
    assert _schema_errors(WIKI_JOB_SCHEMA, doc, WIKI_JOB_SCHEMA) == []
    with_previous = _job(previous=_page())
    assert parse_wiki_job(with_previous).previous_page == parse_wiki_page(_page())
    assert _schema_errors(WIKI_JOB_SCHEMA, with_previous, WIKI_JOB_SCHEMA) == []
    assert with_previous["job_digest"] != doc["job_digest"]


def test_job_refusals() -> None:
    assert _job_code(_job(job_digest="sha256:" + "0" * 64)) == "digest_mismatch"
    private = _job()
    private["sources"][0]["privacy"] = "local-private"
    assert _job_code(_job(sources=private["sources"])) == "privacy_not_releasable"
    assert _schema_errors(WIKI_JOB_SCHEMA, _job(sources=private["sources"]), WIKI_JOB_SCHEMA)
    assert _job_code(_job(page_key="wiki-0000000000000000")) == "page_key"
    assert _job_code(dict(_job(), vault_path="x")) == "field"
    other = _page(concept="concept:example:other")
    assert _job_code(_job(previous=other)) == "field"
    empty = _job()
    empty["sources"][0]["text"] = " "
    assert _job_code(_job(sources=empty["sources"])) == "field"
    assert _job_code(_job(generator={**_generator(), "extra": "x"})) == "field"


def test_job_digest_is_stable_and_input_sensitive() -> None:
    a = _job()["job_digest"]
    assert _job()["job_digest"] == a
    assert _job(checker_version="wiki-gate-0.2.0")["job_digest"] != a
    assert _job(view_version="view-20261120")["job_digest"] == a  # same work, new view
    changed = _job()
    changed["sources"][0]["sha256"] = _sha("edited")
    assert _job(sources=changed["sources"])["job_digest"] != a


# --- contract kit ---------------------------------------------------------------------


def _page_for(job: dict[str, Any]) -> dict[str, Any]:
    return _page(build=_build(job["job_digest"]))


def test_check_wiki_page_accepts_valid_pages() -> None:
    page = check_wiki_page(_page())
    assert isinstance(page, WikiPage)
    assert check_wiki_page(page) == page
    assert check_wiki_page(json.dumps(_page())) == page
    job = _job()
    assert check_wiki_page(_page_for(job), job=job).build.job_digest == job["job_digest"]
    assert isinstance(parse_wiki_job(job), WikiJob)
    check_wiki_page(_page_for(job), job=parse_wiki_job(job))


def test_check_wiki_page_refuses() -> None:
    job = _job()
    with pytest.raises(ContractError, match="uncited_claim"):
        check_wiki_page(_mutate(["sections", 1, "claims", 0, "cites"], []))
    with pytest.raises(ContractError, match="job_digest"):
        check_wiki_page(_page(), job=job)
    foreign = _sources()
    foreign[1]["sha256"] = _sha("not what the job offered")
    with pytest.raises(ContractError, match="source fields differ"):
        check_wiki_page(_page(sources=foreign, build=_build(job["job_digest"])), job=job)
    wrong_build = _build(job["job_digest"])
    wrong_build["model_digest"] = "sha256:" + "f" * 64
    with pytest.raises(ContractError, match="model_digest"):
        check_wiki_page(_page(build=wrong_build), job=job)
    no_questions = _job(open_questions=[])
    with pytest.raises(ContractError, match="open question"):
        check_wiki_page(_page_for(no_questions), job=no_questions)
    with pytest.raises(ContractError, match="memfw-wiki-job/1"):
        check_wiki_page(_page(), job={"format": "nope"})


class OverlapChecker:
    """Synthetic checker: share of hypothesis words (or CJK characters) in the premise."""

    name = "overlap"
    version = "overlap-1"

    def available(self) -> Availability:
        return Availability.ok()

    def score(self, premise: str, hypothesis: str) -> float:
        if not premise.strip() or not hypothesis.strip():
            raise ValueError("empty input")
        words = re.findall(r"[a-z0-9]+|[一-鿿]", hypothesis.lower())
        have = set(re.findall(r"[a-z0-9]+|[一-鿿]", premise.lower()))
        return sum(word in have for word in words) / len(words) if words else 0.0


def test_check_entailment_checker_passes_a_conforming_checker() -> None:
    checker = OverlapChecker()
    assert isinstance(checker, EntailmentChecker)
    scores = check_entailment_checker(checker)
    assert len(scores) == 4 and all(0.0 <= s <= 1.0 for s in scores)
    check_entailment_checker(
        checker,
        labelled=[("the job runs nightly", "the job runs", True), ("a b", "x y z", False)],
        threshold=0.5,
    )


def test_check_entailment_checker_refuses() -> None:
    class Bool(OverlapChecker):
        def score(self, premise: str, hypothesis: str) -> float:
            return True  # type: ignore[return-value]

    class Range(OverlapChecker):
        def score(self, premise: str, hypothesis: str) -> float:
            return 1.5

    class Flaky(OverlapChecker):
        calls = 0

        def score(self, premise: str, hypothesis: str) -> float:
            self.calls += 1
            return (self.calls % 2) / 2

    class Lenient(OverlapChecker):
        def score(self, premise: str, hypothesis: str) -> float:
            return 0.5

    class Unversioned(OverlapChecker):
        version = ""

    class Down(OverlapChecker):
        def available(self) -> Availability:
            return Availability.missing("model_missing")

    for bad in (Bool(), Range(), Flaky(), Lenient(), Unversioned(), Down(), object()):
        with pytest.raises(ContractError):
            check_entailment_checker(bad)
    with pytest.raises(ContractError, match="threshold"):
        check_entailment_checker(OverlapChecker(), labelled=[("a", "a", True)])
    with pytest.raises(ContractError, match="unsupported pair"):
        check_entailment_checker(OverlapChecker(), labelled=[("a b", "a b", False)], threshold=0.5)


# --- versions -------------------------------------------------------------------------


def test_versions() -> None:
    assert PORTS_API_VERSION == "0.4.0"
    assert PORT_VERSIONS["EntailmentChecker"] == 1
    for name in ("WikiPage", "WikiClaim", "EntailmentChecker", "parse_wiki_page"):
        assert name in ports.__all__


# --- review fixes (M1–M3, L1–L6) ------------------------------------------------------


@pytest.mark.parametrize("parse", [parse_wiki_page, parse_wiki_job])
@pytest.mark.parametrize(
    "text",
    [
        "[" * 20000 + "]" * 20000,
        '{"a":' * 5000 + "1" + "}" * 5000,
        "[" * 33 + "]" * 33,
    ],
)
def test_deep_nesting_is_a_format_error_not_a_crash(parse: Any, text: str) -> None:
    for data in (text, text.encode()):
        with pytest.raises(WikiFormatError) as info:
            parse(data)
        assert info.value.code == "too_deep"


def test_brackets_inside_strings_do_not_count_as_nesting() -> None:
    doc = _page()
    doc["sections"][0]["claims"][0]["text"] = "[" * 200 + "{" * 200
    assert parse_wiki_page(json.dumps(doc)).claims[0].text.startswith("[[[")


def test_duplicate_json_keys_are_refused() -> None:
    raw = json.dumps(_page()).replace(
        '"privacy": "shared", "build"', '"privacy": "public", "privacy": "shared", "build"', 1
    )
    assert _code(raw) == "format"
    job = json.dumps(_job()).replace('"tenant": ""', '"tenant": "x", "tenant": ""', 1)
    assert _job_code(job) == "format"


def test_wiki_ids_are_never_memory_refs() -> None:
    with pytest.raises(ValueError) as info:
        Citation.from_ref("mem:wiki-0123456789abcdef@v1")
    assert getattr(info.value, "code", None) == "wiki_as_memory"
    assert Citation("wiki-0123456789abcdef").memory_id  # the old form is untouched
    doc = _page()
    ref = "mem:wiki-0123456789abcdef@v1"
    doc["sources"][0]["ref"] = ref
    for section in doc["sections"]:
        for claim in section["claims"]:
            claim["cites"] = [
                ref if r == "mem:mem-20261101-backup@v3" else r for r in claim["cites"]
            ]
    assert _code(doc) == "wiki_as_memory"
    assert _code(_mutate(["sections", 0, "claims", 0, "cites"], [ref])) == "wiki_as_memory"


@pytest.mark.parametrize(
    "alias",
    [
        "kh:Infra%2FBackup.md#Schedule",
        "kh:Infra/Backup.md# Schedule ",
        "kh:Infra/Backup.md#",
        "kh:Infra/Backup%2Emd#Schedule",
        "kh:Infra/Backup.md#Sched ule",
        "wiki:wiki-0123456789abcdef#",
    ],
)
def test_ref_aliases_are_not_canonical(alias: str) -> None:
    with pytest.raises(ValueError) as info:
        Citation.from_ref(alias)
    assert getattr(info.value, "code", None) in ("not_canonical", "malformed")


def test_canonical_refs_pass_and_note_ref_encodes() -> None:
    from memfw.ports.refs import canonical_ref, note_ref

    for ref in (
        "kh:Infra/Backup.md#Schedule",
        "kh:Infra/Backup.md#Sched%20ule",
        "kh:备份/计划.md#时间",
        "mem:m-1@v3",
        f"att:{PRX}:2",
        "wiki:wiki-0123456789abcdef#facts",
    ):
        assert canonical_ref(ref) == ref
    assert canonical_ref("kh:%E5%A4%87.md") == "kh:备.md"  # CJK is kept, not encoded
    assert canonical_ref("conv:x") is None
    assert note_ref("a b.md", " H#1 ") == "kh:a%20b.md#H%231"
    assert Citation.from_ref(note_ref("备份/计划.md", "时间")).ref == "kh:备份/计划.md#时间"
    doc = _page()
    alias = dict(doc["sources"][1], ref="kh:Infra%2FBackup.md#Schedule")
    doc["sources"].append(alias)
    assert _code(doc) == "not_canonical"
    with pytest.raises(ValueError, match="version differs"):
        Citation("m", 5, ref="mem:m")


def test_check_view_citations_refuses_non_memory_citations() -> None:
    kh_only = DerivedView("concat", "list", "x", (Citation.from_ref("kh:a.md#H"),))
    with pytest.raises(ValueError, match="not a memory"):
        check_view_citations(kh_only, lambda memory_id: True)
    ok = DerivedView("concat", "list", "x", (Citation("m-1"), Citation.from_ref("mem:m-2@v1")))
    assert check_view_citations(ok, lambda memory_id: True) is ok


def test_timeline_citing_inactive_source_must_be_superseded_kind() -> None:
    doc = _mutate(["sections", 2, "claims", 0, "kind"], "statement")
    assert _code(doc) == "inactive_source_kind"
    current_only = _mutate(["sections", 2, "claims", 0, "cites"], ["mem:mem-20261101-backup@v3"])
    current_only["sections"][2]["claims"][0]["kind"] = "statement"
    assert parse_wiki_page(current_only).claims[2].kind == "statement"


def test_superseded_by() -> None:
    page = parse_wiki_page(_page())
    assert page.sources[3].superseded_by == "mem:mem-20261101-backup@v3"
    assert parse_wiki_page(page.canonical_bytes()) == page
    assert _schema_errors(WIKI_PAGE_SCHEMA, page.to_json(), WIKI_PAGE_SCHEMA) == []
    assert "superseded_by" not in page.sources[0].to_json()
    # Required for superseded (G6: say what replaced it), optional for retired.
    missing = _sources()
    del missing[3]["superseded_by"]
    assert _code(_page(sources=missing, built_from_digest="sha256:" + "0" * 64)) == "field"
    retired = dict(missing[3], status="retired")
    assert WikiSource(**retired).superseded_by is None
    assert WikiSource(**dict(retired, superseded_by="kh:a.md")).superseded_by == "kh:a.md"
    for value, status in (
        ("mem:mem-20261101-backup@v3", "current"),
        ("wiki:wiki-0123456789abcdef", "superseded"),
        ("mem:mem-20261001-old-backup@v1", "superseded"),
        ("conv:x", "superseded"),
    ):
        with pytest.raises(WikiFormatError):
            WikiSource(**dict(_sources()[3], status=status, superseded_by=value))


def test_job_digest_covers_every_source_field_and_the_concept() -> None:
    base = _job()["job_digest"]

    def with_source(**fields: Any) -> str:
        doc = _job()
        doc["sources"][0].update(fields)
        return _job(sources=doc["sources"])["job_digest"]

    assert with_source(status="superseded") != base
    assert with_source(privacy="public") != base
    assert with_source(causal="hypothesis") != base
    assert with_source(sha256=_sha("x")) != base
    sup = _job()
    sup["sources"][3]["superseded_by"] = "kh:Infra/Backup.md#Schedule"
    assert _job(sources=sup["sources"])["job_digest"] != base
    for key, value in (
        ("pref_label", "Renamed"),
        ("definition", "changed"),
        ("alt_labels", ["别名"]),
    ):
        concept = dict(_job()["concept"], **{key: value})
        assert _job(concept=concept)["job_digest"] != base
    assert _job(open_questions=[])["job_digest"] != base
    assert _job(view_version="view-20261121")["job_digest"] == base


def test_truncate_source_text_and_truncated_job_sources() -> None:
    from memfw.ports.wiki import MAX_JOB_SOURCE_CHARS, truncate_source_text

    assert truncate_source_text("short") == ("short", False, None)
    long_text = ("line\n" * 1700) + "tail"  # 8504 code points
    head, truncated, full = truncate_source_text(long_text)
    assert truncated and full == hashlib.sha256(long_text.encode()).hexdigest()
    assert len(head) <= MAX_JOB_SOURCE_CHARS and long_text.startswith(head)
    assert not head.endswith("\n") and head.endswith("line")
    no_newline = "备" * 9000
    assert truncate_source_text(no_newline)[0] == "备" * MAX_JOB_SOURCE_CHARS
    # A newline in the first half is not worth losing half the text: hard cut.
    early = "head\n" + "x" * 9000
    assert truncate_source_text(early)[0] == early[:MAX_JOB_SOURCE_CHARS]
    # A cut that would leave only blank text falls back to the hard cut.
    blank = "  \n" + "y" * 9000
    head_blank, cut_blank, _ = truncate_source_text(blank)
    assert cut_blank and head_blank == blank[:MAX_JOB_SOURCE_CHARS] and head_blank.strip()
    late = "z" * 6000 + "\n" + "w" * 3000
    assert truncate_source_text(late)[0] == "z" * 6000
    doc = _job()
    doc["sources"][0].update(text=head, truncated=True, full_sha256=full)
    job = _job(sources=doc["sources"])
    parsed = parse_wiki_job(job)
    assert parsed.sources[0].truncated and parsed.to_json() == job
    assert _schema_errors(WIKI_JOB_SCHEMA, job, WIKI_JOB_SCHEMA) == []
    assert job["job_digest"] != _job()["job_digest"]
    bad = _job()
    bad["sources"][0]["text"] = long_text
    assert _job_code(_job(sources=bad["sources"])) == "field"
    for fields in ({"truncated": False}, {"full_sha256": full}, {"truncated": True}):
        broken = _job()
        broken["sources"][0].update(fields)
        assert _job_code(_job(sources=broken["sources"])) == "field"


def test_previous_page_must_be_an_object() -> None:
    assert _job_code(_job(previous_page="{}")) == "field"
    assert _job_code(_job(previous_page=json.dumps(_page()))) == "field"


@pytest.mark.parametrize("char", ["‮", "​", "⁦", "﻿", " ", " ", "­"])
def test_format_and_separator_characters_are_refused(char: str) -> None:
    assert _code(_mutate(["sections", 0, "claims", 0, "text"], f"abc{char}def")) == "field"
    concept = dict(_job()["concept"], pref_label=f"Backup{char}plan")
    assert _job_code(_job(concept=concept, job_digest="sha256:" + "0" * 64)) == "field"


@pytest.mark.parametrize("text", ["👨\u200d👩\u200d👧 family", "می\u200cخواهم", "क्\u200dष"])
def test_zwj_and_zwnj_are_allowed(text: str) -> None:
    doc = _mutate(["sections", 0, "claims", 0, "text"], text)
    assert parse_wiki_page(doc).claims[0].text == text
    concept = dict(_job()["concept"], pref_label=text)
    assert parse_wiki_job(_job(concept=concept)).concept.pref_label == text


def test_errors_never_echo_page_text() -> None:
    with pytest.raises(WikiFormatError) as info:
        parse_wiki_page(_mutate(["sections", 0, "claims", 0, "text"], "SECRET" * 300))
    assert "SECRET" not in str(info.value)


def test_check_wiki_page_enforces_the_conflict_rule() -> None:
    job = _job()
    assert check_wiki_page(_page_for(job), job=job)
    doc = _page_for(job)
    doc["sections"][0]["claims"][0]["cites"].append("mem:mem-20261105-dispute@v2")
    with pytest.raises((ContractError, WikiFormatError)):
        check_wiki_page(doc, job=job)
    # A summary claim citing one side (a current one) is refused with the job.
    sides = _job()
    sides["open_questions"][0]["conflicts"] = ["mem-20261101-backup", "mem-20261104-other-time"]
    sided = _job(open_questions=sides["open_questions"])
    with pytest.raises(ContractError, match="outside open/timeline"):
        check_wiki_page(_page_for(sided), job=sided)
    both = _job()
    both["open_questions"][0]["conflicts"] = ["mem-20261105-dispute", "mem-20261101-backup"]
    both_job = _job(open_questions=both["open_questions"])
    page = _page_for(both_job)
    page["sections"][0]["claims"][0]["cites"] = ["kh:Infra/Backup.md#Schedule"]
    page["sections"][3]["claims"][0]["cites"] = ["kh:Infra/Backup.md#Schedule"]
    page["sections"][3]["claims"][0]["causal"] = "none"
    page["sections"][2]["claims"][0]["cites"] = ["mem:mem-20261001-old-backup@v1"]
    with pytest.raises(ContractError, match="both sides"):
        check_wiki_page(page, job=both_job)  # c6 cites the dispute and the backup


def test_conflict_side_in_relations_is_refused() -> None:
    sides = _job()
    sides["open_questions"][0]["conflicts"] = ["mem-20261101-backup", "mem-20261104-other-time"]
    job = _job(open_questions=sides["open_questions"])
    page = _page_for(job)
    # Keep the conflict side only in relations (c4) and timeline (c3, allowed).
    kh = "kh:Infra/Backup.md#Schedule"
    page["sections"][0]["claims"][0]["cites"] = [kh]
    page["sections"][4]["claims"][1]["cites"] = ["mem:mem-20261105-dispute@v2"]
    with pytest.raises(ContractError, match="outside open/timeline"):
        check_wiki_page(page, job=job)
    page["sections"][3]["claims"][0].update(cites=[kh], causal="none")
    assert check_wiki_page(page, job=job)  # timeline may still cite it


def test_self_declared_public_page_fails_against_its_job() -> None:
    job = _job()
    doc = _page_for(job)
    for source in doc["sources"]:
        source["privacy"] = "public"
    doc["privacy"] = "public"
    with pytest.raises(ContractError, match="source fields differ"):
        check_wiki_page(doc, job=job)
