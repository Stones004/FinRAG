"""The text-quality score and its vocabulary."""

from __future__ import annotations

import pytest

from finrag.core.config import ConfigError, get_settings
from finrag.parsing.text_quality import Vocabulary, load_vocabulary, text_quality


@pytest.fixture
def vocabulary() -> Vocabulary:
    return load_vocabulary()


CLEAN = "Revenue from operations increased because the company sold more products this quarter."
GARBLED = "Revmue fom operatons Prcfit befor tsx Finmce costs Deproiation mortietion expenss"


def test_clean_text_scores_high(vocabulary) -> None:
    assert text_quality(CLEAN, vocabulary) == 1.0


def test_garbled_text_scores_low(vocabulary) -> None:
    assert text_quality(GARBLED, vocabulary) < get_settings().parsing.pdf.min_text_quality


def test_short_tokens_and_numbers_are_not_counted(vocabulary) -> None:
    # "of", "to" (< 3 letters) and digits are ignored; only "revenue" counts
    assert text_quality("of to 1,234.56 12 revenue", vocabulary) == 1.0


def test_no_countable_tokens_scores_zero(vocabulary) -> None:
    assert text_quality("12,345 67.89 (1,234)", vocabulary) == 0.0
    assert text_quality("", vocabulary) == 0.0


def test_regular_endings_are_accepted(vocabulary) -> None:
    for word in ("products", "producing", "produced", "quickly", "making", "managed"):
        assert vocabulary.knows(word)


def test_financial_terms_come_from_the_registry(vocabulary) -> None:
    assert vocabulary.knows("ebitda")
    assert vocabulary.knows("crore")


def test_threshold_is_read_from_settings(vocabulary) -> None:
    assert 0 < get_settings().parsing.pdf.min_text_quality < 1


def test_missing_vocabulary_file_is_a_config_error(isolated_repo) -> None:
    (isolated_repo / "config" / "vocab" / "common_en.txt").unlink()
    with pytest.raises(ConfigError, match="vocabulary file not found"):
        load_vocabulary()
