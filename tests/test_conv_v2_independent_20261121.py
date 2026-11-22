"""Independent black-box acceptance tests for conv-v2 hardening (Gemini rev 2).

Written strictly from requirements R1–R8 plus public docs,
CLI ``--help`` and module signatures — without reading ``src/`` implementations
or the developer's own acceptance tests.
"""

from __future__ import annotations

import json
import os
import random
import socket
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from memfw_adapter_deepseek import DEFAULT_MODEL, KEY_ENV, DeepSeekBackend, Settings, factory
from memfw_adapter_deepseek.fake import FakeDeepSeek, tag_reply
from typer.testing import CliRunner

from memfw.cli import app as cli_app
from memfw.conversations import SharePolicy
from memfw.conversations.adapter import SqliteStoreSource
from memfw.conversations.backends import (
    ModelReply,
    parse_reply,
    reply_issue,
)
from memfw.conversations.worker import Budgets, ConversationWorker, WorkState
from memfw.ports.models import (
    TAG_OUTCOMES,
    TAG_PROMPT_VERSION,
    TAG_SUMMARY_CHARS,
    TAG_TASKS,
    TASK_TYPES,
    map_project_names,
)
from tests.conversation_fixtures import (
    SECRET,
    isolated_home,  # noqa: F401  (pytest fixture)
    make_vault,
    set_config,
    turn,
    write_store,
)
from tests.real_home_guard import inspecting_real_home

SPEC = "memfw_adapter_deepseek:factory"
TEST_KEY = "sk-test-independent-0000000000000000"
CANARY = "PRIVATE_CANARY_9c4e_内部工单"
REAL_DATA = Path(os.path.expanduser("~")) / ".local" / "share" / "memfw"

_CONNECT = socket.socket.connect
_CONNECT_EX = socket.socket.connect_ex
_CREATE = socket.create_connection
_GETADDRINFO = socket.getaddrinfo
_LOOPBACK = {"127.0.0.1", "localhost"}


