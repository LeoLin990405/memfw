"""MM1a against the base ref (opt-in: set ``MEMFW_ARCH_BASE_REF``, e.g. ``origin/main``).

* Without attachments every memory command's output is byte-identical.
* A 0.3.0 reader (the base ref) of a vault this tree attached to: it reads a
  vault that only gained ``vault_id``, and fails closed on every command that
  reads the event log once an ``attach`` event exists (docs/attachments.md).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memfw.core.repository import Repository

BASE_REF = os.environ.get("MEMFW_ARCH_BASE_REF")
pytestmark = pytest.mark.skipif(
    not BASE_REF, reason="opt-in (slow): set MEMFW_ARCH_BASE_REF to compare against that ref"
)


def _harness():  # type: ignore[no-untyped-def]
    from tests.harness import byte_identical

    if not byte_identical.git_available():
        pytest.skip("git is not available")
    return byte_identical


def test_default_commands_are_byte_identical() -> None:
    byte_identical = _harness()
    results = byte_identical.compare(
        "tests.harness.attachments_probe:default_commands",
        [{"compact": False}, {"compact": True}],
        old_ref=str(BASE_REF),
    )
    for result in results:
        assert result.identical, result.diff


def _vault(root: Path, *, attach: bool) -> Path:
    counter = iter(range(1, 1000))
    repository = Repository(
        root,
        clock=lambda: datetime(2026, 11, 17, tzinfo=UTC),
        id_factory=lambda: hashlib.sha256(f"c:{next(counter)}".encode()).hexdigest()[:32],
    )
    repository.init()
    repository.add("Plain", "A plain memory.", memory_id="m-plain")
    repository.ensure_vault_id()
    if attach:
        source = root.parent / f"{root.name}-scan.pdf"
        source.write_bytes(b"%PDF-1.7 compat")
        repository.add_attachment(source)
    return root


def test_base_ref_reader_of_an_attached_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    byte_identical = _harness()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    configured = _vault(tmp_path / "configured", attach=False)
    attached = _vault(tmp_path / "attached", attach=True)
    with tempfile.TemporaryDirectory(prefix="memfw-attach-compat-") as tmp:
        tree = byte_identical.materialize(str(BASE_REF), Path(tmp) / "old")
        configured_out, attached_out = byte_identical.run_probe(
            tree,
            "tests.harness.attachments_probe:old_reader",
            [{"vault": str(configured)}, {"vault": str(attached)}],
            scratch=Path(tmp) / "runs",
        )
    # Only vault_id was added: the old tree reads and writes the vault normally.
    for row in json.loads(configured_out).values():
        assert row == {"exit": 0, "unknown_event_kind": False}
    # With an attach event the old tree refuses the event log (fail closed).
    # Reads of memory files alone still work (`show`, `index rebuild`; `list`
    # lists but warns of an event log integrity error); every command that
    # reads the log (fsck, log verify, any write) exits non-zero.
    report = json.loads(attached_out)
    for command in ("index rebuild", "show m-plain"):
        assert report.pop(command) == {"exit": 0, "unknown_event_kind": False}, command
    assert report.pop("list") == {"exit": 0, "unknown_event_kind": True}
    assert set(report) == {"fsck", "log verify", "add --title"}
    for command, row in report.items():
        assert row["exit"] != 0 and row["unknown_event_kind"], (command, row)
