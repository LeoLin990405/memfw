"""``Clock`` and ``IdGen`` ports: built-in implementations pass the contract kit."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from memfw.ports import Clock, FixedClock, IdGen, SeededIdGen, SystemClock, UuidIdGen
from memfw.ports.testing import ContractError, check_clock, check_id_gen

T0 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.mark.parametrize("clock", [SystemClock(), FixedClock(T0)], ids=type)
def test_builtin_clocks_pass_the_contract(clock: Clock) -> None:
    samples = check_clock(clock)
    assert all(value.utcoffset() is not None for value in samples)


@pytest.mark.parametrize("id_gen", [UuidIdGen(), SeededIdGen("seed")], ids=type)
def test_builtin_id_gens_pass_the_contract(id_gen: IdGen) -> None:
    check_id_gen(id_gen)


def test_system_clock_is_utc_now() -> None:
    before = datetime.now(UTC)
    value = SystemClock().now()
    assert value.tzinfo is UTC
    assert before <= value <= datetime.now(UTC)


def test_fixed_clock_moves_only_forward() -> None:
    clock = FixedClock(T0)
    assert clock.now() == clock.now() == T0
    assert clock.advance(timedelta(seconds=5)) == T0 + timedelta(seconds=5)
    clock.set(T0 + timedelta(days=1))
    assert clock.now() == T0 + timedelta(days=1)
    with pytest.raises(ValueError):
        clock.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError):
        clock.set(T0)
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        clock.set(datetime(2030, 1, 1))
    offset = FixedClock(datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8))))
    check_clock(offset)


def test_uuid_id_gen_matches_the_repository_default() -> None:
    value = UuidIdGen().new_id()
    assert str(uuid.UUID(value)) == value
    assert uuid.UUID(value).version == 4


def test_seeded_id_gen_matches_the_cli_seed_scheme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memfw.cli as cli

    monkeypatch.setattr(cli, "_ID_SEED", "fixture-seed")
    from_cli = cli._id_factory(tmp_path / "no-vault-yet")
    ours = SeededIdGen("fixture-seed")
    assert [ours.new_id() for _ in range(5)] == [from_cli() for _ in range(5)]
    fresh = SeededIdGen("fixture-seed")
    third = [fresh.new_id() for _ in range(3)][-1]
    assert SeededIdGen("fixture-seed", start=2).new_id() == third


def test_seeded_id_gen_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError):
        SeededIdGen("")
    with pytest.raises(ValueError):
        SeededIdGen("x", start=-1)


def test_ports_plug_into_repository_callables(tmp_path: Path) -> None:
    """``clock.now``/``id_gen.new_id`` are the callables Repository takes today."""

    from memfw.core.repository import Repository

    clock = FixedClock(T0)
    repository = Repository(
        tmp_path / "vault", clock=clock.now, id_factory=SeededIdGen("repo").new_id
    )
    repository.init()
    document = repository.add("Title", "body")
    expected = SeededIdGen("repo")
    assert document.id in {expected.new_id() for _ in range(10)}
    assert document.created_at == T0


class _NaiveClock:
    def now(self) -> datetime:
        return datetime(2026, 1, 1)


class _BackwardsClock:
    def __init__(self) -> None:
        self._at = T0

    def now(self) -> datetime:
        self._at -= timedelta(seconds=1)
        return self._at


class _RepeatingIds:
    def new_id(self) -> str:
        return "same"


class _BadIds:
    def new_id(self) -> str:
        return "../escape"


class _NotAClock:
    pass


@pytest.mark.parametrize("clock", [_NaiveClock(), _BackwardsClock(), _NotAClock()], ids=type)
def test_contract_rejects_broken_clocks(clock: object) -> None:
    with pytest.raises(ContractError):
        check_clock(clock)  # type: ignore[arg-type]


@pytest.mark.parametrize("id_gen", [_RepeatingIds(), _BadIds(), _NotAClock()], ids=type)
def test_contract_rejects_broken_id_gens(id_gen: object) -> None:
    with pytest.raises(ContractError):
        check_id_gen(id_gen)  # type: ignore[arg-type]
