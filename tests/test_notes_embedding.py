from __future__ import annotations

import json
import urllib.request

from memfw.notes import HttpEmbeddingProvider


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_http_embedding_role_instructions(monkeypatch) -> None:
    payloads: list[dict[str, object]] = []

    def fake_urlopen(request, timeout=0):  # noqa: ARG001
        payload = json.loads(request.data.decode("utf-8"))
        payloads.append(payload)
        inputs = payload["input"]
        return _Response(
            {"data": [{"index": index, "embedding": [0.1] * 4} for index in range(len(inputs))]}
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    provider = HttpEmbeddingProvider(
        "http://example.invalid/v1/embeddings",
        model="test-model",
        dimension=4,
        query_instruction="query:",
        document_instruction="document:",
    )
    assert provider.embed(["hello"], role="query") == [[0.1] * 4]
    assert provider.embed(["world"], role="document") == [[0.1] * 4]
    assert payloads[0]["input"] == ["query:hello"]
    assert payloads[1]["input"] == ["document:world"]
    assert provider.fingerprint
