"""Sixth review round for ``memfw history``: each test fails on af4bda5.

Secret-shaped values are assembled at runtime.
"""

from __future__ import annotations

import pytest

from memfw.history import has_secret, redact

UUID = "6a1b2c3d" + "-4e5f-6789-abcd-" + "ef0123456789"


@pytest.mark.parametrize(
    "text",
    [
        "VAULT_SECRET_ID=" + UUID,
        "secret_id: " + UUID,
        '"secret-id": "' + UUID + '"',
        "CLIENT_SECRET_ID=" + "f00ba4" + "-1234",
        "approle_secret_id=" + UUID,
    ],
)
def test_secret_id_is_a_credential(text: str) -> None:
    assert has_secret(text)
    assert "ef0123456789" not in redact(text) and "f00ba4" not in redact(text)


@pytest.mark.parametrize(
    "text",
    ["role_id: " + UUID, "token_id: 42", "api_key_id: key-name", "client_id: my-app"],
)
def test_other_id_keys_stay_clean(text: str) -> None:
    assert not has_secret(text)


def test_flag_value_with_a_slash_is_not_a_path() -> None:
    value = "AbC1" + "/dEf2+GhI3="
    assert has_secret("--token " + value)
    assert value not in redact("--token " + value)


@pytest.mark.parametrize("path", ["/run/secrets/tok", "./tok", "../tok", "~/tok"])
def test_flag_value_that_is_a_path_stays_clean(path: str) -> None:
    assert not has_secret("--token " + path)
