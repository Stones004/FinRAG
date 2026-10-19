import pytest

from finrag.core import ids

SHA = "ab" * 32


def test_ulids_are_unique_and_26_chars() -> None:
    generated = [ids.new_ulid() for _ in range(20000)]
    assert len(set(generated)) == len(generated)
    assert all(len(u) == 26 for u in generated)


def test_ulids_sort_by_creation_time() -> None:
    generated = [ids.new_ulid() for _ in range(5000)]
    assert generated == sorted(generated)


def test_ulids_sort_across_milliseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter(range(1_700_000_000_000, 1_700_000_000_050))
    monkeypatch.setattr(ids, "_now_ms", lambda: next(clock))
    generated = [ids.new_ulid() for _ in range(40)]
    assert generated == sorted(generated)


def test_ulids_stay_monotonic_if_clock_steps_back(monkeypatch: pytest.MonkeyPatch) -> None:
    times = iter([2000, 1000, 1000, 3000])
    monkeypatch.setattr(ids, "_now_ms", lambda: next(times))
    generated = [ids.new_ulid() for _ in range(4)]
    assert generated == sorted(generated)
    assert len(set(generated)) == 4


def test_new_id_prefix() -> None:
    assert ids.new_id("QRY").startswith("QRY-")
    with pytest.raises(ValueError):
        ids.new_id("")


def test_filing_id_is_deterministic() -> None:
    assert ids.filing_id_from_sha(SHA) == ids.filing_id_from_sha(SHA) == "FIL-abababababab"
    with pytest.raises(ValueError):
        ids.filing_id_from_sha("abc")


def test_chunk_id() -> None:
    assert ids.chunk_id("FIL-abababababab", 7, 2) == "CHK-FIL-abababababab-p7-2"
    assert ids.chunk_id("FIL-abababababab", None, 3) == "CHK-FIL-abababababab-x-3"