@pytest.fixture(autouse=True)
def _loopback_only(disable_network: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow only loopback sockets (for FakeDeepSeek) while keeping external network blocked."""

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


@pytest.fixture(autouse=True)
def _real_home_untouched(isolated_home: Path) -> Any:  # noqa: F811
    def snapshot() -> Any:
        try:
            return sorted((entry.name, entry.stat().st_mtime_ns) for entry in REAL_DATA.iterdir())
        except OSError:
            return None

    with inspecting_real_home():
        before = snapshot()
    yield
    assert Path.home() == isolated_home
    with inspecting_real_home():
        assert snapshot() == before


@pytest.fixture
def fake() -> Any:
    with FakeDeepSeek() as server:
        yield server


def _iso_ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).replace(microsecond=0).isoformat()


def _realistic_rows(
    session_id: str = "sess-acme-19",
    *,
    app: str = "codex",
    hours_ago: float = 2.0,
) -> list[dict[str, Any]]:
    """Mixed Chinese/English session with non-contiguous offsets."""

    t0 = _iso_ago(hours_ago + 0.2)
    t1 = _iso_ago(hours_ago + 0.1)
    t2 = _iso_ago(hours_ago)
    return [
        turn(
            app,
            session_id,
            5,
            "user",
            "排查 Acme Core 调度器在重启后 SQLite WAL 锁残留的问题，并确认重试退避上限。",
            t0,
        ),
        turn(
            app,
            session_id,
            17,
            "assistant",
            "复现了 acme-core 的锁竞争，已把退避上限改成 30s，并补充了单测覆盖失败路径。",
            t1,
        ),
        turn(
            app,
            session_id,
            39,
            "user",
            "好的，把变更和回归日志整理到发布检查清单里。",
            t2,
        ),
    ]


def _multi_chunk_rows(
    session_id: str,
    *,
    chunks: int = 2,
    app: str = "codex",
    hours_ago: float = 2.0,
) -> list[dict[str, Any]]:
    """Build a realistic multi-chunk session with exact ``chunks`` and non-contiguous offsets.

    Each turn caps at 4,000 chars and CHUNK_CHARS is 32,000, so 8 full turns fill one chunk;
    ``(chunks - 1) * 8 + 4`` turns produce exactly ``chunks`` chunks.
    """

    rows: list[dict[str, Any]] = []
    offset = 3
    total = (chunks - 1) * 8 + 4
    for idx in range(total):
        ts = _iso_ago(hours_ago + (total - idx) * 0.01)
        role = "user" if idx % 2 == 0 else "assistant"
        body = (
            f"第 {idx + 1} 轮讨论 Acme Core 分片迁移与重试策略（chunk 负载填充）："
            + ("验证 WAL checkpoint 与索引一致性；" * 240)
        )
        rows.append(turn(app, session_id, offset, role, body, ts))
        offset += 7 + (idx % 5)
    return rows


def _cli_worker(vault: Path, tmp_path: Path, *extra: str) -> Any:
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


def _cli_status(vault: Path, *extra: str) -> Any:
    return CliRunner().invoke(
        cli_app,
        ["conversations", "status", "--root", str(vault), *extra],
    )


def _query(tmp_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    db = tmp_path / "data" / "work.sqlite"
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _make_worker(
    tmp_path: Path,
    backend: Any,
    rows: list[dict[str, Any]],
    *,
    policy: SharePolicy | None = None,
    budgets: Budgets | None = None,
    summary_language: str = "zh",
    project_vocab: dict[str, list[str]] | None = None,
) -> ConversationWorker:
    store = tmp_path / "data" / "conversations.sqlite"
    write_store(store, rows)
    return ConversationWorker(
        SqliteStoreSource(store),
        WorkState(tmp_path / "data" / "work.sqlite"),
        backend=backend,
        policy=policy or SharePolicy.from_config({}),
        budgets=budgets or Budgets(idle_minutes=0),
        summary_language=summary_language,
        project_vocab=project_vocab,
    )


# ---------------------------------------------------------------------------
# R1: 默认模型是 deepseek-flash
# ---------------------------------------------------------------------------


def test_r1_default_model_is_deepseek_flash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeDeepSeek
) -> None:
    """R1: Without ``options.model``, DeepSeek adapter sends ``model: deepseek-flash``."""

    assert DEFAULT_MODEL == "deepseek-flash"
    monkeypatch.setenv(KEY_ENV, TEST_KEY)

    direct = factory({"base_url": fake.url})
    assert direct.settings.model == "deepseek-flash"
    assert direct.name == "deepseek:deepseek-flash"

    vault = make_vault(tmp_path / "vault")
    write_store(tmp_path / "data" / "conversations.sqlite", _realistic_rows())
    set_config(
        vault,
        {
            "conversation_worker": {"idle_minutes": 0},
            "conversations": {
                "backend": SPEC,
                "cloud": True,
                "options": {"base_url": fake.url},
            },
        },
    )
    result = _cli_worker(vault, tmp_path)
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["status"] == "ok"
    assert report["backend"] == "deepseek:deepseek-flash"
    assert len(fake.requests) >= 2
    assert {req["body"]["model"] for req in fake.requests} == {"deepseek-flash"}


# ---------------------------------------------------------------------------
# R2: 具体原因码 + 截断识别 + 绝不泄露模型原文
# ---------------------------------------------------------------------------


def _v2_payload(summary: Any = "修复了 Acme Core 的重试配置。", **tags: Any) -> str:
    base_tags: dict[str, Any] = {
        "project": ["acme-core"],
        "task_type": "debug",
        "components": ["scheduler"],
        "intent": "排查并修复 WAL 锁残留",
        "outcome": "done",
    }
    base_tags.update(tags)
    return json.dumps({"summary": summary, "tags": base_tags}, ensure_ascii=False)


def test_r2_bad_outputs_record_specific_reason_codes_without_leaking_text(
    tmp_path: Path, fake: FakeDeepSeek
) -> None:
    """R2: Distinct validation failures produce distinct non-generic codes in report and job,
    never leaking raw model output text or secrets."""

    cases: list[tuple[str, dict[str, Any]]] = [
        ("not_json", tag_reply(content=f"这不是合法 JSON，含隐私 {CANARY}")),
        (
            "missing_summary",
            tag_reply(
                content=json.dumps(
                    {
                        "tags": {
                            "project": ["acme-core"],
                            "task_type": "debug",
                            "components": ["scheduler"],
                            "intent": f"排查 {CANARY}",
                            "outcome": "done",
                        }
                    },
                    ensure_ascii=False,
                )
            ),
        ),
        ("summary_too_long", tag_reply(content=_v2_payload(f"{CANARY} " + "字" * 850))),
        ("bad_enum", tag_reply(content=_v2_payload(f"完成 {CANARY}", task_type="coding"))),
        (
            "bad_shape",
            tag_reply(
                content=json.dumps(
                    {"summary": f"正常摘要 {CANARY}", "tags": ["acme-core"]},
                    ensure_ascii=False,
                )
            ),
        ),
        (
            "secret_in_output",
            tag_reply(content=_v2_payload(f"不要泄露 {SECRET} 和 {CANARY}")),
        ),
    ]

    codes: dict[str, str] = {}
    for label, response in cases:
        case_dir = tmp_path / label
        fake.requests.clear()
        fake.script = [(200, response)]
        fake.default = response
        backend = DeepSeekBackend(Settings(base_url=fake.url), key=TEST_KEY)
        report = _make_worker(case_dir, backend, _realistic_rows(f"sess-{label}")).run()

        assert report.errors, f"{label}: expected an error code in report.errors"
        assert len(report.errors) == 1, (label, report.errors)
        (code, count) = next(iter(report.errors.items()))
        assert count == 1
        assert code != "bad_output", f"{label}: got generic 'bad_output' instead of specific code"
        codes[label] = code

        job_rows = _query(case_dir, "SELECT status, error FROM jobs WHERE kind = 'chunk_summary'")
        assert len(job_rows) == 1
        assert job_rows[0]["error"] == code
        assert _query(case_dir, "SELECT COUNT(*) AS n FROM summaries")[0]["n"] == 0

        run_rows = _query(case_dir, "SELECT report FROM runs")
        serialized = json.dumps(
            {"errors": report.errors, "jobs": job_rows, "runs": run_rows},
            ensure_ascii=False,
        )
        assert CANARY not in serialized, f"{label}: raw model text leaked!"
        assert SECRET not in serialized, f"{label}: secret leaked!"

    # The core schema failure categories must be distinguishable from one another.
    distinct = {
        codes["not_json"],
        codes["missing_summary"],
        codes["summary_too_long"],
        codes["bad_enum"],
        codes["bad_shape"],
    }
    assert len(distinct) == 5, f"expected 5 distinct reason codes, got {codes}"


def test_r2_truncated_reply_finish_reason_length_reports_truncation(
    tmp_path: Path, fake: FakeDeepSeek
) -> None:
    """R2: When DeepSeek returns ``finish_reason='length'``, the code identifies truncation."""

    truncated_content = f'{{"summary": "修复了 Acme Core 调度器锁问题，包含 {CANARY}'
    fake.default = tag_reply(content=truncated_content, finish_reason="length")
    backend = DeepSeekBackend(Settings(base_url=fake.url), key=TEST_KEY)
    report = _make_worker(tmp_path, backend, _realistic_rows("sess-trunc-1")).run()

    assert len(report.errors) == 1
    (code, count) = next(iter(report.errors.items()))
    assert count == 1
    assert code != "bad_output"
    assert "truncat" in code.lower(), f"expected truncation code, got {code!r}"

    job_rows = _query(tmp_path, "SELECT error FROM jobs WHERE kind = 'chunk_summary'")
    assert len(job_rows) == 1 and job_rows[0]["error"] == code
    assert CANARY not in json.dumps({"errors": report.errors, "jobs": job_rows}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# R3: summary_language 默认 zh & tag_vocab.project 别名归一化 + 未映射计数
# ---------------------------------------------------------------------------


def test_r3_summary_language_defaults_to_zh_and_respects_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeDeepSeek
) -> None:
    """R3: ``conversations.summary_language`` defaults to ``zh`` and appears in model prompt."""

    monkeypatch.setenv(KEY_ENV, TEST_KEY)

    # Default (no summary_language in vault.yaml) -> zh
    dir_zh = tmp_path / "default_zh"
    vault_zh = make_vault(dir_zh / "vault")
    write_store(dir_zh / "data" / "conversations.sqlite", _realistic_rows("sess-lang-zh"))
    set_config(
        vault_zh,
        {
            "conversation_worker": {"idle_minutes": 0},
            "conversations": {
                "backend": SPEC,
                "cloud": True,
                "options": {"base_url": fake.url},
            },
        },
    )
    res_zh = _cli_worker(vault_zh, dir_zh)
    assert res_zh.exit_code == 0, res_zh.output
    assert fake.requests
    prompt_zh = json.dumps(fake.requests[0]["body"]["messages"], ensure_ascii=False).lower()
    assert any(tok in prompt_zh for tok in ("chinese", "中文", "简体", "zh")), prompt_zh

    # Explicit summary_language: en
    fake.requests.clear()
    dir_en = tmp_path / "explicit_en"
    vault_en = make_vault(dir_en / "vault")
    write_store(dir_en / "data" / "conversations.sqlite", _realistic_rows("sess-lang-en"))
    set_config(
        vault_en,
        {
            "conversation_worker": {"idle_minutes": 0},
            "conversations": {
                "backend": SPEC,
                "cloud": True,
                "summary_language": "en",
                "options": {"base_url": fake.url},
            },
        },
    )
    res_en = _cli_worker(vault_en, dir_en)
    assert res_en.exit_code == 0, res_en.output
    assert fake.requests
    prompt_en = json.dumps(fake.requests[0]["body"]["messages"], ensure_ascii=False).lower()
    assert "english" in prompt_en and prompt_en != prompt_zh


def test_r3_project_tag_vocab_maps_aliases_and_counts_unmapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeDeepSeek
) -> None:
    """R3: ``tag_vocab.project`` maps aliases case/whitespace-insensitively and counts unmapped."""

    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    vault = make_vault(tmp_path / "vault")
    write_store(tmp_path / "data" / "conversations.sqlite", _realistic_rows("sess-vocab-1"))
    set_config(
        vault,
        {
            "conversation_worker": {"idle_minutes": 0},
            "conversations": {
                "backend": SPEC,
                "cloud": True,
                "tag_vocab": {
                    "project": {
                        "acme-core": ["Acme Core", "acme_core_repo", "  ACME  "],
                        "beacon-cli": ["Beacon CLI"],
                    }
                },
                "options": {"base_url": fake.url},
            },
        },
    )
    fake.default = tag_reply(
        summary="整理了 Acme Core 和 Beacon CLI 的发布脚本。",
        tags={
            "project": ["  acme core ", "ACME_CORE_REPO", "  Beacon cli  ", "orphan-sidecar"],
            "task_type": "ops",
            "components": ["release"],
            "intent": "统一发布脚本并检查回滚",
            "outcome": "done",
        },
    )
    result = _cli_worker(vault, tmp_path)
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["status"] == "ok"
    # 1 unmapped project ("orphan-sidecar") in chunk_summary + 1 in session_summary = 2
    assert report.get("project_unmapped", 0) == 2

    rows = _query(tmp_path, "SELECT chunk_start, tags FROM summaries ORDER BY chunk_start")
    assert len(rows) == 2
    for row in rows:
        tags = json.loads(row["tags"])
        projects = tags["project"]
        assert "acme-core" in projects
        assert "beacon-cli" in projects
        assert "orphan-sidecar" in projects
        # Raw aliases must not remain after canonicalization; duplicates collapsed.
        assert "acme core" not in projects
        assert "acme_core_repo" not in projects
        assert "beacon cli" not in projects
        assert len(projects) == 3


# ---------------------------------------------------------------------------
# R4: 命令行 --backend 生效 options + 拒绝 api_key + 不可绕过 cloud 开关
# ---------------------------------------------------------------------------


def test_r4_cli_backend_applies_options_and_enforces_security_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeDeepSeek
) -> None:
    """R4: ``--backend`` forwards ``conversations.options`` and preserves key & cloud gates."""

    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    vault = make_vault(tmp_path / "vault")
    write_store(tmp_path / "data" / "conversations.sqlite", _realistic_rows("sess-r4-1"))

    # 1. options.model and base_url take effect under --backend
    set_config(
        vault,
        {
            "conversation_worker": {"idle_minutes": 0},
            "conversations": {
                "cloud": True,
                "options": {
                    "model": "deepseek-reasoner",
                    "base_url": fake.url,
                    "price_in_per_mtok": 0.55,
                    "price_out_per_mtok": 2.19,
                },
            },
        },
    )
    ok = _cli_worker(vault, tmp_path, "--backend", SPEC, "--allow-cloud")
    assert ok.exit_code == 0, ok.output
    report = json.loads(ok.output)
    assert report["backend"] == "deepseek:deepseek-reasoner"
    assert {req["body"]["model"] for req in fake.requests} == {"deepseek-reasoner"}

    # 2. options price overrides actually enforce cost budget
    (tmp_path / "data" / "work.sqlite").unlink()
    fake.requests.clear()
    set_config(
        vault,
        {
            "conversation_worker": {"idle_minutes": 0},
            "conversations": {
                "cloud": True,
                "options": {
                    "model": "deepseek-reasoner",
                    "base_url": fake.url,
                    "price_in_per_mtok": 5000.0,
                    "price_out_per_mtok": 5000.0,
                    "max_run_cost_usd": 0.01,
                },
            },
        },
    )
    budgeted = _cli_worker(vault, tmp_path, "--backend", SPEC, "--allow-cloud")
    assert budgeted.exit_code == 0, budgeted.output
    assert json.loads(budgeted.output)["stopped"] == "backend_budget"
    assert fake.requests == []

    # 3. Red-team: api_key in options is refused and never echoed
    leaked = "sk-redteam-forbidden-key-7777"
    set_config(
        vault,
        {
            "conversation_worker": {"idle_minutes": 0},
            "conversations": {
                "cloud": True,
                "options": {"api_key": leaked, "base_url": fake.url},
            },
        },
    )
    refused_key = _cli_worker(vault, tmp_path, "--backend", SPEC, "--allow-cloud")
    assert refused_key.exit_code != 0
    assert leaked not in refused_key.output
    assert fake.requests == []

    # 4. Red-team: missing or false conversations.cloud cannot be widened by --allow-cloud
    for conv_cfg in (
        {"options": {"model": "deepseek-reasoner", "base_url": fake.url}},
        {"cloud": False, "options": {"model": "deepseek-reasoner", "base_url": fake.url}},
    ):
        set_config(vault, {"conversation_worker": {"idle_minutes": 0}, "conversations": conv_cfg})
        refused_cloud = _cli_worker(vault, tmp_path, "--backend", SPEC, "--allow-cloud")
        assert refused_cloud.exit_code != 0
        assert fake.requests == []

    # 5. Narrowing: cloud: true without --allow-cloud still refuses
    set_config(
        vault,
        {
            "conversation_worker": {"idle_minutes": 0},
            "conversations": {
                "cloud": True,
                "options": {"model": "deepseek-reasoner", "base_url": fake.url},
            },
        },
    )
    narrowed = _cli_worker(vault, tmp_path, "--backend", SPEC)
    assert narrowed.exit_code != 0
    assert fake.requests == []


# ---------------------------------------------------------------------------
# R5: 预算有限时优先做完已开始的会话 + 新会话保底 + 本轮完成整会话摘要计数
# ---------------------------------------------------------------------------


class _ScriptedV2Backend:
    """Deterministic in-process conv-v2 backend with fixed token charges for budget tests."""

    name = "fake:conv-v2"
    local = True
    prompt_version = TAG_PROMPT_VERSION
    tasks = TAG_TASKS

    def __init__(self, *, tokens_per_call: int = 500) -> None:
        self.tokens_per_call = tokens_per_call
        self.calls: list[str] = []

    def run(self, task: str, data: dict[str, Any], *, max_tokens: int) -> ModelReply:
        self.calls.append(task)
        reply = {
            "summary": "完成了 Acme Core 分片与会话汇总。",
            "tags": {
                "project": ["acme-core"],
                "task_type": "dev",
                "components": ["worker"],
                "intent": "验证预算调度顺序",
                "outcome": "done",
            },
        }
        return ModelReply(
            text=json.dumps(reply, ensure_ascii=False),
            tokens_in=self.tokens_per_call,
            tokens_out=50,
            finish_reason="stop",
        )


def test_r5_in_progress_session_finishes_first_and_reports_sessions_summarised(
    tmp_path: Path,
) -> None:
    """R5: Under a tight token budget, an already half-done session finishes (including
    ``session_summary``) before newer untouched sessions, and ``sessions_summarised`` counts it."""

    # Run 1: older multi-chunk session (2 chunks) completes only chunk 0 under tight budget.
    started_rows = _multi_chunk_rows("sess-started", chunks=2, hours_ago=5.0)
    backend1 = _ScriptedV2Backend(tokens_per_call=12_000)
    run1 = _make_worker(
        tmp_path,
        backend1,
        started_rows,
        budgets=Budgets(idle_minutes=0, max_sessions=10, max_tokens=15_000),
    ).run()
    assert run1.stopped == "max_tokens"
    assert run1.jobs.get("chunk_summary:done") == 1
    assert run1.sessions_summarised == 0

    # Newer untouched sessions arrive (more recent than sess-started).
    all_rows = (
        started_rows
        + _multi_chunk_rows("sess-new-1", chunks=2, hours_ago=2.0)
        + _multi_chunk_rows("sess-new-2", chunks=2, hours_ago=1.0)
    )
    # Run 2: budget allows ~2 calls (1 remaining chunk + 1 session_summary, not all 3 sessions).
    backend2 = _ScriptedV2Backend(tokens_per_call=12_000)
    run2 = _make_worker(
        tmp_path,
        backend2,
        all_rows,
        budgets=Budgets(idle_minutes=0, max_sessions=10, max_tokens=28_000),
    ).run()

    finished_sessions = {
        row["session_id"]
        for row in _query(tmp_path, "SELECT session_id FROM summaries WHERE chunk_start = -1")
    }
    assert "sess-started" in finished_sessions, (
        f"in-progress session did not finish first; finished={finished_sessions}, "
        f"run2.jobs={run2.jobs}"
    )
    assert run2.sessions_summarised == run2.jobs.get("session_summary:done", 0)
    assert run2.sessions_summarised >= 1


def test_r5_new_sessions_have_guaranteed_share_and_are_not_starved(tmp_path: Path) -> None:
    """R5: Even with a large backlog of half-done sessions, new sessions still get a slice."""

    # Seed 8 multi-chunk sessions (2 chunks each) and mark chunk 0 done for all 8
    # so all 8 are half-done (chunk 1 + session_summary still pending).
    all_rows: list[dict[str, Any]] = []
    for idx in range(8):
        all_rows.extend(_multi_chunk_rows(f"sess-half-{idx}", chunks=2, hours_ago=10.0 - idx * 0.2))
    # Plan all 8 sessions without executing any jobs (hit max_seconds right at _execute),
    # then mark chunk 0 of each session done so all 8 are half-done.
    planner = _make_worker(
        tmp_path,
        _ScriptedV2Backend(tokens_per_call=12_000),
        all_rows,
        budgets=Budgets(idle_minutes=0, max_sessions=20, max_tokens=500_000, max_seconds=1.0),
    )
    ticks = iter([0.0, 10.0, 10.0, 10.0])
    planner.monotonic = lambda: next(ticks, 10.0)
    plan_run = planner.run()
    assert plan_run.sessions_queued == 8
    assert plan_run.jobs == {}
    with sqlite3.connect(tmp_path / "data" / "work.sqlite") as conn:
        conn.execute(
            "UPDATE jobs SET status = 'done' WHERE kind = 'chunk_summary' AND chunk_start = 3"
        )
        # Seed matching chunk-0 summaries so session_summary can read them.
        rows_done = conn.execute(
            "SELECT app, session_id, chunk_start, chunk_end FROM jobs"
            " WHERE kind = 'chunk_summary' AND status = 'done'"
        ).fetchall()
        for app, sid, cs, ce in rows_done:
            conn.execute(
                "INSERT INTO summaries (app, session_id, chunk_start, chunk_end, prompt_version,"
                " model, summary, created_at) VALUES (?, ?, ?, ?, 'conv-v2', 'fake:conv-v2',"
                " '已完成第一段摘要', ?)",
                (app, sid, cs, ce, _iso_ago(1.0)),
            )

    # Add 4 brand-new sessions (more recent, 1 chunk each so they are cheap to pick up).
    for idx in range(4):
        all_rows.extend(_realistic_rows(f"sess-fresh-{idx}", hours_ago=1.0 - idx * 0.1))

    # Run with budget for ~10 jobs (< 16 jobs needed to drain all 8 half-done sessions).
    run = _make_worker(
        tmp_path,
        _ScriptedV2Backend(tokens_per_call=10_000),
        all_rows,
        budgets=Budgets(idle_minutes=0, max_sessions=20, max_tokens=105_000),
    ).run()

    touched_fresh = {
        row["session_id"]
        for row in _query(
            tmp_path,
            "SELECT DISTINCT session_id FROM summaries WHERE session_id LIKE 'sess-fresh-%'",
        )
    }
    finished_half = {
        row["session_id"]
        for row in _query(
            tmp_path,
            "SELECT session_id FROM summaries WHERE chunk_start = -1"
            " AND session_id LIKE 'sess-half-%'",
        )
    }
    assert finished_half, f"expected half-done sessions to finish; run.jobs={run.jobs}"
    assert touched_fresh, (
        f"new sessions were starved behind half-done backlog; finished_half={finished_half}, "
        f"run.jobs={run.jobs}"
    )
    assert run.sessions_summarised == run.jobs.get("session_summary:done", 0)


# ---------------------------------------------------------------------------
# R6: memfw conversations status 报告与 expect_backend 告警退出码
# ---------------------------------------------------------------------------


def test_r6_status_reports_backend_24h_counts_degraded_reasons_and_bad_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeDeepSeek
) -> None:
    """R6: ``status`` reports backend, 24h per-version counts, degraded reasons and bad rate."""

    vault = make_vault(tmp_path / "vault")
    state_file = tmp_path / "data" / "work.sqlite"
    rows = _realistic_rows("sess-st-1", hours_ago=2.0) + _realistic_rows(
        "sess-st-2", hours_ago=1.5
    )
    write_store(tmp_path / "data" / "conversations.sqlite", rows)
    base_cfg: dict[str, Any] = {
        "conversation_worker": {"state_path": str(state_file), "idle_minutes": 0},
        "conversations": {
            "backend": SPEC,
            "cloud": True,
            "expect_backend": "deepseek:deepseek-flash",
            "options": {"base_url": fake.url},
        },
    }
    set_config(vault, base_cfg)

    # Run 1: 1 bad output on sess-st-1 chunk, ok on sess-st-2 chunk + session
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    fake.script = [
        (200, tag_reply(content=f"坏回复 {CANARY}")),
        (200, tag_reply(summary="正常分块摘要")),
        (200, tag_reply(summary="正常会话摘要")),
    ]
    r1 = _cli_worker(vault, tmp_path)
    assert r1.exit_code == 0, r1.output

    # Run 2: degraded run (missing API key)
    monkeypatch.delenv(KEY_ENV, raising=False)
    r2 = _cli_worker(vault, tmp_path)
    assert r2.exit_code == 0, r2.output
    assert json.loads(r2.output)["status"] == "degraded"

    # Restore key and inspect status
    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    st = _cli_status(vault, "--runs", "10")
    assert st.exit_code == 0, st.output
    data = json.loads(st.output)
    dumped = json.dumps(data, ensure_ascii=False)

    # Privacy: never leaks model text or secret key
    assert CANARY not in dumped
    assert TEST_KEY not in dumped

    # Must report backend in use
    assert "backend" in data and data["backend"] is not None
    assert "deepseek" in json.dumps(data["backend"])

    # Must report per-prompt-version counts for the last 24 hours (including conv-v2 >= 2)
    def _find_key_containing(obj: Any, needle: str) -> Any:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if needle in k.lower():
                    return v
                found = _find_key_containing(v, needle)
                if found is not None:
                    return found
        return None

    # Check 24h per-version summary counts exist and include conv-v2 with count >= 2
    assert "conv-v2" in dumped
    counts_24h = _find_key_containing(data, "24h") or _find_key_containing(data, "summaries")
    assert isinstance(counts_24h, dict) and counts_24h.get("conv-v2", 0) >= 2, data

    # Must report recent degraded reasons (from Run 2's missing_key)
    assert "missing_key" in dumped, data

    # Must report bad-output rate/ratio (> 0 since Run 1 had a bad output)
    rate = _find_key_containing(data, "bad_output") or _find_key_containing(data, "rate")
    assert rate is not None, data
    if isinstance(rate, (int, float)):
        assert 0 < rate <= 1.0
    elif isinstance(rate, dict):
        assert any(isinstance(v, (int, float)) and v > 0 for v in rate.values()), rate


def test_r6_status_exits_nonzero_on_expect_backend_mismatch_or_stale_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeDeepSeek
) -> None:
    """R6: ``status`` exits non-zero when ``expect_backend`` mismatches or has no 24h summaries."""

    monkeypatch.setenv(KEY_ENV, TEST_KEY)
    vault = make_vault(tmp_path / "vault")
    state_file = tmp_path / "data" / "work.sqlite"
    write_store(tmp_path / "data" / "conversations.sqlite", _realistic_rows("sess-alert-1"))

    # Case A: expect_backend set, but actual backend is extractive -> non-zero exit
    set_config(
        vault,
        {
            "conversation_worker": {"state_path": str(state_file), "idle_minutes": 0},
            "conversations": {"expect_backend": "deepseek:deepseek-flash"},
        },
    )
    assert _cli_worker(vault, tmp_path).exit_code == 0
    mismatch = _cli_status(vault)
    assert mismatch.exit_code != 0, mismatch.output
    assert "backend_not_in_use" in mismatch.output

    # Case B: actual backend matches expect_backend (deepseek:deepseek-flash),
    # but the only stored summary is conv-v1 (no conv-v2 summary in last 24h) -> non-zero exit
    set_config(
        vault,
        {
            "conversation_worker": {"state_path": str(state_file), "idle_minutes": 0},
            "conversations": {
                "backend": SPEC,
                "cloud": True,
                "expect_backend": "deepseek:deepseek-flash",
                "options": {"base_url": fake.url},
            },
        },
    )
    no_v2 = _cli_status(vault)
    assert no_v2.exit_code != 0, no_v2.output
    assert "backend_not_in_use" in no_v2.output

    # Case C: run conv-v2 worker so a fresh conv-v2 summary exists -> exit 0
    assert _cli_worker(vault, tmp_path).exit_code == 0
    healthy = _cli_status(vault)
    assert healthy.exit_code == 0, healthy.output

    # Case D: backdate all conv-v2 summaries to 48 hours ago -> stale -> non-zero exit
    old_ts = _iso_ago(48.0)
    with sqlite3.connect(state_file) as conn:
        conn.execute("UPDATE summaries SET created_at = ?", (old_ts,))
    stale = _cli_status(vault)
    assert stale.exit_code != 0, stale.output
    assert "backend_not_in_use" in stale.output


# ---------------------------------------------------------------------------
# R7: 缺 tag 键时不整条拒绝、补默认值并计数；非对象/未知键/非法枚举仍拒绝
# ---------------------------------------------------------------------------


_EXPECTED_DEFAULTS: dict[str, Any] = {
    "project": [],
    "task_type": "other",
    "components": [],
    "intent": "",
    "outcome": "unknown",
}


@pytest.mark.parametrize("omitted_key", ["project", "task_type", "components", "intent", "outcome"])
def test_r7_missing_tag_key_fills_default_and_counts_instead_of_rejecting(
    tmp_path: Path, fake: FakeDeepSeek, omitted_key: str
) -> None:
    """R7 (expected post-rev-32): Missing a tag key does NOT drop the reply; it fills the
    default value and increments a counter in the run report."""

    tags: dict[str, Any] = {
        "project": ["acme-core"],
        "task_type": "debug",
        "components": ["scheduler"],
        "intent": "排查 WAL 锁残留",
        "outcome": "done",
    }
    del tags[omitted_key]
    fake.default = tag_reply(content=json.dumps({"summary": "修复了锁残留问题。", "tags": tags}))
    backend = DeepSeekBackend(Settings(base_url=fake.url), key=TEST_KEY)
    report = _make_worker(tmp_path, backend, _realistic_rows(f"sess-r7-{omitted_key}")).run()

    # Must NOT be rejected
    assert report.errors == {}, f"omitting {omitted_key!r} should not error, got {report.errors}"
    rows = _query(tmp_path, "SELECT chunk_start, tags FROM summaries ORDER BY chunk_start")
    assert len(rows) == 2, f"expected chunk + session summaries stored for missing {omitted_key!r}"
    for row in rows:
        stored_tags = json.loads(row["tags"])
        assert stored_tags[omitted_key] == _EXPECTED_DEFAULTS[omitted_key]

    # Must be counted in run report (any positive tag/missing/default counter field)
    report_dict = report.as_dict()
    counter_keys = [
        k
        for k, v in report_dict.items()
        if isinstance(v, int)
        and v > 0
        and any(word in k.lower() for word in ("tag", "missing", "default", "fill"))
    ]
    assert counter_keys, (
        f"expected a positive counter for missing tag key {omitted_key!r} in report: {report_dict}"
    )


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("tags_list", json.dumps({"summary": "合法摘要。", "tags": ["acme-core"]})),
        ("tags_str", json.dumps({"summary": "合法摘要。", "tags": "debug"})),
        ("tags_null", json.dumps({"summary": "合法摘要。", "tags": None})),
        ("unknown_key", _v2_payload("合法摘要。", severity="high")),
        ("bad_task_type", _v2_payload("合法摘要。", task_type="refactoring")),
        ("bad_outcome", _v2_payload("合法摘要。", outcome="success")),
    ],
)
def test_r7_non_object_unknown_key_and_bad_enum_still_rejected(
    tmp_path: Path, fake: FakeDeepSeek, label: str, payload: str
) -> None:
    """R7: Non-object ``tags``, unknown keys inside ``tags``, and invalid enums remain rejected."""

    fake.default = tag_reply(content=payload)
    backend = DeepSeekBackend(Settings(base_url=fake.url), key=TEST_KEY)
    report = _make_worker(tmp_path, backend, _realistic_rows(f"sess-r7-rej-{label}")).run()

    assert report.errors, f"{label}: expected rejection"
    assert "bad_output" not in report.errors
    assert _query(tmp_path, "SELECT COUNT(*) AS n FROM summaries")[0]["n"] == 0


# ---------------------------------------------------------------------------
# R8: 没配置云端行为不变 + 随机回复性质测试
# ---------------------------------------------------------------------------


def test_r8_unconfigured_cloud_keeps_extractive_behavior_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R8: Without cloud config, worker runs extractive (conv-v1), respects opt-out/hidden apps,
    is idempotent, and status exits 0."""

    monkeypatch.delenv(KEY_ENV, raising=False)
    vault = make_vault(tmp_path / "vault")
    state_file = tmp_path / "data" / "work.sqlite"
    rows = _realistic_rows("sess-local-1", app="codex", hours_ago=2.0) + _realistic_rows(
        "sess-hidden-1", app="cursor", hours_ago=2.0
    )
    write_store(tmp_path / "data" / "conversations.sqlite", rows)
    set_config(
        vault,
        {
            "conversation_worker": {"state_path": str(state_file), "idle_minutes": 0},
            "conversation_share": {"apps": {"cursor": {"readable": False}}},
        },
    )

    first = _cli_worker(vault, tmp_path)
    assert first.exit_code == 0, first.output
    r1 = json.loads(first.output)
    assert r1["status"] == "ok"
    assert r1["backend"] == "extractive"
    assert r1["errors"] == {}
    assert r1["episodes_queued"] == 1

    summaries = _query(tmp_path, "SELECT app, prompt_version, tags FROM summaries")
    assert len(summaries) == 2
    assert {row["app"] for row in summaries} == {"codex"}
    assert {row["prompt_version"] for row in summaries} == {"conv-v1"}
    assert all(row["tags"] is None for row in summaries)

    # Idempotent second run
    second = _cli_worker(vault, tmp_path)
    assert second.exit_code == 0, second.output
    assert json.loads(second.output)["jobs"] == {}

    # Status succeeds without expect_backend
    st = _cli_status(vault)
    assert st.exit_code == 0, st.output


def test_r8_property_random_conv_v2_replies_obey_validation_and_vocab_rules() -> None:
    """R8 property test: 300 randomized conv-v2 replies satisfy R2/R3/R7 rejection, reason-code,
    privacy, and project-vocab normalization invariants."""

    rng = random.Random(20261121)
    vocab = {
        "acme-core": ("Acme Core", "acme_core_repo"),
        "beacon-cli": ("Beacon CLI", "beacon"),
    }
    bad_enums_task = ["coding", "bugfix", "infra", "unknown_type"]
    bad_enums_outcome = ["success", "failed", "wip", "complete"]

    for idx in range(300):
        kind = rng.choice(
            [
                "valid",
                "valid_with_aliases",
                "valid_with_url_item",
                "truncated_length",
                "not_json",
                "not_object",
                "missing_summary",
                "summary_empty",
                "summary_too_long",
                "tags_not_object",
                "unknown_tag_key",
                "bad_task_type",
                "bad_outcome",
                "secret_summary",
            ]
        )
        finish_reason = "stop"
        canary = f"CANARY_{idx}_{rng.randint(1000, 9999)}"
        summary_ok = f"第 {idx} 次修改 Acme Core 重试逻辑，验证通过。"
        valid_tags = {
            "project": [rng.choice(["acme-core", "beacon-cli"])],
            "task_type": rng.choice(TASK_TYPES),
            "components": [rng.choice(["worker", "scheduler", "store"])],
            "intent": "排查并修复调度问题",
            "outcome": rng.choice(TAG_OUTCOMES),
        }

        if kind == "valid":
            raw = json.dumps({"summary": summary_ok, "tags": valid_tags}, ensure_ascii=False)
            should_accept = True
        elif kind == "valid_with_aliases":
            alias = rng.choice(["  Acme Core ", "ACME_CORE_REPO", "  beacon cli  "])
            unmapped = f"sidecar-{idx}"
            tags = {**valid_tags, "project": [alias, unmapped]}
            raw = json.dumps({"summary": summary_ok, "tags": tags}, ensure_ascii=False)
            should_accept = True
        elif kind == "valid_with_url_item":
            url_item = rng.choice(["https://example.com/a", "src/acme/worker.py", "~/notes"])
            tags = {**valid_tags, "components": [url_item, "worker"]}
            raw = json.dumps({"summary": summary_ok, "tags": tags}, ensure_ascii=False)
            should_accept = True
        elif kind == "truncated_length":
            raw = f'{{"summary": "被截断的回复 {canary}'
            finish_reason = "length"
            should_accept = False
        elif kind == "not_json":
            raw = f"plain text {canary} {{{idx}"
            should_accept = False
        elif kind == "not_object":
            raw = json.dumps([canary, idx])
            should_accept = False
        elif kind == "missing_summary":
            raw = json.dumps({"tags": valid_tags}, ensure_ascii=False)
            should_accept = False
        elif kind == "summary_empty":
            raw = json.dumps({"summary": "   \n ", "tags": valid_tags}, ensure_ascii=False)
            should_accept = False
        elif kind == "summary_too_long":
            raw = json.dumps(
                {"summary": f"{canary} " + "长" * (TAG_SUMMARY_CHARS + 20), "tags": valid_tags},
                ensure_ascii=False,
            )
            should_accept = False
        elif kind == "tags_not_object":
            raw = json.dumps({"summary": summary_ok, "tags": [canary]}, ensure_ascii=False)
            should_accept = False
        elif kind == "unknown_tag_key":
            tags = {**valid_tags, f"extra_{idx}": canary}
            raw = json.dumps({"summary": summary_ok, "tags": tags}, ensure_ascii=False)
            should_accept = False
        elif kind == "bad_task_type":
            tags = {**valid_tags, "task_type": rng.choice(bad_enums_task)}
            raw = json.dumps({"summary": summary_ok, "tags": tags}, ensure_ascii=False)
            should_accept = False
        elif kind == "bad_outcome":
            tags = {**valid_tags, "outcome": rng.choice(bad_enums_outcome)}
            raw = json.dumps({"summary": summary_ok, "tags": tags}, ensure_ascii=False)
            should_accept = False
        else:  # secret_summary
            raw = json.dumps(
                {"summary": f"发现密钥 {SECRET} {canary}", "tags": valid_tags},
                ensure_ascii=False,
            )
            should_accept = False

        parsed = parse_reply("session_summary", raw, prompt_version=TAG_PROMPT_VERSION)
        issue = reply_issue(
            "session_summary",
            raw,
            prompt_version=TAG_PROMPT_VERSION,
            finish_reason=finish_reason,
        )

        if should_accept:
            assert parsed is not None, (idx, kind, raw, issue)
            expected_keys = {"project", "task_type", "components", "intent", "outcome"}
            assert set(parsed["tags"]) == expected_keys
            if kind == "valid_with_aliases":
                mapped, unmapped_count = map_project_names(parsed["tags"]["project"], vocab)
                assert unmapped_count == 1
                assert any(c in mapped for c in ("acme-core", "beacon-cli"))
                assert f"sidecar-{idx}" in mapped
        else:
            assert parsed is None, (idx, kind, raw)
            assert issue and issue != "bad_output", (idx, kind, issue)
            assert canary not in issue and SECRET not in issue
            if kind == "truncated_length":
                assert "truncat" in issue.lower(), (idx, issue)
