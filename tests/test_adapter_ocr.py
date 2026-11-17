"""The OCR proxy adapter (``packages/adapters/memfw-adapter-ocr``, MM4).

No real OCR engine is needed. The MCP backend talks to
``tests/fixtures/ocr/fake_mcp_server.py``, a tiny stdio MCP server that replays
recorded tool answers (``mcp_recorded.json``); the pdftotext backend parses a
recorded ``pdftotext -bbox-layout`` output. One test runs the real
``pdftotext`` on a synthetic PDF and is skipped when it is not installed.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memfw.ports import AdapterRegistry, AdaptersConfig, Availability, DeriveRequest
from memfw.ports.testing import check_deriver

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "packages" / "adapters" / "memfw-adapter-ocr"
ADAPTER_SRC = ADAPTER / "src"
if str(ADAPTER_SRC) not in sys.path:  # a workspace member, not installed here
    sys.path.insert(0, str(ADAPTER_SRC))

from memfw_adapter_ocr import (  # noqa: E402
    PROXY_FORMAT,
    McpOcrBackend,
    OcrBudget,
    OcrDeriver,
    OcrError,
    OcrLine,
    OcrPage,
    OcrResult,
    PdftotextBackend,
    Settings,
    adapter_spec,
    factory,
    generate_proxy,
    to_proxy_candidate,
    write_proxy,
)
from memfw_adapter_ocr.cli import main as cli_main  # noqa: E402
from memfw_adapter_ocr.mcp import child_env, parse_page_markdown  # noqa: E402
from memfw_adapter_ocr.pdftotext import parse_bbox_layout  # noqa: E402
from memfw_adapter_ocr.proxy import sha256_file  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "ocr"
SERVER = FIXTURES / "fake_mcp_server.py"
RECORDED = FIXTURES / "mcp_recorded.json"
ATT = "att-01a1e931-1818-772b-8713-76e6bea4a716"
NOW = datetime(2026, 1, 6, 12, 0, tzinfo=UTC)


def synthetic_pdf(texts: list[str]) -> bytes:
    """A minimal valid PDF, one US-letter page per text (Helvetica 24 at 72,700)."""

    count = len(texts)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            "<< /Type /Pages /Kids ["
            + " ".join(f"{3 + 2 * i} 0 R" for i in range(count))
            + f"] /Count {count} >>"
        ).encode(),
    ]
    font = 3 + 2 * count
    for index, text in enumerate(texts):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * index}"
            f" 0 R /Resources << /Font << /F1 {font} 0 R >> >> >>".encode()
        )
        stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return out


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def pdf(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "originals"
    root.mkdir()
    path = root / "scan.pdf"
    path.write_bytes(synthetic_pdf(["Hello calibration", "Second page drift"]))
    return path, sha256_file(path)


def _mcp(tmp_path: Path, mode: str = "normal", **kwargs: Any) -> McpOcrBackend:
    env = {
        "FAKE_MCP_RECORDED": str(RECORDED),
        "FAKE_MCP_LOG": str(tmp_path / "mcp-log.jsonl"),
        "FAKE_MCP_MODE": mode,
    }
    return McpOcrBackend([sys.executable, "-I", str(SERVER)], env=env, poll_seconds=0.01, **kwargs)


def _calls(tmp_path: Path) -> list[dict[str, Any]]:
    log = tmp_path / "mcp-log.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _run(backend: Any, path: Path, sha: str, **kwargs: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "attachment_id": ATT,
        "sha256": sha,
        "media_type": "application/pdf",
        "privacy": "shared",
        "now": NOW,
    }
    options.update(kwargs)
    return generate_proxy(path, backend=backend, **options)


# -- packaging and isolation ------------------------------------------------------------


def test_adapter_imports_only_ports_and_stdlib() -> None:
    allowed = set(sys.stdlib_module_names) | {"memfw_adapter_ocr"}
    for path in (ADAPTER_SRC / "memfw_adapter_ocr").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                if name.startswith(("memfw.", "memfw_")):
                    assert name.startswith(("memfw.ports", "memfw_ports")), (path.name, name)
                else:
                    assert name.split(".")[0] in allowed, (path.name, name)


def test_memfw_never_imports_it_and_no_host_paths_are_baked_in() -> None:
    for path in (ROOT / "src").rglob("*.py"):
        assert "memfw_adapter_ocr" not in path.read_text(encoding="utf-8"), path
    project = tomllib.loads((ADAPTER / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["dependencies"] == ["memfw-ports==0.3.0"]
    assert project["project"]["entry-points"]["memfw.adapters"] == {
        "ocr": "memfw_adapter_ocr:adapter_spec"
    }
    texts = [
        p.read_text(encoding="utf-8") for p in ADAPTER.rglob("*") if p.suffix in (".py", ".md")
    ]
    texts += [p.read_text(encoding="utf-8") for p in FIXTURES.iterdir()]
    for text in texts:
        for marker in ("/Users/", "/home/", "/opt/"):
            assert marker not in text, marker
    readme = (ADAPTER / "README.md").read_text(encoding="utf-8")
    assert "MinerU" in readme and "memfw.proxy" in readme


def test_registry_keeps_it_off_and_reports_missing_engine_settings(tmp_path: Path) -> None:
    registry = AdapterRegistry()
    registry.register(adapter_spec())
    assert registry.load("ocr", AdaptersConfig()).reason == "disabled"
    enabled = AdaptersConfig.from_mapping({"enabled": ["ocr"], "ocr": {}})
    assert registry.load("ocr", enabled).reason == "bad_mcp_command"
    config = AdaptersConfig.from_mapping(
        {
            "enabled": ["ocr"],
            "ocr": {
                "mcp_command": [sys.executable, "-I", str(SERVER)],
                "source_roots": [str(tmp_path)],
            },
        }
    )
    status = registry.load("ocr", config)
    assert status.available and isinstance(status.adapter, OcrDeriver)


def test_settings_refuse_credentials_relative_paths_and_unknown_keys(tmp_path: Path) -> None:
    with pytest.raises(OcrError, match="key_in_config"):
        Settings.from_mapping({"api_key": "x"})
    with pytest.raises(OcrError, match="bad_mcp_env"):
        Settings.from_mapping({"mcp_env": {"SOME_TOKEN": "x"}})
    with pytest.raises(OcrError, match="unknown_setting"):
        Settings.from_mapping({"cloud_endpoint": "https://example.invalid"})
    with pytest.raises(OcrError, match="source_root_not_absolute"):
        Settings.from_mapping({"source_roots": ["rel"]})
    with pytest.raises(OcrError, match="mcp_command_not_absolute"):
        McpOcrBackend(["ocr-server"])
    with pytest.raises(OcrError, match="pdftotext_not_absolute"):
        PdftotextBackend("pdftotext")
    assert PdftotextBackend(str(tmp_path / "missing")).available().reason == "pdftotext_missing"


# -- the MCP backend over the fake server -------------------------------------------


def test_text_layer_pdf_uses_pdf_text_and_a_private_copy(
    tmp_path: Path, pdf: tuple[Path, str]
) -> None:
    path, sha = pdf
    candidate = _run(_mcp(tmp_path), path, sha)
    assert (candidate["format"], candidate["version"]) == (PROXY_FORMAT, 1) == ("memfw.proxy", 1)
    assert candidate["kind"] == "extract"
    assert candidate["coverage"] == {"status": "full", "detail": "pages 1-2/2"}
    assert candidate["source"] == {"pages": 2}
    assert candidate["producer"] == {"tool": "mcp", "version": "1.2.3"}
    assert [(s["locator"], s["text"]) for s in candidate["segments"]] == [
        ({"page": 1}, "Hello calibration"),
        ({"page": 1}, "second line on page one"),
        ({"page": 2}, "Second page drift"),
    ]
    assert candidate["source_sha256"] == sha and candidate["privacy"] == "shared"
    calls = [c for c in _calls(tmp_path) if "tool" in c]
    assert [c["tool"] for c in calls] == ["pdf_info", "pdf_text"]
    handed = Path(calls[0]["arguments"]["path"])
    assert handed != path and handed.name == "original.pdf"  # a copy, never the original
    assert not handed.exists()  # removed after the run
    out_dir = Path(calls[1]["arguments"]["out_dir"])
    assert out_dir.parent.parent == handed.parent and not out_dir.exists()


def test_scanned_pdf_short_uses_ocr_pdf(tmp_path: Path, pdf: tuple[Path, str]) -> None:
    path, sha = pdf
    candidate = _run(_mcp(tmp_path, "scan", lang="ch", engine="paddle", dpi=150), path, sha)
    assert candidate["kind"] == "ocr"
    assert [s["text"] for s in candidate["segments"]] == ["Hello calibration", "Second page drift"]
    (call,) = [c for c in _calls(tmp_path) if c.get("tool") == "ocr_pdf"]
    args = call["arguments"]
    assert (args["lang"], args["engine"], args["dpi"], args["max_pages"]) == (
        "ch",
        "paddle",
        150,
        0,
    )
    assert args["timeout"] >= 1


def test_long_scanned_pdf_uses_the_async_tools(tmp_path: Path, pdf: tuple[Path, str]) -> None:
    path, sha = pdf
    candidate = _run(_mcp(tmp_path, "scan", sync_max_pages=1), path, sha)
    tools = [c["tool"] for c in _calls(tmp_path) if "tool" in c]
    assert tools == ["pdf_info", "ocr_submit", "ocr_job_status", "ocr_job_status"]
    assert candidate["coverage"] == {"status": "full", "detail": "pages 1-2/2"}


def test_page_budget_reads_part_and_coverage_says_so(tmp_path: Path, pdf: tuple[Path, str]) -> None:
    path, sha = pdf
    candidate = _run(_mcp(tmp_path, "scan"), path, sha, budget=OcrBudget(60, max_pages=1))
    assert candidate["coverage"] == {"status": "partial", "detail": "pages 1/2"}
    assert [s["locator"] for s in candidate["segments"]] == [{"page": 1}]
    (call,) = [c for c in _calls(tmp_path) if c.get("tool") == "ocr_pdf"]
    assert call["arguments"]["max_pages"] == 1


def test_image_keeps_confidence_and_has_no_box(tmp_path: Path) -> None:
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    candidate = _run(_mcp(tmp_path), image, sha256_file(image), media_type="image/png")
    assert candidate["coverage"] == {"status": "full", "detail": "image"}
    assert "source" not in candidate
    assert [(s.get("locator"), s["text"], s["confidence"]) for s in candidate["segments"]] == [
        (None, "Hello calibration", 1.0),
        (None, "Second line", 0.87),
    ]


def test_image_boxes_are_top_left_pixels_and_out_of_range_ones_dropped(tmp_path: Path) -> None:
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    candidate = _run(_mcp(tmp_path, "boxes"), image, sha256_file(image), media_type="image/png")
    first, second = candidate["segments"]
    assert first["locator"] == {"bbox": [3, 10, 120, 30]}  # pixels, top-left
    assert candidate["source"] == {"width": 400, "height": 300}
    assert "locator" not in second  # x1 900 > width 400: box dropped, text kept
    assert candidate["coverage"]["detail"] == "image; 1 box(es) dropped"


def test_structured_content_and_protocol_noise_are_handled(
    tmp_path: Path, pdf: tuple[Path, str]
) -> None:
    path, sha = pdf
    assert _run(_mcp(tmp_path, "structured"), path, sha)["segments"]
    assert _run(_mcp(tmp_path, "noise"), path, sha)["segments"]


@pytest.mark.parametrize(
    ("mode", "code"),
    [("escape", "mcp_output_outside_workdir"), ("tool_error", "mcp_tool_error")],
)
def test_server_misbehaviour_fails_closed(
    tmp_path: Path, pdf: tuple[Path, str], mode: str, code: str
) -> None:
    path, sha = pdf
    with pytest.raises(OcrError) as raised:
        _run(_mcp(tmp_path, mode), path, sha)
    assert raised.value.code == code


def test_hanging_server_times_out_and_is_stopped(tmp_path: Path, pdf: tuple[Path, str]) -> None:
    path, sha = pdf
    with pytest.raises(OcrError) as raised:
        _run(_mcp(tmp_path, "hang"), path, sha, budget=OcrBudget(timeout_seconds=1.5))
    assert raised.value.code == "mcp_timeout"


@pytest.mark.parametrize(
    ("mode", "cap", "code"),
    [
        ("flood", None, "mcp_flood"),
        ("flood_slow", None, "mcp_timeout"),
        # no cap: our answers fill the pipe the server never reads (writes block)
        ("flood", 10**6, "mcp_timeout"),
    ],
)
def test_a_server_that_floods_and_stops_reading_cannot_outlast_the_deadline(
    tmp_path: Path,
    pdf: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    cap: int | None,
    code: str,
) -> None:
    import time

    from memfw_adapter_ocr import mcp as mcp_module

    if cap is not None:
        monkeypatch.setattr(mcp_module, "MAX_SERVER_REQUESTS", cap)
    path, sha = pdf
    started = time.monotonic()
    with pytest.raises(OcrError) as raised:
        _run(_mcp(tmp_path, mode), path, sha, budget=OcrBudget(timeout_seconds=2))
    assert raised.value.code == code
    assert time.monotonic() - started < 8


def test_engine_children_are_killed_before_the_workdir_is_removed(
    tmp_path: Path, pdf: tuple[Path, str]
) -> None:
    import time

    path, sha = pdf
    candidate = _run(_mcp(tmp_path, "spawn"), path, sha)  # leader exits, child ignores SIGTERM
    assert candidate["segments"]
    pid = int((tmp_path / "mcp-log.jsonl.worker").read_text(encoding="utf-8"))
    calls = [c for c in _calls(tmp_path) if c.get("tool") == "pdf_text"]
    work = Path(calls[0]["arguments"]["out_dir"]).parent
    time.sleep(0.5)  # a surviving child would recreate its directory by now
    assert not work.exists() and not work.parent.exists()
    with pytest.raises(ProcessLookupError):
        for _ in range(50):
            os.kill(pid, 0)
            time.sleep(0.05)


def test_child_environment_has_no_proxy_or_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pdf: tuple[Path, str]
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "not-a-real-key")
    env = child_env({"https_proxy": "http://127.0.0.1:9", "OCR_LANG": "en"}, tmp_path)
    assert "HTTPS_PROXY" not in env and "https_proxy" not in env
    assert "DEEPSEEK_API_KEY" not in env and env["OCR_LANG"] == "en"
    assert env["TMPDIR"] == str(tmp_path)
    path, sha = pdf
    _run(_mcp(tmp_path), path, sha)
    seen = _calls(tmp_path)[0]["env"]
    assert "HTTPS_PROXY" not in seen and "DEEPSEEK_API_KEY" not in seen


# -- privacy, hashes ----------------------------------------------------------------


def test_local_private_is_refused_unless_cleared(tmp_path: Path, pdf: tuple[Path, str]) -> None:
    path, sha = pdf
    with pytest.raises(OcrError, match="privacy_not_cleared"):
        _run(_mcp(tmp_path), path, sha, privacy="local-private")
    assert not (tmp_path / "mcp-log.jsonl").exists()  # the server never started
    cleared = _run(
        _mcp(tmp_path),
        path,
        sha,
        privacy="local-private",
        clearance=frozenset({"public", "shared", "local-private"}),
    )
    assert cleared["privacy"] == "local-private"


class ChangingBackend:
    """Swap test double: changes the ORIGINAL while it runs."""

    name = "changing"

    def __init__(self, original: Path) -> None:
        self.original = original

    def available(self) -> Availability:
        return Availability.ok()

    def extract(self, path: Path, *, media: Any, budget: OcrBudget, workdir: Path) -> OcrResult:
        self.original.write_bytes(b"tampered")
        return OcrResult("changing", "0", "pdf", 1, (OcrPage(1, (OcrLine("x"),)),))


def test_sha256_is_checked_before_and_after(tmp_path: Path, pdf: tuple[Path, str]) -> None:
    path, sha = pdf
    with pytest.raises(OcrError, match="sha_mismatch"):
        _run(_mcp(tmp_path), path, "0" * 64)
    with pytest.raises(OcrError, match="source_changed"):
        _run(ChangingBackend(path), path, sha)
    link = tmp_path / "link.pdf"
    link.symlink_to(path)
    with pytest.raises(OcrError, match="symlink_refused"):
        _run(_mcp(tmp_path), link, sha)


# -- swap: any OcrBackend works -------------------------------------------------------


class FakeBackend:
    """Any implementation of the interface: here PDF boxes in poppler's top-left frame."""

    name = "fake"

    def __init__(self) -> None:
        self.paths: list[Path] = []

    def available(self) -> Availability:
        return Availability.ok()

    def extract(self, path: Path, *, media: Any, budget: OcrBudget, workdir: Path) -> OcrResult:
        self.paths.append(path)
        assert path.read_bytes().startswith(b"%PDF") and workdir.is_dir()
        page = OcrPage(
            1,
            (
                OcrLine("Hello", bbox=(72.0, 74.768, 242.712, 96.968), confidence=0.5),
                OcrLine("off page", bbox=(0.0, 0.0, 700.0, 10.0)),
            ),
            width=612.0,
            height=792.0,
            unit="pt",
            origin="top-left",
        )
        return OcrResult("fake", "9", "pdf", 2, (page, OcrPage(2, ())))


