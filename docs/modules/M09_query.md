# M09 — Query Engine and LLM

**Packages:** `src/finrag/llm/`, `src/finrag/query/` · **Depends on:** M07, M08 · **Tests:** `tests/m09_query/`

## Purpose
Turn a question into a verified, cited answer:
`question → intent → plan → retrieve facts/metrics/chunks → numbers block (template) + explanation (LLM) → post-check → log`.

## Design rules
- The system retrieves; the LLM never chooses what to fetch.
- Intent fields are always IDs from closed lists (watch list, metric registry, fiscal periods). The LLM can only pick from a shortlist and its output is validated.
- **Numbers in the answer come from a template, not the LLM.** The LLM writes only the explanation, citing aliases it was given.
- If retrieval finds nothing, the answer says so. No gap is filled from the model's own knowledge.
- Every parse and answer is logged in `query_log`.

## Config
- `settings.query.*`, `settings.llm.*`, `settings.narrative.retrieval.*`
- `config/intents.yaml` (owned here):
```yaml
version: 1
question_types:
  lookup:      ["what is", "what was", "how much", "show me", "value of"]
  comparison:  ["compare", " vs ", "versus", "trend", "over the last", "change in"]
  explanation: ["why", "explain", "reason", "what caused", "what drove", "due to"]
  narrative:   ["what did management say", "guidance", "outlook", "commentary", "auditor"]
default_question_type_with_metric: explanation
llm_confidence_map: { high: 0.9, medium: 0.7, low: 0.5 }
period_patterns:          # regex → handler name in period_resolver
  - { regex: "\\bq([1-4])\\s*fy\\s*'?(\\d{2,4})\\b", kind: quarter }
  - { regex: "\\bh([12])\\s*fy\\s*'?(\\d{2,4})\\b",  kind: half }
  - { regex: "\\b9m\\s*fy\\s*'?(\\d{2,4})\\b",       kind: nine_months }
  - { regex: "\\bfy\\s*'?(\\d{2,4})\\b",             kind: fiscal_year }
  - { regex: "\\blast\\s+(\\d+)\\s+years?\\b",       kind: last_n_years }
  - { regex: "\\blast quarter\\b",                   kind: last_quarter }
  - { regex: "\\blast year\\b",                      kind: last_fiscal_year }
comparison_words:
  yoy:        ["year on year", "yoy", "vs last year", "compared to last year"]
  sequential: ["qoq", "quarter on quarter", "sequential", "vs last quarter"]
section_hints:            # words that narrow narrative search to sections
  auditor_report: ["auditor", "audit opinion", "qualified"]
  related_party:  ["related party"]
  contingent:     ["contingent"]
  mdna:           ["management", "outlook", "guidance"]
```
- `config/prompts/v1/classify.txt` and `config/prompts/v1/explain.txt` — prompt templates (text with `{placeholders}`). Prompt changes = new version folder, never edit in place.

## Files — `llm/`

### `llm/base.py`
- **Does:** `LLMClient` protocol: `generate(system: str, prompt: str, json_schema: dict | None) -> LLMResponse(text, model, prompt_version, latency_ms)`.

### `llm/openai_compat.py`
- **Process:** any OpenAI-compatible server (LM Studio by default; Ollama also works at its own `/v1` URL). `POST {base_url}/chat/completions` with `model`, `temperature`, `messages` (system + user), `stream: false`. When `json_schema` is given, send it via `response_format` (`{"type": "json_schema", ...}`); if the server rejects `response_format` or the reply is not valid JSON for the schema, retry **once** with the schema stated in the prompt, then validate the JSON; still invalid → `LLMUnavailable`-style failure handled by callers. `num_ctx` is a server-side/model-load setting for these servers; it is validated against config and logged, not sent unless the server is known to accept it (TODO: confirm for LM Studio). Timeout and retries from config. `ping()` calls `GET {base_url}/models` and checks that `llm.model` is listed.
- **Output:** `LLMResponse`. Errors → `LLMUnavailable` (callers fall back to the template-only answer).
- **Hardware note:** only one GPU model is loaded at a time (OCR during ingestion, LLM at query time); target model is a 3–4B instruct GGUF.

### `llm/fake.py`
- **Does:** deterministic fake client for tests (returns canned responses keyed by prompt hash).

