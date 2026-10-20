"""Step 1: settings, intents registry, prompts."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from finrag.core.config import ConfigError, get_settings, load_registry, load_settings
from finrag.llm.prompts import load_prompt, placeholders, render
from finrag.query.intents import load_intents, parse_intents


def test_query_settings_loaded() -> None:
    q = get_settings().query
    assert q.retrieval.k_candidates > q.answer.max_chunks_in_prompt
    assert 0 <= q.retrieval.mmr_lambda <= 1
    assert q.intent.unfiled_period_confidence < q.intent.auto_proceed_threshold
    assert q.intent.default_period_confidence >= q.intent.auto_proceed_threshold


def test_k_candidates_must_cover_prompt_chunks(isolated_repo: Path) -> None:
    path = isolated_repo / "config" / "settings.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["query"]["retrieval"]["k_candidates"] = 2
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="k_candidates"):
        load_settings(path, root=isolated_repo)


def test_intents_load_and_validate() -> None:
    intents = load_intents()
    assert intents.question_type_order == ["explanation", "narrative", "comparison", "lookup"]
    assert "cfo" in intents.concept_synonyms
    assert intents.llm_confidence_map["high"] == 0.9


def test_intents_reject_bad_concept_duplicate_phrase_and_regex() -> None:
    raw = load_registry("intents")
    bad = {**raw, "concept_synonyms": {**raw["concept_synonyms"], "not_a_concept": ["x"]}}
    with pytest.raises(ConfigError, match="not_a_concept"):
        parse_intents(bad)
    dup = {**raw, "concept_synonyms": {**raw["concept_synonyms"], "cfi": ["cfo"]}}
    with pytest.raises(ConfigError, match="both"):
        parse_intents(dup)
    regex = {**raw, "period_patterns": [{"regex": "(", "kind": "quarter"}]}
    with pytest.raises(ConfigError, match="regex"):
        parse_intents(regex)


def test_prompts_have_the_documented_placeholders() -> None:
    assert placeholders(load_prompt("classify")) == {
        "question",
        "entity_shortlist",
        "metric_shortlist",
    }
    for name in ("explain", "guidance"):
        assert placeholders(load_prompt(name)) == {
            "question",
            "period",
            "basis",
            "numbers_block",
            "chunks",
        }
    # v1 is kept as it was; v2 asks for claims with quotes
    assert "period" not in placeholders(load_prompt("explain", version="v1"))
    assert '"quote"' in load_prompt("explain") and '"quote"' not in load_prompt("explain", "v1")


def test_render_fills_known_placeholders_only() -> None:
    assert render('a {x} {"k": 1}', {"x": "1"}) == 'a 1 {"k": 1}'
    with pytest.raises(ValueError, match="missing"):
        render("{x} {y}", {"x": "1"})


def test_unknown_prompt_version_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_prompt("explain", version="v99")