def test_swap_any_backend_through_the_deriver(tmp_path: Path, pdf: tuple[Path, str]) -> None:
    path, sha = pdf
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "vault.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    fake = FakeBackend()
    deriver = OcrDeriver(Settings(source_roots=(str(path.parent),)), backend=fake)
    request = DeriveRequest(
        kind="ocr_proxy",
        memory_ids=(ATT,),
        options={
            "source": "scan.pdf",
            "sha256": sha,
            "media_type": "application/pdf",
            "privacy": "public",
        },
    )
    view = check_deriver(deriver, vault=vault, request=request)
    assert view.epistemic_cap == "experience" and view.causal_cap == "none"
    candidate = json.loads(view.content)
    (segment, kept_text) = candidate["segments"]
    # poppler's top-left box (y 74.768..96.968 on a 792 pt page) → bottom-left
    assert segment["locator"] == {"page": 1, "bbox": [72, 695.032, 242.712, 717.232]}
    assert segment["confidence"] == 0.5
    assert kept_text["locator"] == {"page": 1} and kept_text["text"] == "off page"
    assert candidate["coverage"] == {
        "status": "full",
        "detail": "pages 1-2/2; no text on 1 page(s); 1 box(es) dropped",
    }
    assert fake.paths and fake.paths[0] != path
    for bad in ("../scan.pdf", "/etc/hosts", ""):
        with pytest.raises(OcrError):
            deriver.derive(
                vault, DeriveRequest("ocr_proxy", (ATT,), {**request.options, "source": bad})
            )