## Files — `query/`

### `query/models.py`
- `Intent(entity_isin, metrics, periods, comparison, question_type, basis, sections, confidence, defaults_applied, unresolved)`, `Plan`, `Evidence(facts, metric_values, chunks)`, `Answer(text, numbers_block, explanation, citations, interpretation, warnings)`.

### `query/entity_resolver.py`
- **Process:** match the question against watch-list aliases (exact token match, then fuzzy ≥ `query.intent.fuzzy_min_score`). One match → confidence 1.0 (exact) or score/100 (fuzzy). If the watch list has one active company and none is named → use it, recorded in `defaults_applied`.
- **Output:** `(isin | None, confidence, candidates)`.

### `query/metric_resolver.py`
- **Process:** longest-synonym-first match against `MetricRegistry.synonyms()`; then fuzzy; also detects raw concept names from the taxonomy (lookup questions like "what was revenue").
- **Output:** `(metric_ids | concept_ids, confidence, candidates)`.

### `query/period_resolver.py`
- **Process:** apply `period_patterns`; two-digit years → FY; relative periods resolved against today's date and data actually available; comparison words → `yoy` / `sequential`; none found → `default_period` (latest period with data), recorded in `defaults_applied`.
- **Output:** `(periods, comparison, confidence)`. Always echoed as "Q1 FY26 (Apr–Jun 2025)".

### `query/qtype.py`
- **Process:** match `question_types`; if none and a metric was found → `default_question_type_with_metric`; section hints → `sections`.

### `query/llm_classifier.py`
- **When:** a resolver returned confidence below `auto_proceed_threshold` and `llm_fallback_enabled`.
- **Input:** question + shortlist (≤ `shortlist_size`) for the weak field(s) with ID, name, one-line description.
- **Process:** prompt `classify.txt`; JSON schema `{entity: id|none, metric: id|none, confidence: high|medium|low, reason}`; validate IDs ∈ shortlist; anything else → discard.
- **Output:** chosen IDs with mapped confidence (high 0.9, medium 0.7, low 0.5 — values in `intents.yaml` under `llm_confidence_map`).

### `query/intent_parser.py`
- **Process:** run resolvers → LLM fallback for weak fields → overall confidence = min of field confidences → decide:
  - ≥ `auto_proceed_threshold` → proceed, show interpretation;
  - between thresholds → proceed, show interpretation and ask "Is this right?";
  - < `clarify_threshold` or a required field unresolved → return a `Clarification` with top candidates.
  Multiple entities and `multi_entity_enabled: false` → clarification "one company at a time".
- **Output:** `Intent | Clarification`.

### `query/planner.py`
- **Input:** `Intent`.
- **Process:** expand into a `Plan`: metric values and their input facts for each period (+ comparison periods); narrative `SearchRequest`s (isin, fiscal years covering the periods, sections from hints or default `[mdna, directors_report, notes]`, query text = question + metric name + synonyms) — only for `explanation` and `narrative` types.
- **Source order by period type** (period components from `core.fiscal.components_of`):
  - **Quarter (Q):** (1) transcripts for that quarter (`source_types=["transcript"]`); then (2) annual-report whitelisted sections, only to fill the remaining `max_chunks_in_prompt` slots if transcript hits don't fill them (or none exist).
  - **Half-year / nine-month (H, 9M):** transcripts for **every quarter inside the period** (H1 = Q1 + Q2 calls, 9M = Q1–Q3), not just the latest. Annual-report sections are searched **only if the period ends at the fiscal year-end** (i.e. never for H1 or 9M; an H2 question ends at year-end and does include them).
  - **Fiscal year (FY):** all four quarterly transcripts plus annual-report whitelisted sections, searched together.
  - Within a source type, results are ranked by relevance (RRF), not by quarter; each hit keeps the quarter (`period_label`) it came from. Explicit `section_hints` (e.g. auditor) go straight to annual-report sections.
- **Output:** `Plan` (logged as JSON, including the source order).

### `query/retriever.py`
- **Process:** execute the plan via `metrics.query`, `facts.current_view`, `narrative.search`; include `not_computed` metrics with their reasons.
- **Output:** `Evidence`, with local aliases assigned: facts/metrics `F1..Fn`, chunks `N1..Nn` (capped by `max_facts_in_prompt`, `max_chunks_in_prompt`).

