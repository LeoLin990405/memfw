"""M3a: the DeepSeek adapter (``packages/adapters/memfw-adapter-deepseek``) and conv-v2 tags.

No test here reaches the network: every request goes to the adapter's own
loopback fake (``memfw_adapter_deepseek.fake.FakeDeepSeek``), and the keys are
made-up test strings.
"""

from __future__ import annotations

import ast
import json
import logging
import pickle
import socket
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw.cli import app as cli_app
from memfw.conversations import SharePolicy
from memfw.conversations.adapter import SqliteStoreSource
from memfw.conversations.backends import (
    ExtractiveBackend,
    RefusedBackend,
    backend_status,
    configured_backend,
    load_backend,
    parse_reply,
    reply_issue,
)
from memfw.conversations.worker import Budgets, ConversationWorker, WorkState
from memfw.dream.claims import DreamClaimStager, DreamClaimVerifier
from memfw.ports.models import (
    TAG_INTENT_CHARS,
    TAG_ITEM_CHARS,
    TAG_MAX_ITEMS,
    TAG_PROMPT_VERSION,
    TAG_SUMMARY_CHARS,
    BackendBudgetExhausted,
    BackendUnavailable,
    ModelReply,
    render_tag_prompt,
)
from memfw.ports.testing import ContractError, check_model_backend
from tests.conversation_fixtures import (
    EMAIL,
    SECRET,
    isolated_home,  # noqa: F401  (fixture)
    make_vault,
    set_config,
    standard_rows,
    turn,
    write_store,
)

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "packages" / "adapters" / "memfw-adapter-deepseek"
ADAPTER_SRC = ADAPTER / "src"
if str(ADAPTER_SRC) not in sys.path:  # the adapter is a workspace member, not installed here
    sys.path.insert(0, str(ADAPTER_SRC))

from memfw_adapter_deepseek import (  # noqa: E402
    KEY_ENV,
    DeepSeekBackend,
    DeepSeekError,
    Settings,
    factory,
)
from memfw_adapter_deepseek.backend import estimate_tokens  # noqa: E402
from memfw_adapter_deepseek.fake import FakeDeepSeek, tag_reply  # noqa: E402

NOW = datetime(2026, 1, 6, 12, 0, tzinfo=UTC)
# Made-up, test-only key; checked for in every output below.
TEST_KEY = "sk-" + "unit-test-key-0000-not-real"
SPEC = "memfw_adapter_deepseek:factory"
HIDDEN = "zebra-hidden-marker-41"
OPTED = "okapi-optout-marker-73"


_CONNECT = socket.socket.connect
_CONNECT_EX = socket.socket.connect_ex
_CREATE = socket.create_connection
_GETADDRINFO = socket.getaddrinfo
_LOOPBACK = {"127.0.0.1", "localhost"}


@pytest.fixture(autouse=True)
def _home(isolated_home: Path) -> Path:  # noqa: F811
    return isolated_home


@pytest.fixture(autouse=True)
def loopback_only(disable_network: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Narrow the suite's no-network guard to the fake server: loopback only."""

    def host_of(address: Any) -> str:
        return str(address[0]) if isinstance(address, tuple) and address else str(address)

    def guard(real: Any, address_index: int) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            if host_of(args[address_index]) not in _LOOPBACK:
                raise AssertionError("Tests must not open network sockets")
            return real(*args, **kwargs)

        return call

    monkeypatch.setattr(socket.socket, "connect", guard(_CONNECT, 1))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(_CONNECT_EX, 1))
    monkeypatch.setattr(socket, "create_connection", guard(_CREATE, 0))
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, *a, **k: guard(_GETADDRINFO, 0)(host, *a, **k)
    )


@pytest.fixture
def fake() -> Any:
    with FakeDeepSeek() as server:
        yield server


def _backend(fake: FakeDeepSeek, **settings: Any) -> DeepSeekBackend:
    sleeps: list[float] = []
    backend = DeepSeekBackend(
        Settings(base_url=fake.url, **settings), key=TEST_KEY, sleep=sleeps.append
    )
    backend.sleeps = sleeps  # type: ignore[attr-defined]
    return backend


def _rows() -> list[dict[str, Any]]:
    day = "2026-01-05T11:0"
    return [
        *standard_rows(),
        turn("stepfun", "s-delta", 0, "user", f"Do not send {HIDDEN} anywhere.", day + "0:00Z"),
        turn("stepfun", "s-delta", 1, "assistant", f"Kept {HIDDEN} local.", day + "1:00Z"),
        turn("private", "s-eps", 0, "user", f"Opted out: {OPTED}.", day + "2:00Z"),
        turn("private", "s-eps", 1, "assistant", f"Still {OPTED}.", day + "3:00Z"),
    ]


POLICY = {
    "conversation_share": {"apps": {"stepfun": {"process": False}}},
    "conversation_optout": {"agents": ["private"]},
}


def _worker(
    tmp_path: Path,
    backend: Any,
    *,
    config: dict[str, Any] | None = None,
    vault: Path | None = None,
    now: datetime = NOW,
    backfill_days: float | None = None,
    replan_limit: int | None = None,
    project_vocab: dict[str, tuple[str, ...]] | None = None,
    **budgets: Any,
) -> ConversationWorker:
    store = tmp_path / "data" / "conversations.sqlite"
    if not store.exists():
        write_store(store, _rows())
    return ConversationWorker(
        SqliteStoreSource(store),
        WorkState(tmp_path / "data" / "work.sqlite"),
        backend=backend,
        policy=SharePolicy.from_config(POLICY if config is None else config),
        budgets=Budgets(**budgets),
        vault=vault,
        verifier=DreamClaimVerifier(),
        stager=DreamClaimStager(),
        now=lambda: now,
        backfill_days=backfill_days,
        replan_limit=replan_limit,
        project_vocab=project_vocab,
    )


def _rows_of(tmp_path: Path, sql: str, *args: Any) -> list[dict[str, Any]]:
    with sqlite3.connect(tmp_path / "data" / "work.sqlite") as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, args)]


# -- the ModelBackend contract, shared by the built-in and the adapter --------------------


def test_contract_holds_for_extractive_and_deepseek(fake: FakeDeepSeek) -> None:
    assert len(check_model_backend(ExtractiveBackend())) == 3
    replies = check_model_backend(_backend(fake))
    assert len(replies) == 2 and len(fake.requests) == 2  # memory_claims refused, never sent
    assert all(isinstance(reply, ModelReply) for reply in replies)


def test_contract_catches_a_broken_tag_backend(fake: FakeDeepSeek) -> None:
    fake.default = tag_reply(tags={"task_type": "nonsense"})
    with pytest.raises(ContractError, match="tag schema"):
        check_model_backend(_backend(fake))

    class Liar(ExtractiveBackend):
        tasks = ("chunk_summary",)  # says it refuses session_summary but answers it

    with pytest.raises(ContractError, match="does not accept"):
        check_model_backend(Liar())


# -- request shape --------------------------------------------------------------------