def test_module_factory_backend(tmp_path: Path) -> None:
    module = tmp_path / "ocr_test_backend.py"
    module.write_text(
        "from memfw.ports import Availability\n"
        "class B:\n"
        "    name = 'b'\n"
        "    def available(self): return Availability.ok()\n"
        "    def extract(self, path, *, media, budget, workdir): raise RuntimeError\n"
        "def factory(options): return B()\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        deriver = factory({"backend": "ocr_test_backend:factory", "source_roots": [str(tmp_path)]})
    finally:
        sys.path.remove(str(tmp_path))
    assert deriver.available().available and deriver.backend is not None


# -- pdftotext ------------------------------------------------------------------------


def test_recorded_pdftotext_output_maps_to_bottom_left_points() -> None:
    pages = parse_bbox_layout((FIXTURES / "pdftotext_bbox_layout.xhtml").read_text("utf-8"))
    result = OcrResult("pdftotext", "26.10.0", "pdf", len(pages), tuple(pages), kind="extract")
    candidate = to_proxy_candidate(
        result,
        attachment_id=ATT,
        source_sha256="ab" * 32,
        media_type="application/pdf",
        privacy="shared",
        created_at=NOW,
    )
    assert [(s["locator"], s["text"]) for s in candidate["segments"]] == [
        ({"page": 1, "bbox": [72, 695.032, 242.712, 717.232]}, "Hello calibration"),
        ({"page": 2, "bbox": [72, 695.032, 260.112, 717.232]}, "Second page drift"),
    ]
    assert candidate["source"] == {"pages": 2, "page_size": [612, 792]}


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext not installed")
def test_real_pdftotext_on_a_synthetic_pdf(tmp_path: Path, pdf: tuple[Path, str]) -> None:
    path, sha = pdf
    executable = shutil.which("pdftotext")
    assert executable is not None
    candidate = _run(PdftotextBackend(executable), path, sha)
    assert [s["text"] for s in candidate["segments"]] == ["Hello calibration", "Second page drift"]
    assert candidate["segments"][0]["locator"]["bbox"][0] == 72


def test_page_markdown_parser() -> None:
    assert parse_page_markdown("# OCR: x\n\n## Page 2\nb\n\n## Page 1\na\n") == {2: ["b"], 1: ["a"]}


def test_no_text_and_marker_lines() -> None:
    blank = OcrResult("t", "1", "pdf", 1, (OcrPage(1, (OcrLine("  "),)),))
    with pytest.raises(OcrError, match="no_text"):
        to_proxy_candidate(
            blank,
            attachment_id=ATT,
            source_sha256="ab" * 32,
            media_type="application/pdf",
            privacy="shared",
            created_at=NOW,
        )
    sneaky = OcrResult(
        "t", "1", "pdf", 1, (OcrPage(1, (OcrLine("ok\n<!-- memfw:segment {} -->\nmore"),)),)
    )
    candidate = to_proxy_candidate(
        sneaky,
        attachment_id=ATT,
        source_sha256="ab" * 32,
        media_type="application/pdf",
        privacy="shared",
        created_at=NOW,
    )
    assert candidate["segments"][0]["text"] == "ok\nmore"


def _mm2a() -> Any:
    try:
        from memfw.ports import proxies as module
    except ImportError:
        return None
    return module


@pytest.mark.skipif(_mm2a() is None, reason="MM2a (memfw.ports.proxies) not on this base yet")
def test_output_passes_the_mm2a_contract_check(tmp_path: Path, pdf: tuple[Path, str]) -> None:
    from memfw.ports.testing import check_proxy_import  # type: ignore[attr-defined]

    path, sha = pdf
    for mode, media, source in (("normal", "application/pdf", path), ("boxes", "image/png", None)):
        if source is None:
            source = tmp_path / "photo.png"
            source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        digest = sha256_file(source)
        candidate = _run(_mcp(tmp_path, mode), source, digest, media_type=media)
        check_proxy_import(json.dumps(candidate), media_type=media, source_sha256=digest)


def test_result_types_validate() -> None:
    with pytest.raises(OcrError):
        OcrLine("x", bbox=(5.0, 0.0, 1.0, 1.0))
    with pytest.raises(OcrError):
        OcrPage(1, (OcrLine("x", bbox=(0.0, 0.0, 1.0, 1.0)),))  # a box needs a page size
    with pytest.raises(OcrError):
        OcrResult("t", "1", "pdf", 1, (OcrPage(3, ()),))


# -- CLI ------------------------------------------------------------------------------


def test_cli_writes_a_private_file_once(
    tmp_path: Path, pdf: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    path, sha = pdf
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "mcp_command": [sys.executable, "-I", str(SERVER)],
                "mcp_env": {
                    "FAKE_MCP_RECORDED": str(RECORDED),
                    "FAKE_MCP_LOG": str(tmp_path / "mcp-log.jsonl"),
                },
                "source_roots": [str(path.parent)],
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "proxy.json"
    argv = [
        "run",
        "--settings",
        str(settings),
        "--attachment-id",
        ATT,
        "--source",
        str(path),
        "--sha256",
        sha,
        "--media-type",
        "application/pdf",
        "--privacy",
        "shared",
        "--out",
        str(out),
    ]
    assert cli_main(argv) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["segments"] == 3 and "Hello" not in json.dumps(summary)
    assert os.stat(out).st_mode & 0o077 == 0
    assert json.loads(out.read_text(encoding="utf-8"))["attachment_id"] == ATT
    assert cli_main(argv) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "out_exists"}
    assert cli_main(["check", "--settings", str(settings)]) == 0


def test_write_proxy_refuses_existing(tmp_path: Path) -> None:
    out = tmp_path / "x.json"
    write_proxy(out, {"a": 1})
    with pytest.raises(OcrError, match="out_exists"):
        write_proxy(out, {"a": 2})
