from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from memfw.cli import app


def test_consolidate_is_review_only_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    runner = CliRunner()
    init = runner.invoke(app, ["init", str(root)])
    assert init.exit_code == 0
    add = runner.invoke(
        app,
        [
            "add",
            "--title",
            "Recent memory",
            "--body",
            "recent body",
            "--root",
            str(root),
        ],
    )
    assert add.exit_code == 0, add.output
    preview = runner.invoke(app, ["consolidate", "--root", str(root), "--since", "7d"])
    assert preview.exit_code == 0, preview.output
    payload = json.loads(preview.output)
    assert payload["authority"] is False
    assert payload["candidates"]
    assert not (root / "_candidates" / "consolidate").exists()
    applied = runner.invoke(app, ["consolidate", "--root", str(root), "--since", "7d", "--apply"])
    assert applied.exit_code == 0, applied.output
    files = list((root / "_candidates" / "consolidate").glob("*.json"))
    assert len(files) == 1
    assert not (root / "memories").joinpath("candidate.md").exists()