def test_request_shape_is_exactly_what_is_documented(fake: FakeDeepSeek) -> None:
    data = {"turns": [{"role": "user", "text": "Fix the retry test."}]}
    backend = _backend(fake)
    backend.run("chunk_summary", data, max_tokens=1024)
    (request,) = fake.requests
    system, user = render_tag_prompt("chunk_summary", data)
    assert request["path"] == "/chat/completions"
    assert request["body"] == {
        "model": "deepseek-flash",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "max_tokens": 512,
        "temperature": 0,
        "stream": False,
    }
    assert "json" in system.lower()  # JSON mode needs the word in the prompt
    headers = request["headers"]
    assert headers["authorization"] == f"Bearer {TEST_KEY}"
    assert headers["content-type"] == "application/json"
    assert headers["user-agent"].startswith("memfw-adapter-deepseek/")
    assert backend.usage() == {
        "requests": 1,
        "tokens_in": 120,
        "tokens_out": 40,
        "cost_usd": round((120 * 0.28 + 40 * 0.42) / 1_000_000, 6),
        "stopped": "",
    }
    with pytest.raises(ValueError):
        backend.run("memory_claims", {"summary": "x"}, max_tokens=10)
    assert len(fake.requests) == 1


# -- factory, key handling ----------------------------------------------------------------


def test_factory_reads_the_key_from_the_environment_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    (tmp_path / ".deepseek_key").write_text(TEST_KEY)  # a key file is never read
    monkeypatch.chdir(tmp_path)
    with pytest.raises(BackendUnavailable) as missing:
        factory()
    assert missing.value.code == "missing_key"
    monkeypatch.setenv(KEY_ENV, "   ")
    with pytest.raises(BackendUnavailable):
        factory()
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    backend = factory({"model": "deepseek-reasoner", "max_run_cost_usd": 0.1})
    assert backend.name == "deepseek:deepseek-reasoner" and backend.local is False
    assert backend.prompt_version == TAG_PROMPT_VERSION
    assert backend.settings.max_run_cost_usd == 0.1


@pytest.mark.parametrize(
    ("options", "code"),
    [
        ({"api_key": TEST_KEY}, "key_in_config"),
        ({"API_KEY": TEST_KEY}, "key_in_config"),
        ({"token": TEST_KEY}, "key_in_config"),
        ({"base_url": "http://api.example.com"}, "bad_base_url"),
        ({"base_url": "https://api.example.com"}, "bad_base_url"),
        ({"base_url": "https://api.deepseek.com.example.com"}, "bad_base_url"),
        ({"base_url": "http://api.deepseek.com"}, "bad_base_url"),
        ({"base_url": "https://user:pw@api.example.com"}, "bad_base_url"),
        ({"base_url": "https://api.example.com/?k=1"}, "bad_base_url"),
        ({"nonsense": 1}, "bad_option"),
        ({"timeout": "slow"}, "bad_option"),
        ({"max_run_tokens": -1}, "bad_option"),
        ({"max_retries": True}, "bad_option"),
    ],
)
def test_factory_refuses_bad_options(
    monkeypatch: pytest.MonkeyPatch, options: dict[str, Any], code: str
) -> None:
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    with pytest.raises(BackendUnavailable) as refused:
        factory(options)
    assert refused.value.code == code
    assert TEST_KEY not in str(refused.value) and TEST_KEY not in repr(refused.value)


