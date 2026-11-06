from __future__ import annotations

from pathlib import Path

from memfw.code_retrieval import CodeIndex, index_code


class FakeEmbedder:
    dimension = 2

    def embed(self, texts: list[str], *, role: str = "document") -> list[list[float]]:
        del role
        return [
            [1.0, 0.0] if ("total" in t or "sum" in t or "aggregate" in t) else [0.0, 1.0]
            for t in texts
        ]


def test_lexical_and_structure_find_python_symbols(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "billing.py").write_text(
        "def compute_total(items):\n"
        "    return sum(items)\n\n"
        "def caller():\n"
        "    return compute_total([1, 2])\n",
        encoding="utf-8",
    )
    index_path = tmp_path / "code.sqlite"
    counts = index_code(root, index_path)
    assert counts["chunks"] >= 2
    index = CodeIndex(index_path)
    exact = index.search("compute_total", limit=5)
    assert exact and exact[0]["symbol"] == "compute_total"
    definition = index.search("where is compute_total defined", limit=5)
    assert definition and definition[0]["symbol"] == "compute_total"
    callers = index.search("who calls compute_total", limit=10)
    assert any(item["symbol"] == "caller" for item in callers)


def test_semantic_paraphrase_and_lexical_fallback(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "billing.py").write_text(
        "def compute_total(items):\n    return sum(items)\n", encoding="utf-8"
    )
    embedded_path = tmp_path / "embedded.sqlite"
    index_code(root, embedded_path, embedder=FakeEmbedder())
    semantic = CodeIndex(embedded_path, embedder=FakeEmbedder())
    results = semantic.search("calculate grand aggregate", limit=5)
    assert results and results[0]["symbol"] == "compute_total"

    lexical_path = tmp_path / "lexical.sqlite"
    index_code(root, lexical_path)
    fallback = CodeIndex(lexical_path)
    assert fallback.search("compute_total", limit=5)
    assert fallback.search("calculate grand aggregate", limit=5) == []


def test_repo_map(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "cli.py").write_text("def main():\n    pass\n", encoding="utf-8")
    (root / "lib.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    index_path = tmp_path / "code.sqlite"
    index_code(root, index_path)
    payload = CodeIndex(index_path).repo_map()
    assert "cli.py" in payload["entries"]
    assert payload["files"] == 2