### `query/formatter.py`
- **Process:** format values for display (`defaults.display_unit`, `display_decimals`, percent / days / times); also produce all acceptable string forms of each value for the numeric check.

### `query/numbers_block.py`
- **Process:** deterministic template: one line per metric/period with value, change, basis and alias; `not_computed` items shown with reason; empty evidence → "No data for … (missing: …)".
- **Output:** markdown text. **This is the only place numbers enter the answer.**

### `query/answerer.py`
- **Process:** if question type needs explanation and chunks exist → render `explain.txt` with numbers block (as context, aliases only) + chunks + rules (cite aliases; no arithmetic; no new numbers; say "the filings do not explain this" if passages don't) → LLM. If no chunks → skip LLM, add "No explanation found in the indexed filings." If LLM unavailable → numbers block + note.
- **Output:** draft `Answer`.

### `query/postcheck.py`
- **Process:** split explanation into sentences; for each: must cite ≥1 alias (if `require_citation_per_sentence`); all cited aliases must exist in evidence; if `numeric_check_enabled`, every number in the sentence must match a formatted value of a cited alias — otherwise drop the sentence. Record dropped sentences and reasons.
- **Output:** final `Answer` with `citations` mapped back to real IDs (fact IDs, metric value IDs, chunk IDs → filing + page) and `warnings`. **Each chunk citation records its `source_type`** (`transcript` | `annual_report` | `results`), the **quarter/period it came from** (`period_label`, e.g. Q1 FY27) and, for transcripts, the speaker, role and firm, so the answer can say e.g. "(earnings call, CFO)" vs "(annual report, MD&A p. 42)".

### `query/service.py`
- **Does:** `ask(question) -> Answer | Clarification`; writes `query_log` (question, intent, plan, retrieved IDs, prompt version, model, answer, cited IDs, post-check result). `feedback(query_id, correct: bool, note)`.

### `query/cli.py`
- `finrag ask "question"`, `finrag ask --explain-plan "question"` (prints intent and plan, no LLM call), `finrag routing test` (runs routing cases).

## Tests and acceptance criteria
1. **Routing set** `tests/m09_query/routing_cases.yaml`: ≥50 questions with expected `{isin, metrics, periods, question_type}` or `clarify`. Pass rate reported; target ≥90% correct, and **0 wrong-confident** (wrong intent above `auto_proceed_threshold`).
2. Unknown metric → clarification listing nearest registry metrics, no LLM answer.
3. LLM classifier returning an ID not in the shortlist → discarded, clarification returned.
4. Numbers block matches `metric_values` exactly; LLM output never adds a number that survives post-check unless it equals a cited value.
5. Post-check drops a sentence with no citation, one citing an unknown alias, and one with a number not matching its cited fact.
6. No chunks retrieved → answer contains numbers block + "No explanation found…" and no LLM call is made.
7. `not_computed` metric → answer shows the reason (e.g. blocked by `cash_ties_to_bs`).
8. LLM server down (`ping()` fails or connection refused) → numbers-only answer with a warning, no crash. Mocked HTTP tests also cover: `response_format` rejected → one retry with the schema in the prompt; invalid JSON twice → discarded.
9. Every `ask` writes one `query_log` row with plan and citations.
10. Basis default is stated in every answer.
11. Planner: a quarterly "why" question produces a transcript-first plan with annual-report fallback; an H1 question searches the Q1 and Q2 transcripts and no annual report; a 9M question the Q1–Q3 transcripts and no annual report; an FY question all four quarterly transcripts plus annual-report sections; the plan JSON shows the order and the quarters.
12. With both transcript and annual-report chunks available for a quarterly question, transcript chunks are ranked into the prompt first; annual-report chunks appear only to fill remaining slots. With no transcripts indexed, the annual-report fallback is used and the answer notes that no transcript was available.
13. Every chunk citation in `Answer.citations` carries `source_type` and the quarter it came from (and speaker/role/firm for transcripts); a multi-quarter answer shows different quarters on different citations.

## Out of scope
Multi-company comparison, conversational memory across questions, shenanigan explanations.