def test_key_never_in_repr_errors_or_logs(
    fake: FakeDeepSeek, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    backend = _backend(fake, max_retries=1)
    texts = [repr(backend), str(backend), repr(vars(backend)), repr(backend._key)]
    with pytest.raises(TypeError):
        pickle.dumps(backend)
    fake.script = [(500, {"error": TEST_KEY[:3]}), (500, {})]
    with pytest.raises(BackendUnavailable) as failed:
        backend.run("chunk_summary", {"turns": []}, max_tokens=10)
    texts += [str(failed.value), repr(failed.value), repr(failed.value.__cause__)]
    assert failed.value.__cause__ is None and failed.value.__context__ is None
    fake.script = [(401, {"error": "bad key"})]
    with pytest.raises(BackendUnavailable) as auth:
        backend.run("chunk_summary", {"turns": []}, max_tokens=10)
    assert auth.value.code == "auth" and auth.value.__cause__ is None
    texts += [str(auth.value), repr(auth.value), caplog.text, *capsys.readouterr()]
    assert all(TEST_KEY not in text for text in texts), texts


# -- retries and budgets ---------------------------------------------------------------


def test_retries_429_and_5xx_with_backoff(fake: FakeDeepSeek) -> None:
    fake.script = [(429, {}), (503, {}), (500, {}), (200, tag_reply())]
    backend = _backend(fake)
    reply = backend.run("chunk_summary", {"turns": []}, max_tokens=64)
    assert json.loads(reply.text)["tags"]["task_type"] == "dev"
    assert len(fake.requests) == 4 and backend.sleeps == [1.0, 2.0, 4.0]  # type: ignore[attr-defined]


def test_retries_give_up_and_other_4xx_are_not_retried(fake: FakeDeepSeek) -> None:
    fake.script = [(502, {})] * 4
    backend = _backend(fake, max_retries=3, max_backoff=3.0)
    with pytest.raises(BackendUnavailable) as exhausted:  # a stop, not a job failure
        backend.run("chunk_summary", {"turns": []}, max_tokens=64)
    assert exhausted.value.code == "transient"
    assert len(fake.requests) == 4 and backend.sleeps == [1.0, 2.0, 3.0]  # type: ignore[attr-defined]
    fake.script = [(400, {})]
    with pytest.raises(DeepSeekError, match="http_400"):
        backend.run("chunk_summary", {"turns": []}, max_tokens=64)
    assert len(fake.requests) == 5
    fake.script = [(200, b"not json")]
    with pytest.raises(DeepSeekError, match="bad_response"):
        backend.run("chunk_summary", {"turns": []}, max_tokens=64)


def test_token_and_cost_budgets_stop_before_sending(fake: FakeDeepSeek) -> None:
    with pytest.raises(BackendBudgetExhausted, match="backend_tokens"):
        _backend(fake, max_run_tokens=10).run("chunk_summary", {"turns": []}, max_tokens=64)
    with pytest.raises(BackendBudgetExhausted, match="backend_cost"):
        _backend(fake, max_run_cost_usd=0.0).run("chunk_summary", {"turns": []}, max_tokens=64)
    assert fake.requests == []
    # Room for one worst case (prompt estimate + 64 out), not two once usage is charged.
    system, user = render_tag_prompt("chunk_summary", {"turns": []})
    reserve = estimate_tokens(system + user) + 64
    fake.default = tag_reply(tokens_in=400, tokens_out=40)
    backend = _backend(fake, max_run_tokens=reserve + 100)
    backend.run("chunk_summary", {"turns": []}, max_tokens=64)
    with pytest.raises(BackendBudgetExhausted):
        backend.run("chunk_summary", {"turns": []}, max_tokens=64)
    assert len(fake.requests) == 1 and backend.usage()["stopped"] == "backend_tokens"


# -- conv-v2 parsing: validate, normalise, redact again ---------------------------------


def _v2(summary: str = "Fixed it.", **tags: Any) -> str:
    return json.dumps({"summary": summary, "tags": tags})


def test_parse_conv_v2_normalises_and_redacts() -> None:
    parsed = parse_reply(
        "session_summary",
        _v2(
            "Fixed it.",
            project=["MemFW", "memfw", "  Big   Project "],
            task_type="Debug",
            components=["Worker", "x" * 100],
            intent="Fix The Worker",
            outcome="DONE",
        ),
        prompt_version=TAG_PROMPT_VERSION,
    )
    assert parsed == {
        "summary": "Fixed it.",
        "tags": {
            "project": ["memfw", "big project"],
            "task_type": "debug",
            "components": ["worker", "x" * 48],
            "intent": "fix the worker",
            "outcome": "done",
        },
        "intent": "fix the worker",
        "outcome": "success",
    }
    # An empty tags object omits every key. Rev 32 fills the defaults and keeps
    # the summary. Rev 31 rejected it (missing_tag_key).
    assert parse_reply("chunk_summary", _v2(), prompt_version=TAG_PROMPT_VERSION) == {
        "summary": "Fixed it.",
        "tags": {
            "project": [],
            "task_type": "other",
            "components": [],
            "intent": "",
            "outcome": "unknown",
        },
    }
    filled = parse_reply(
        "chunk_summary",
        _v2(project=[], task_type="other", components=[], intent="", outcome="unknown"),
        prompt_version=TAG_PROMPT_VERSION,
    )
    assert filled == {
        "summary": "Fixed it.",
        "tags": {
            "project": [],
            "task_type": "other",
            "components": [],
            "intent": "",
            "outcome": "unknown",
        },
    }
    redacted = parse_reply(
        "chunk_summary",
        _v2(
            f"Mailed {EMAIL}.",
            project=["uploader"],
            task_type="dev",
            components=[EMAIL],
            intent="mail the result",
            outcome="done",
        ),
        prompt_version="conv-v2",
    )
    assert redacted is not None and EMAIL not in json.dumps(redacted)


@pytest.mark.parametrize(
    "reply",
    [
        _v2(task_type="coding"),
        _v2(outcome="success"),
        _v2(extra="x"),
        _v2(project=[1]),
        _v2(intent=3),
        _v2("x" * 801),
        _v2(""),
        _v2(SECRET),
        _v2(components=[SECRET]),
        _v2(intent=f"store {SECRET}"),
        json.dumps({"summary": "s", "tags": {}, "intent": "x"}),
        json.dumps({"summary": "s"}),
        "not json",
    ],
)
def test_parse_conv_v2_drops_invalid_replies(reply: str) -> None:
    assert parse_reply("session_summary", reply, prompt_version=TAG_PROMPT_VERSION) is None


AWS = "AKIA" + "IOSFODNN7EXAMPLE"
PEM = "-----BEGIN RSA " + "PRIVATE KEY-----"
JWT = (
    "eyJhbGciOiJIUzI1NiJ9."
    + "eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)


@pytest.mark.parametrize("secret", [AWS, PEM, JWT, SECRET])
@pytest.mark.parametrize("where", ["summary", "project", "components", "intent"])
def test_secrets_are_caught_before_tags_are_lowercased(secret: str, where: str) -> None:
    value = f"see {secret}"
    if where == "summary":
        reply = _v2(value)
    elif where == "intent":
        reply = _v2(intent=value)
    else:
        reply = _v2(**{where: [value]})
    assert parse_reply("session_summary", reply, prompt_version=TAG_PROMPT_VERSION) is None


def test_home_paths_are_redacted_before_lowercasing() -> None:
    path = "/Users/" + "someone/proj/x.py"
    parsed = parse_reply(
        "session_summary",
        _v2(
            f"Edited {path}.",
            project=["uploader"],
            task_type="dev",
            components=["worker"],
            intent=f"edit {path}",
            outcome="done",
        ),
        prompt_version="conv-v2",
    )
    assert parsed is not None and "someone" not in json.dumps(parsed)


@pytest.mark.parametrize(
    "item",
    [
        "https://evil.example.com/x",
        "ftp://host/x",
        "www.example.com",
        "/etc/passwd",
        "~/notes",
        "C:\\work",
        "\\\\share\\x",
        "x.com/a",
        "evil.example.com/login",
        "file:secrets",
        "mailto:someone",
        "..\\up",
        "../up",
        "a\\b",
        "src/memfw",
        "go to https://e.example now",
    ],
)
def test_urls_and_paths_reject_the_reply(item: str) -> None:
    base = {
        "project": ["uploader"],
        "task_type": "dev",
        "components": ["retry"],
        "intent": "fix it",
        "outcome": "done",
    }
    for key in ("project", "components"):
        tags = dict(base)
        tags[key] = ["uploader", item, "retry"]
        parsed = parse_reply("chunk_summary", _v2("Fixed it.", **tags), prompt_version="conv-v2")
        assert parsed is None, (key, item)
    names = ["uploader", "next.js", "c++", "retry worker"]
    ok = parse_reply(
        "chunk_summary",
        _v2(
            "Fixed it.",
            project=["uploader"],
            task_type="dev",
            components=names,
            intent="fix it",
            outcome="done",
        ),
        prompt_version="conv-v2",
    )
    assert ok is not None and ok["tags"]["components"] == names


def test_a_path_like_name_is_an_output_code(tmp_path: Path, fake: FakeDeepSeek) -> None:
    fake.default = tag_reply(
        tags={
            "project": ["src/memfw", "some-user/memfw"],
            "task_type": "dev",
            "components": ["Worker"],
            "intent": "rename a module",
            "outcome": "done",
        }
    )
    report = _worker(tmp_path, _backend(fake)).run()
    assert report.errors == {"item_looks_like_url": 3}
    assert _rows_of(tmp_path, "SELECT * FROM summaries") == []
    stored = json.dumps(_rows_of(tmp_path, "SELECT error FROM jobs"))
    assert "src/memfw" not in stored and "item_looks_like_url" in stored


def test_a_secret_in_a_tag_still_drops_the_whole_reply() -> None:
    reply = _v2(project=["memfw", f"key {AWS}"], components=["/Users/" + "someone/x"])
    assert parse_reply("session_summary", reply, prompt_version="conv-v2") is None


def test_conv_v1_parsing_is_unchanged() -> None:
    assert parse_reply("chunk_summary", '{"summary": "ok"}') == {"summary": "ok"}
    assert parse_reply("chunk_summary", _v2()) is None  # tags are not conv-v1
    assert parse_reply("memory_claims", '{"claims": []}', prompt_version="conv-v2") == {
        "claims": []
    }


# -- the worker with the adapter ---------------------------------------------------------


def test_worker_sends_only_redacted_text_of_processable_apps(
    tmp_path: Path, fake: FakeDeepSeek
) -> None:
    report = _worker(tmp_path, _backend(fake)).run()
    assert report.status == "ok" and report.backend == "deepseek:deepseek-flash"
    bodies = fake.bodies()
    assert bodies and "uploader" in bodies.lower()
    for raw in (EMAIL, SECRET, "correct-horse", HIDDEN, OPTED, TEST_KEY):
        assert raw not in bodies, raw
    assert "[REDACTED:email]" in bodies
    sent = [json.loads(item["body"]["messages"][1]["content"][7:-9]) for item in fake.requests]
    assert all(set(item) in ({"turns"}, {"first_user", "chunks"}) for item in sent)
    # stepfun (process: false) and private (opted out) are never queued, let alone sent.
    apps = {row["app"] for row in _rows_of(tmp_path, "SELECT DISTINCT app FROM jobs")}
    assert apps == {"claude", "codex", "cursor"}
    # One chunk_summary and one session_summary per session; memory_claims stays local.
    assert len(fake.requests) == 6
    assert all("memory" not in item["raw"].lower() for item in fake.requests)


def test_worker_writes_conv_v2_tags_and_episodes(tmp_path: Path, fake: FakeDeepSeek) -> None:
    fake.default = tag_reply(
        "Added a retry wrapper.",
        {
            "project": ["Uploader"],
            "task_type": "DEV",
            "components": ["Retry"],
            "intent": "Add a retry wrapper",
            "outcome": "done",
        },
    )
    worker = _worker(tmp_path, _backend(fake), vault=make_vault(tmp_path / "vault"))
    assert isinstance(worker.claims_backend, ExtractiveBackend)  # claims stay extractive
    report = worker.run()
    assert report.jobs["memory_claims:done"] == 3 and report.jobs["episode:done"] == 3
    assert len(fake.requests) == 6  # chunk + session per session, no memory_claims call
    rows = _rows_of(
        tmp_path,
        "SELECT * FROM summaries WHERE app = 'codex' ORDER BY chunk_start",
    )
    assert [row["prompt_version"] for row in rows] == ["conv-v2", "conv-v2"]
    assert rows[0]["chunk_start"] == -1 and rows[0]["intent"] == "add a retry wrapper"
    assert rows[0]["outcome"] == "success" and rows[0]["model"] == "deepseek:deepseek-flash"
    assert json.loads(rows[0]["tags"]) == {
        "components": ["retry"],
        "intent": "add a retry wrapper",
        "outcome": "done",
        "project": ["uploader"],
        "task_type": "dev",
    }
    episodes = _rows_of(tmp_path, "SELECT outcome, intent FROM episodes WHERE app = 'codex'")
    assert episodes == [{"outcome": "success", "intent": "add a retry wrapper"}]
    jobs = _rows_of(tmp_path, "SELECT DISTINCT prompt_version FROM jobs")
    assert jobs == [{"prompt_version": "conv-v2"}]


def test_invalid_output_is_not_written_and_retried_next_run(
    tmp_path: Path, fake: FakeDeepSeek
) -> None:
    fake.default = tag_reply(content="{not json")
    report = _worker(tmp_path, _backend(fake)).run()
    assert report.errors == {"not_json": 3}
    assert _rows_of(tmp_path, "SELECT * FROM summaries") == []
    pending = _rows_of(tmp_path, "SELECT * FROM jobs WHERE kind = 'chunk_summary'")
    assert {row["status"] for row in pending} == {"pending"}
    fake.default = tag_reply()
    report = _worker(tmp_path, _backend(fake)).run()
    assert report.jobs["chunk_summary:done"] == 3 and report.jobs["session_summary:done"] == 3


def test_a_secret_in_model_output_drops_the_reply(tmp_path: Path, fake: FakeDeepSeek) -> None:
    fake.default = tag_reply(f"Saved {SECRET}.")
    report = _worker(tmp_path, _backend(fake)).run()
    assert report.errors == {"bad_output": 3}
    assert _rows_of(tmp_path, "SELECT * FROM summaries") == []


def test_backend_budget_stops_the_run_and_is_recorded(tmp_path: Path, fake: FakeDeepSeek) -> None:
    fake.default = tag_reply(tokens_in=500, tokens_out=40)
    report = _worker(tmp_path, _backend(fake, max_run_tokens=1500)).run()
    assert report.stopped == "backend_budget" and report.status == "ok"
    assert 0 < len(fake.requests) < 6
    recorded = _rows_of(tmp_path, "SELECT report FROM runs")
    assert json.loads(recorded[0]["report"])["stopped"] == "backend_budget"
    waiting = _rows_of(tmp_path, "SELECT attempts FROM jobs WHERE status = 'pending'")
    assert waiting and all(row["attempts"] == 0 for row in waiting)  # no attempt spent
    # The stopped run already stored the newest session's summary (finish-started).
    # This run must drain the other two. The old page loop stored none on the
    # stopped run, so its counter here was 3.
    report = _worker(tmp_path, _backend(fake)).run()
    assert report.stopped == "" and report.jobs["session_summary:done"] == 2
    done = _rows_of(
        tmp_path, "SELECT status FROM jobs WHERE kind = 'session_summary'"
    )
    assert len(done) == 3 and {row["status"] for row in done} == {"done"}


def test_rejected_key_degrades_the_run(tmp_path: Path, fake: FakeDeepSeek) -> None:
    fake.default = {"error": {"message": "auth"}}
    fake.script = [(401, {})]
    report = _worker(tmp_path, _backend(fake)).run()
    assert report.status == "degraded" and report.stopped == "backend_unavailable"
    assert len(fake.requests) == 1
    assert TEST_KEY not in json.dumps(_rows_of(tmp_path, "SELECT * FROM runs"))


def test_new_prompt_version_does_not_backfill_history_by_default(
    tmp_path: Path, fake: FakeDeepSeek
) -> None:
    first = _worker(tmp_path, ExtractiveBackend()).run()
    assert first.sessions_queued == 3
    later = NOW + timedelta(days=60)  # the three sessions are now 61 days old
    store = tmp_path / "data" / "conversations.sqlite"
    fresh = [
        turn("codex", "s-new", 0, "user", "Tag the new session first.", "2026-03-06T10:00:00Z"),
        turn("codex", "s-new", 1, "assistant", "Tagged.", "2026-03-06T10:01:00Z"),
    ]
    write_store(store, [*_rows(), *fresh])
    second = _worker(tmp_path, _backend(fake), now=later).run()
    # Only the new session is planned: unchanged history is not re-processed.
    assert second.sessions_queued == 1 and second.jobs["session_summary:done"] == 1
    assert len(fake.requests) == 2
    assert "tag the new session" in fake.requests[0]["raw"].lower()
    third = _worker(tmp_path, _backend(fake), now=later).run()
    assert third.sessions_queued == 0 and len(fake.requests) == 2
    versions = _rows_of(
        tmp_path,
        "SELECT prompt_version, COUNT(*) AS n, COUNT(tags) AS tagged FROM summaries"
        " GROUP BY prompt_version ORDER BY prompt_version",
    )
    assert versions == [
        {"prompt_version": "conv-v1", "n": 6, "tagged": 0},
        {"prompt_version": "conv-v2", "n": 2, "tagged": 2},
    ]


def test_backfill_is_opt_in_and_runs_newest_sessions_first(
    tmp_path: Path, fake: FakeDeepSeek
) -> None:
    _worker(tmp_path, ExtractiveBackend()).run()
    later = NOW + timedelta(days=60)
    report = _worker(tmp_path, _backend(fake), now=later, backfill_days=90).run()
    assert report.sessions_queued == 3 and report.jobs["session_summary:done"] == 3
    # Newest session first: cursor s-gamma (10:06), claude s-beta (10:05), codex s-alpha.
    order = [json.loads(item["body"]["messages"][1]["content"][7:-9]) for item in fake.requests]
    texts = [json.dumps(item) for item in order if "turns" in item]
    assert "for cursor" in texts[0] and "REDACTED:email" in texts[1]
    assert "retry wrapper" in texts[-1]
    back = _worker(tmp_path, ExtractiveBackend(), now=later).run()  # back to v1: nothing redone
    assert back.sessions_queued == 3 and back.jobs.get("chunk_summary:done", 0) == 0
    with pytest.raises(Exception, match="backfill_days"):
        _worker(tmp_path, _backend(fake), backfill_days=-1)


def _long_session(app: str, session: str, turns: int, start: int = 0) -> list[dict[str, Any]]:
    body = "Refactor the retry loop and keep the tests green. " * 80  # ~4000 chars
    return [
        turn(
            app,
            session,
            offset,
            "user" if offset % 2 == 0 else "assistant",
            body,
            f"2026-01-05T09:{offset:02d}:00Z",
        )
        for offset in range(start, start + turns)
    ]


def test_resumed_sessions_never_stall_newest_first_order(
    tmp_path: Path, fake: FakeDeepSeek
) -> None:
    """20 sessions of 2 chunks, a budget-stopped run, a new turn in each: every later run
    makes progress until the queue is drained (no page of only-waiting jobs stops it)."""

    store = tmp_path / "data" / "conversations.sqlite"
    sessions = [f"s-{index:02d}" for index in range(20)]
    rows = [row for name in sessions for row in _long_session("codex", name, 10)]
    write_store(store, rows)
    big = {"max_tokens": 10**8, "max_sessions": 100}
    fake.default = tag_reply(tokens_in=11_000)  # each chunk call is charged ~11k tokens
    first = _worker(tmp_path, _backend(fake, max_run_tokens=120_000), config={}, **big).run()
    assert first.stopped == "backend_budget" and 0 < len(fake.requests) < 40
    write_store(store, [*rows, *(r for n in sessions for r in _long_session("codex", n, 1, 10))])
    fake.default = tag_reply()
    for _ in range(8):
        before = len(fake.requests)
        report = _worker(tmp_path, _backend(fake), config={}, **big).run()
        pending = _rows_of(tmp_path, "SELECT COUNT(*) AS n FROM jobs WHERE status = 'pending'")
        if pending[0]["n"] == 0:
            break
        assert len(fake.requests) > before, report.as_dict()  # never a run that does nothing
    done = _rows_of(
        tmp_path,
        "SELECT COUNT(*) AS n FROM jobs WHERE kind = 'session_summary' AND status = 'done'"
        " AND fingerprint LIKE '11:%'",
    )
    assert done[0]["n"] == 20
    # A reused chunk sorts with its session's newest jobs.
    spread = _rows_of(
        tmp_path,
        "SELECT session_id, COUNT(DISTINCT session_ts) AS n FROM jobs"
        " WHERE fingerprint LIKE '11:%' GROUP BY session_id",
    )
    assert {row["n"] for row in spread} == {1}


def test_replanning_history_is_capped_newest_first(
    tmp_path: Path, fake: FakeDeepSeek, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memfw.conversations import worker as worker_module

    _worker(tmp_path, ExtractiveBackend()).run()
    monkeypatch.setattr(worker_module, "DEFAULT_REPLAN_LIMIT", 2)
    report = _worker(tmp_path, _backend(fake)).run()
    # Default cap (2 here, 1000 in production): the newest two of the three sessions.
    assert report.sessions_queued == 2 and report.as_dict()["replan_held_back"] == 1
    planned = {
        row["session_id"]
        for row in _rows_of(
            tmp_path, "SELECT DISTINCT session_id FROM jobs WHERE prompt_version = 'conv-v2'"
        )
    }
    assert planned == {"s-gamma", "s-beta"}  # cursor (10:06) and claude (10:05)
    again = _worker(tmp_path, _backend(fake)).run()
    assert again.sessions_queued == 0 and again.as_dict()["replan_held_back"] == 1
    # A new session is always planned, even with the cap at 0.
    store = tmp_path / "data" / "conversations.sqlite"
    fresh = [turn("codex", "s-new", 0, "user", "Brand new work.", "2026-01-05T11:30:00Z")]
    write_store(store, [*_rows(), *fresh])
    capped = _worker(tmp_path, _backend(fake), replan_limit=0).run()
    assert capped.sessions_queued == 1 and capped.as_dict()["replan_held_back"] == 1
    # An explicit backfill lifts the default cap.
    lifted = _worker(tmp_path, _backend(fake), backfill_days=30).run()
    assert lifted.sessions_queued == 1 and "replan_held_back" not in lifted.as_dict()
    with pytest.raises(Exception, match="replan_limit"):
        _worker(tmp_path, _backend(fake), replan_limit=-1)


def test_replan_limit_from_vault_yaml() -> None:
    from memfw.conversations.backends import configured_replan_limit

    assert configured_replan_limit({}) is None
    assert configured_replan_limit({"conversations": {"replan_limit": 5000}}) == 5000
    for bad in (-1, 1.5, True, "all"):
        with pytest.raises(Exception, match="replan_limit"):
            configured_replan_limit({"conversations": {"replan_limit": bad}})


def test_backfill_days_from_vault_yaml(tmp_path: Path) -> None:
    from memfw.conversations.backends import configured_backfill_days

    assert configured_backfill_days({}) is None
    assert configured_backfill_days({"conversations": {"backfill_days": 365}}) == 365.0
    with pytest.raises(Exception, match="backfill_days"):
        configured_backfill_days({"conversations": {"backfill_days": "all"}})


def test_transient_outage_stops_the_run_without_burning_attempts(
    tmp_path: Path, fake: FakeDeepSeek
) -> None:
    fake.default_status, fake.default = 503, {"error": "overloaded"}
    for _ in range(5):  # more runs than MAX_ATTEMPTS: an outage never fails a job
        report = _worker(tmp_path, _backend(fake, max_retries=1)).run()
        assert report.status == "degraded" and report.stopped == "backend_unavailable"
        assert report.errors == {"backend_transient": 1}
    jobs = _rows_of(tmp_path, "SELECT status, attempts FROM jobs WHERE kind = 'chunk_summary'")
    assert {row["status"] for row in jobs} == {"pending"}
    assert {row["attempts"] for row in jobs} == {0}
    fake.default_status, fake.default = 200, tag_reply()  # the provider recovers
    report = _worker(tmp_path, _backend(fake)).run()
    assert report.status == "ok" and report.jobs["session_summary:done"] == 3
    assert report.jobs["episode:done"] == 3


def test_deadline_bounds_attempts_and_backoff(fake: FakeDeepSeek) -> None:
    now = [100.0]
    backend = DeepSeekBackend(
        Settings(base_url=fake.url), key=TEST_KEY, sleep=lambda s: None, clock=lambda: now[0]
    )
    backend.limit_time(0)
    with pytest.raises(BackendUnavailable) as stopped:
        backend.run("chunk_summary", {"turns": []}, max_tokens=64)
    assert stopped.value.code == "deadline" and fake.requests == []
    fake.script = [(503, {})]
    backend.limit_time(0.5)  # the first backoff (1 s) would overrun it
    with pytest.raises(BackendUnavailable) as stopped:
        backend.run("chunk_summary", {"turns": []}, max_tokens=64)
    assert stopped.value.code == "deadline" and len(fake.requests) == 1


def test_worker_hands_the_backend_its_remaining_time(tmp_path: Path, fake: FakeDeepSeek) -> None:
    backend = _backend(fake)
    seen: list[float] = []
    original = backend.limit_time
    backend.limit_time = lambda seconds: (seen.append(seconds), original(seconds))[1]  # type: ignore[method-assign]
    _worker(tmp_path, backend, max_seconds=50.0).run()
    assert seen and all(0 < value <= 50.0 for value in seen)


class _Broken:
    """An opener whose every request times out (or breaks off)."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def __call__(self, request: Any, timeout: float) -> Any:
        self.calls += 1
        raise self.error


def test_timeouts_are_charged_their_worst_case_and_budgeted() -> None:
    opener = _Broken(TimeoutError())
    backend = DeepSeekBackend(
        Settings(max_retries=5), key=TEST_KEY, opener=opener, sleep=lambda s: None
    )
    system, user = render_tag_prompt("chunk_summary", {"turns": []})
    worst = estimate_tokens(system + user) + 64
    backend.settings = Settings(max_retries=5, max_run_tokens=worst * 2 + 1)
    with pytest.raises(BackendBudgetExhausted):
        backend.run("chunk_summary", {"turns": []}, max_tokens=64)
    assert opener.calls == 2  # the third attempt no longer fits the budget
    assert backend.tokens_in + backend.tokens_out == worst * 2


def test_http_exceptions_and_overflowing_usage_are_mapped() -> None:
    import http.client

    opener = _Broken(http.client.RemoteDisconnected("closed"))
    backend = DeepSeekBackend(
        Settings(max_retries=1), key=TEST_KEY, opener=opener, sleep=lambda s: None
    )
    with pytest.raises(BackendUnavailable, match="transient"):
        backend.run("chunk_summary", {"turns": []}, max_tokens=64)
    assert opener.calls == 2
    with FakeDeepSeek() as fake:
        content = json.dumps(json.dumps({"summary": "s", "tags": {}}))
        fake.script = [
            (
                200,
                (
                    '{"choices": [{"message": {"content": ' + content + "}}],"
                    ' "usage": {"prompt_tokens": 1e999, "completion_tokens": 1}}'
                ).encode(),
            )
        ]
        with pytest.raises(DeepSeekError, match="bad_response"):
            _backend(fake).run("chunk_summary", {"turns": []}, max_tokens=64)


# -- migration -----------------------------------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, app TEXT NOT NULL,
    session_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
    chunk_start INTEGER NOT NULL, chunk_end INTEGER NOT NULL,
    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, tried_run TEXT NOT NULL DEFAULT '');
CREATE TABLE summaries (
    app TEXT NOT NULL, session_id TEXT NOT NULL, chunk_start INTEGER NOT NULL,
    chunk_end INTEGER NOT NULL, prompt_version TEXT NOT NULL, model TEXT NOT NULL,
    summary TEXT NOT NULL, intent TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (app, session_id, chunk_start, chunk_end, prompt_version));
INSERT INTO jobs VALUES ('j1', 'chunk_summary', 'codex', 's', 'f', 0, 3, 'done', 1, '',
    't0', 't0', '');
INSERT INTO summaries VALUES ('codex', 's', -1, 3, 'conv-v1', 'extractive', 'old summary',
    'old intent', 'success', 't0', 0, 0);
"""


def test_migration_adds_columns_and_leaves_old_rows_untouched(tmp_path: Path) -> None:
    path = tmp_path / "data" / "work.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(OLD_SCHEMA)
    path.chmod(0o600)
    state = WorkState(path)
    with state.open():
        pass
    with state.open():  # idempotent
        pass
    rows = _rows_of(tmp_path, "SELECT * FROM summaries")
    assert rows == [
        {
            "app": "codex",
            "session_id": "s",
            "chunk_start": -1,
            "chunk_end": 3,
            "prompt_version": "conv-v1",
            "model": "extractive",
            "summary": "old summary",
            "intent": "old intent",
            "outcome": "success",
            "created_at": "t0",
            "tokens_in": 0,
            "tokens_out": 0,
            "tags": None,
        }
    ]
    (job,) = _rows_of(tmp_path, "SELECT * FROM jobs")
    assert job["prompt_version"] == "conv-v1" and job["status"] == "done"
    assert job["session_ts"] == ""
    assert state.session_summary("codex", "s") == "old summary"


# -- the gate (vault.yaml) -------------------------------------------------------------------


def _section(**values: Any) -> dict[str, Any]:
    return {"conversations": {"backend": SPEC, **values}}


def test_gate_needs_config_cloud_and_key(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    assert isinstance(configured_backend({}), ExtractiveBackend)
    assert isinstance(configured_backend({"conversations": {}}), ExtractiveBackend)
    assert isinstance(configured_backend(_section(backend="extractive")), ExtractiveBackend)
    refused = configured_backend(_section(cloud=True))
    assert isinstance(refused, RefusedBackend) and refused.refused == "unavailable:missing_key"
    refused = configured_backend(_section())  # no key and no cloud: refused either way
    assert isinstance(refused, RefusedBackend) and refused.refused == "unavailable:missing_key"
    refused = configured_backend(_section(cloud=True, backend="no_such_module:factory"))
    assert isinstance(refused, RefusedBackend) and refused.refused == "not_importable"
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    for section in (_section(), _section(cloud=False)):
        refused = configured_backend(section)
        assert isinstance(refused, RefusedBackend) and refused.refused == "cloud_not_allowed"
    with pytest.raises(Exception, match="cloud must be true or false"):
        configured_backend(_section(cloud="yes"))
    refused = configured_backend(_section(cloud=True, options={"api_key": TEST_KEY}))
    assert isinstance(refused, RefusedBackend) and refused.refused == "unavailable:key_in_config"
    ready = configured_backend(_section(cloud=True, options={"base_url": fake.url}))
    assert isinstance(ready, DeepSeekBackend) and ready.settings.base_url == fake.url
    assert backend_status(_section(cloud=True)) == {
        "spec": SPEC,
        "name": "deepseek:deepseek-flash",
        "cloud": True,
        "state": "ready",
        "reason": "",
        "prompt_version": "conv-v2",
    }
    assert backend_status({}) is None
    assert fake.requests == []  # checking the gate never calls the model


def test_load_backend_function_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    with pytest.raises(Exception, match="--allow-cloud"):
        load_backend(SPEC)
    assert isinstance(load_backend(SPEC, allow_cloud=True), DeepSeekBackend)
    monkeypatch.delenv(KEY_ENV)
    with pytest.raises(Exception, match="unavailable: missing_key"):
        load_backend(SPEC, allow_cloud=True)


def test_cli_allow_cloud_cannot_widen_vault_cloud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeDeepSeek
) -> None:
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    vault = make_vault(tmp_path / "vault")
    write_store(tmp_path / "data" / "conversations.sqlite", _rows())
    flags = ("--backend", SPEC, "--max-sessions", "0")
    result = _cli(vault, tmp_path, *flags, "--allow-cloud")  # vault.yaml has no cloud: true
    assert result.exit_code == 2 and "conversations.cloud: true" in result.output
    set_config(vault, {"conversations": {"cloud": False}})
    assert _cli(vault, tmp_path, *flags, "--allow-cloud").exit_code == 2
    set_config(vault, {"conversations": {"cloud": True}})
    result = _cli(vault, tmp_path, *flags)  # the flag may narrow: still refused without it
    assert result.exit_code == 2 and "--allow-cloud" in result.output
    result = _cli(vault, tmp_path, *flags, "--allow-cloud")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["backend"] == "deepseek:deepseek-flash"
    assert TEST_KEY not in result.output and fake.requests == []


def test_refused_backend_gives_a_degraded_run_never_extractive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    report = _worker(tmp_path, configured_backend(_section(cloud=True))).run()
    assert report.status == "degraded" and report.stopped == "backend_refused"
    assert report.errors == {"unavailable:missing_key": 1}
    assert report.sessions_queued == 0 and report.jobs == {}
    assert _rows_of(tmp_path, "SELECT * FROM summaries") == []
    assert _rows_of(tmp_path, "SELECT * FROM jobs") == []
    (run,) = _rows_of(tmp_path, "SELECT status FROM runs")
    assert run == {"status": "degraded"}


def _cli(vault: Path, tmp_path: Path, *extra: str) -> Any:
    return CliRunner().invoke(
        cli_app,
        [
            "conversations",
            "worker",
            "--root",
            str(vault),
            "--store",
            str(tmp_path / "data" / "conversations.sqlite"),
            "--state",
            str(tmp_path / "data" / "work.sqlite"),
            "--no-ingest",
            *extra,
        ],
    )


def test_cli_worker_and_status_with_the_configured_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeDeepSeek
) -> None:
    vault = make_vault(tmp_path / "vault")
    write_store(tmp_path / "data" / "conversations.sqlite", _rows())
    state = {"state_path": str(tmp_path / "data" / "work.sqlite"), "idle_minutes": 0}
    set_config(
        vault,
        {
            **POLICY,
            "conversation_worker": state,
            "conversations": {"backend": SPEC, "cloud": True, "options": {"base_url": fake.url}},
        },
    )
    monkeypatch.delenv(KEY_ENV, raising=False)
    result = _cli(vault, tmp_path)
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["status"] == "degraded" and report["backend"] == f"refused:{SPEC}"
    status = CliRunner().invoke(cli_app, ["conversations", "status", "--root", str(vault)])
    assert status.exit_code == 0, status.output
    shown = json.loads(status.output)
    assert shown["backend"]["state"] == "degraded"
    assert shown["backend"]["reason"] == "unavailable:missing_key"
    assert shown["last_run"]["status"] == "degraded"
    assert fake.requests == []

    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    result = _cli(vault, tmp_path)
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["status"] == "ok" and report["backend"] == "deepseek:deepseek-flash"
    assert report["jobs"]["session_summary:done"] == 3 and len(fake.requests) == 6
    assert TEST_KEY not in result.output and HIDDEN not in fake.bodies()
    status = CliRunner().invoke(cli_app, ["conversations", "status", "--root", str(vault)])
    assert json.loads(status.output)["backend"]["state"] == "ready"
    assert TEST_KEY not in status.output
    # --backend on the command line still wins over vault.yaml.
    result = _cli(vault, tmp_path, "--backend", "extractive")
    assert json.loads(result.output)["backend"] == "extractive"


# -- layering of the adapter ------------------------------------------------------------------


def test_adapter_imports_only_stdlib_and_the_ports() -> None:
    allowed_roots = set(sys.stdlib_module_names) | {"memfw_adapter_deepseek", "memfw_ports"}
    for path in sorted((ADAPTER_SRC / "memfw_adapter_deepseek").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                ok = name.split(".")[0] in allowed_roots or name.startswith("memfw.ports")
                assert ok, (path.name, name)
                assert name == "memfw.ports.models" or not name.startswith("memfw."), name
    kernel = ROOT / "src" / "memfw"
    for path in kernel.rglob("*.py"):
        assert "memfw_adapter_deepseek" not in path.read_text(encoding="utf-8"), path


# -- default behaviour byte-identical to the base ref ---------------------------------------


@pytest.mark.skipif(
    not __import__("os").environ.get("MEMFW_ARCH_BASE_REF"),
    reason="opt-in (slow): set MEMFW_ARCH_BASE_REF to compare the worker against that ref",
)
def test_unconfigured_worker_is_byte_identical_to_base_ref() -> None:
    from tests.harness import byte_identical

    if not byte_identical.git_available():
        pytest.skip("git is not available")
    variants = [
        {},
        {"config": POLICY},
        {"config": {"conversation_worker": {"max_tokens": 60}}},
        {"config": {"conversations": {"backend": "extractive"}}},
    ]
    results = byte_identical.compare(
        "tests.harness.conversations_probe:worker_default",
        variants,
        old_ref=__import__("os").environ["MEMFW_ARCH_BASE_REF"],
    )
    for result in results:
        assert result.identical, f"{result.payload}:\n{result.diff[:4000]}"
    probe = json.loads(results[0].new)  # the probe really ran the worker
    assert json.loads(probe["cli"][0]["output"])["sessions_queued"] == 3
    assert len(probe["summaries"]) == 6 and len(probe["episodes"]) == 3


# -- reason codes, vocab, options, schema size (rev 31) --------------------------------------


def _valid_tags() -> dict[str, Any]:
    return {
        "project": ["Uploader"],
        "task_type": "dev",
        "components": ["Retry"],
        "intent": "Add a retry wrapper",
        "outcome": "done",
    }


def _harbor_rows() -> list[dict[str, Any]]:
    return [
        turn(
            "codex",
            "s-harbor",
            0,
            "user",
            "给 Uploader 加一个 retry，中英都要测。",
            "2026-01-05T09:00:00Z",
        ),
        turn(
            "codex",
            "s-harbor",
            1,
            "assistant",
            "Use exponential backoff with jitter.",
            "2026-01-05T09:01:00Z",
        ),
    ]


def _output_cases() -> list[tuple[str, str, str, str]]:
    """(code, reply body, finish_reason, a snippet that must not be stored)."""

    tags = _valid_tags()
    return [
        ("not_json", "{not json", "stop", "not json"),
        ("missing_summary", json.dumps({"tags": tags}), "stop", "Uploader"),
        (
            "bad_enum",
            json.dumps({"summary": "harbor-note", "tags": {**tags, "task_type": "coding"}}),
            "stop",
            "coding",
        ),
        (
            "summary_too_long",
            json.dumps({"summary": "长" * (TAG_SUMMARY_CHARS + 1), "tags": tags}),
            "stop",
            "长长长",
        ),
        (
            "item_looks_like_url",
            json.dumps(
                {"summary": "harbor-note", "tags": {**tags, "project": ["https://example.com/a"]}}
            ),
            "stop",
            "example.com",
        ),
        (
            "truncated",
            json.dumps({"summary": "Added a retry wrapper. Tests pass.", "tags": tags}),
            "length",
            "retry wrapper",
        ),
        (
            "bad_shape",
            json.dumps({"summary": "harbor-note", "extra": "shape-marker"}),
            "stop",
            "shape-marker",
        ),
    ]


@pytest.mark.parametrize(("code", "content", "finish_reason", "forbidden"), _output_cases())
def test_fake_backend_records_one_reason_code(
    tmp_path: Path,
    fake: FakeDeepSeek,
    code: str,
    content: str,
    finish_reason: str,
    forbidden: str,
) -> None:
    fake.script.append((200, tag_reply(content=content, finish_reason=finish_reason)))
    write_store(tmp_path / "data" / "conversations.sqlite", _harbor_rows())
    report = _worker(tmp_path, _backend(fake)).run()
    assert report.errors == {code: 1}
    jobs = _rows_of(tmp_path, "SELECT kind, status, error FROM jobs")
    chunk = next(row for row in jobs if row["kind"] == "chunk_summary")
    assert chunk["status"] == "pending" and chunk["error"] == code
    blob = json.dumps({"errors": report.errors, "jobs": jobs}, ensure_ascii=False)
    assert forbidden not in blob
    assert _rows_of(tmp_path, "SELECT summary FROM summaries") == []
    shown = WorkState(tmp_path / "data" / "work.sqlite").status(now=NOW)
    assert shown["recent_runs"]["reasons"][code] == 1
    assert shown["bad_output_share"] == 1.0


def test_omitted_tag_key_is_stored_and_counted_by_name(
    tmp_path: Path, fake: FakeDeepSeek
) -> None:
    """A legal summary with intent omitted is kept. The report names the key only."""

    tags = _valid_tags()
    del tags["intent"]
    note = "harbor-kept-summary"
    fake.default = tag_reply(content=json.dumps({"summary": note, "tags": tags}))
    write_store(tmp_path / "data" / "conversations.sqlite", _harbor_rows())
    report = _worker(tmp_path, _backend(fake)).run()
    assert report.errors == {}
    assert report.tag_key_defaulted == {"intent": 2}
    assert report.as_dict()["tag_key_defaulted"] == {"intent": 2}
    assert note not in json.dumps(report.as_dict())
    stored = [json.loads(row["tags"]) for row in _rows_of(tmp_path, "SELECT tags FROM summaries")]
    assert len(stored) == 2
    assert all(row["intent"] == "" and row["task_type"] == "dev" for row in stored)
    assert all(row["outcome"] == "done" for row in stored)


def test_present_empty_task_type_is_not_the_missing_key_default() -> None:
    tags = _valid_tags()
    tags["task_type"] = ""
    reply = json.dumps({"summary": "kept", "tags": tags})
    assert parse_reply("chunk_summary", reply, prompt_version="conv-v2") is None
    assert reply_issue("chunk_summary", reply, prompt_version="conv-v2") == "bad_enum"


def test_worker_maps_project_aliases_and_prompts_canonical_names(
    tmp_path: Path, fake: FakeDeepSeek
) -> None:
    vocab = {"Northwind": ("northwind-app", "north wind"), "Lumen": ("lumen-notes",)}
    body = {
        "summary": "给 Northwind 加了 retry。Tests pass.",
        "tags": {
            "project": ["North Wind", "lumen-notes", "other-tool"],
            "task_type": "dev",
            "components": ["Retry"],
            "intent": "给 Northwind 加重试",
            "outcome": "done",
        },
    }
    fake.default = tag_reply(content=json.dumps(body, ensure_ascii=False))
    write_store(tmp_path / "data" / "conversations.sqlite", _harbor_rows())
    report = _worker(tmp_path, _backend(fake), project_vocab=vocab).run()
    assert report.status == "ok"
    assert report.project_unmapped == 2  # chunk and session summary each keep other-tool
    system = fake.requests[0]["body"]["messages"][0]["content"]
    assert "Northwind" in system and "Lumen" in system
    assert "Chinese" in system and "proper nouns" in system
    assert "northwind-app" not in system and "lumen-notes" not in system
    assert "north wind" not in system
    stored = [
        json.loads(row["tags"])
        for row in _rows_of(tmp_path, "SELECT tags FROM summaries ORDER BY chunk_start")
    ]
    assert stored and all(row["project"] == ["northwind", "lumen", "other-tool"] for row in stored)


def test_empty_project_vocab_counts_nothing(tmp_path: Path, fake: FakeDeepSeek) -> None:
    body = {
        "summary": "Notes on the uploader.",
        "tags": {**_valid_tags(), "project": ["other-tool"]},
    }
    fake.default = tag_reply(content=json.dumps(body, ensure_ascii=False))
    write_store(tmp_path / "data" / "conversations.sqlite", _harbor_rows())
    report = _worker(tmp_path, _backend(fake)).run()
    assert report.project_unmapped == 0
    stored = [json.loads(row["tags"]) for row in _rows_of(tmp_path, "SELECT tags FROM summaries")]
    assert stored and all(row["project"] == ["other-tool"] for row in stored)
    system = fake.requests[0]["body"]["messages"][0]["content"]
    assert "Chinese" in system and "canonical name" not in system


def test_cli_backend_flag_applies_options_and_refuses_an_api_key(
    tmp_path: Path, fake: FakeDeepSeek, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = make_vault(tmp_path / "vault")
    write_store(tmp_path / "data" / "conversations.sqlite", _harbor_rows())
    set_config(
        vault,
        {
            "conversation_worker": {"idle_minutes": 0},
            "conversations": {
                "cloud": True,
                "options": {"model": "deepseek-v4-pro", "base_url": fake.url},
            },
        },
    )
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    result = _cli(vault, tmp_path, "--backend", SPEC, "--allow-cloud")
    assert result.exit_code == 0, result.output
    assert fake.requests
    assert {item["body"]["model"] for item in fake.requests} == {"deepseek-v4-pro"}
    assert TEST_KEY not in result.output
    sent = len(fake.requests)

    set_config(
        vault,
        {
            "conversations": {
                "cloud": True,
                "options": {"api_key": TEST_KEY, "base_url": fake.url},
            },
        },
    )
    refused = _cli(vault, tmp_path, "--backend", SPEC, "--allow-cloud")
    assert refused.exit_code == 2
    assert "key_in_config" in refused.output
    assert TEST_KEY not in refused.output
    assert len(fake.requests) == sent


def test_schema_max_reply_exceeds_the_512_output_cap() -> None:
    """Measured with the adapter's estimator on 2026-11-21: 1826 tokens.

    The payload is the schema maximum (CJK), not a live model reply. 512 is
    still the code default; the example vault raises the cap in options.
    """

    item = "名" * TAG_ITEM_CHARS
    payload = {
        "summary": "测" * TAG_SUMMARY_CHARS,
        "tags": {
            "project": [item] * TAG_MAX_ITEMS,
            "task_type": "dev",
            "components": [item] * TAG_MAX_ITEMS,
            "intent": "图" * TAG_INTENT_CHARS,
            "outcome": "done",
        },
    }
    measured = estimate_tokens(json.dumps(payload, ensure_ascii=False))
    assert measured > 512
    assert measured == 1826
