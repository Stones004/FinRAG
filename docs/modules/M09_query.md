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
- `settings.query.*`, `settings.llm.*`, `settings.narrative.retrieval.*`. Added with this module: `query.intent.{default_period_confidence, unfiled_period_confidence, fuzzy_min_chars, max_last_n_years}`, `query.retrieval.{k_candidates, guidance_quarters, mmr_lambda}`, `query.routing.{cases, min_accuracy}`.
- `config/intents.yaml` (owned here; the file is the reference, validated by `query/intents.py`). Keys:
  - `question_type_order` (explanation, narrative, comparison, lookup: the first type with a keyword match wins), `question_types` (keywords per type), `default_question_type_with_metric` (**lookup** since 2026-10-20: a metric with no explanation, narrative or guidance cue is answered with the figure only, no narrative search, no LLM call), `default_question_type_with_section_hint` (narrative), `llm_confidence_map` (high 0.9, medium 0.7, low 0.5), `question_starters` (words that open a question, never read as a company), `contractions`, `latest_words` ("latest", "most recent"; "current" is not one, it would eat "current ratio"), `forward_words` (plan, outlook, guidance, expect, target, will, going forward, forecast and simple forms; "expected" is left out: "expected credit loss" is a bank term).
  - `basis_words` (consolidated / standalone), `period_patterns` (regex → handler: quarter, half, nine_months, fiscal_year, last_n_years, last_quarter, last_fiscal_year), `comparison_words` (yoy, sequential, basis; removed from the text before the period patterns run, so "vs last quarter" is a comparison, not a period), `trend_words`.
  - `concept_synonyms`: taxonomy concept → words ("cfo", "pat", "revenue", "sales", "profit", ...). The taxonomy has no synonyms of its own (checked 2026-10-19). A metric synonym that is longer wins; a phrase may not belong to a metric and a concept at once (tested).
  - `companions`: metric → figures fetched beside it for a *why* question and shown only when not zero (profit after tax → profit from continuing operations, share of associates and JVs, associates' share of profit). Decision 2026-10-19, needed for the Gabriel Q1 FY27 check; the names are validated against the registries.
  - `section_hints` (words → results sections; v1 indexes `results_notes`, `segment_notes`, `audit_opinion`, `auditor_report`, `press_release` and the transcript sections), `topical_section_hints` (`press_release`: the section is added but the transcripts are still searched first; every other hint searches only that results section), `unavailable_sections` + `unavailable_names` (annual-report sources that are not indexed: a match sets `Plan.unavailable_sources`).
- Added 2026-10-20 (first live runs): `management_words` + `management_speaker_roles` (a question about what management said searches management's own turns only), `speaker_terms` (the words that name management and an analyst in a claim), `causal_words`, `growth_words` + `growth_counterparts` ("growth" with a metric that has a year-on-year growth metric reads as that metric), `default_results_sections` (results notes, segment notes, press release) + `audit_sections` (the auditor's report and opinion are searched only when the question is about the audit). Explanation cues: why, explain, reason, driver(s), cause(d), what caused/drove, how come, due to; narrative cues: said, mention(ed), commentary, management, guidance, outlook, auditor. `query.answer.min_quote_chars`.
- `config/prompts/v2/{classify,explain,guidance}.txt` (`v1/` is kept unchanged; `llm.prompt_version` is `v2`; v2 asks for JSON claims with quotes) — prompt templates (text with `{placeholders}`, filled by `llm/prompts.py`). Prompt changes = new version folder, never edit in place.

## Files — `llm/`

### `llm/base.py`
- **Does:** `LLMClient` protocol: `generate(system: str, prompt: str, json_schema: dict | None) -> LLMResponse(text, model, prompt_version, latency_ms)`.

### `llm/openai_compat.py`
- **Process:** any OpenAI-compatible server (LM Studio by default; Ollama also works at its own `/v1` URL). `POST {base_url}/chat/completions` with `model`, `temperature`, `messages` (system + user), `stream: false`. When `json_schema` is given, send it via `response_format` (`{"type": "json_schema", ...}`); if the server rejects `response_format` or the reply is not valid JSON for the schema, retry **once** with the schema stated in the prompt, then validate the JSON; still invalid → `LLMUnavailable`-style failure handled by callers. `num_ctx` is a server-side/model-load setting for these servers; it is validated against config and logged, not sent unless the server is known to accept it (TODO: confirm for LM Studio). Timeout and retries from config. `ping()` calls `GET {base_url}/models` and checks that `llm.model` is listed.
- **Output:** `LLMResponse`. Errors → `LLMUnavailable` (callers fall back to the template-only answer).
- **Hardware note:** only one GPU model is loaded at a time (OCR during ingestion, LLM at query time); target model is a 3–4B instruct GGUF.

### `llm/prompts.py`, `llm/schema.py`
- `prompts.py`: `load_prompt(name, version=None)` reads `config/prompts/<version>/<name>.txt` (version default `llm.prompt_version`); `render` fills only the named `{placeholders}` (JSON braces are left alone) and refuses a missing value. `schema.py`: a small JSON-schema checker (type, enum, required, properties, additionalProperties, items) so no dependency is added.

### `llm/fake.py`
- **Does:** deterministic fake client for tests (returns canned responses keyed by prompt hash).

## Files — `query/`

### `query/intents.py`, `query/text.py`, `query/availability.py`, `query/routing.py`, `query/mmr.py`
- `intents.py`: the registry schema and validation. `text.py`: `normalize` (lower case, contractions, possessives and punctuation removed) and `Working`, the question with understood spans blanked so that no resolver reads the same words twice. `availability.py`: what the stored data holds (`has_period`, `latest`), a balance-sheet date mapped to the fiscal period ending on it, and a stub for tests. `routing.py`: the routing-test runner (tuning and held-out reported apart; wrong-confident counted). `mmr.py`: the MMR selection.

### `query/models.py`
- `Intent(entity_isin, metrics, periods, comparison, question_type, basis, sections, confidence, defaults_applied, unresolved)`, `Plan`, `Evidence(facts, metric_values, chunks)`, `Answer(text, numbers_block, explanation, citations, interpretation, warnings)`.

### `query/entity_resolver.py`
- **Process:** match the question against watch-list aliases (exact token match, then fuzzy ≥ `query.intent.fuzzy_min_score`). One match → confidence 1.0 (exact) or score/100 (fuzzy). If the watch list has one active company and none is named → use it, recorded in `defaults_applied`. Short names derived from the entity name (the name without `Limited`/`Ltd`, and its first word when no other entity shares it: "Gabriel", "HDFC") count as exact aliases. A capitalised word that matches no entity → `unknown_company` clarification; two entities named → `multi_entity`.
- **Output:** `(isin | None, confidence, candidates)`; problems `no_entity`, `unknown_company` (a capitalised word that is not a question opener and names nothing tracked), `multi_entity`.

### `query/metric_resolver.py`
- **Process:** longest-synonym-first match against `MetricRegistry.synonyms()`; then fuzzy; also detects raw concepts through `concept_synonyms` in `intents.yaml` (lookup questions like "what was revenue"). A synonym shared by metrics of different sectors (`roe` / `roe_bank`) is chosen by the entity's `sector_type`. A word that matches several different metrics (`margin`, `margins`) is `ambiguous_metric`, listing the candidates; an unknown word that is not common English (`Sharpe`) is `unknown_metric`. Unknown metric → `unknown_metric` with the nearest registry metrics; no metric on a lookup/comparison/explanation question → `missing_metric`.
- **Output:** `(metric_ids | concept_ids, confidence, candidates)`.

### `query/period_resolver.py`
- **Process:** two phases: `parse_periods` (text only: comparison words, patterns, `latest`) and `resolve_periods` (dates and stored data). Apply `period_patterns`; two-digit years → FY; relative periods resolved against today's date and data actually available; comparison words → `yoy` / `sequential`; none found → `default_period` (latest period with data), recorded in `defaults_applied`.
- **Period not filed yet (decision 2026-10-20):** a *named* period (quarter, half, nine months, fiscal year) that ends after the company's latest filed period (any metric or fact) is never evidence. `resolve_periods` puts it in `future`; unless the question is forward-looking, the parser returns a `period_not_available` Clarification (confidence 0.0): "FY28 (Apr 2027–Mar 2028) is not available yet: the latest filed period for <company> is Q1 FY27 (Apr–Jun 2026). Ask for Q1 FY27, or leave the period out and I will use it.", with the latest period as the candidate. A period *before* the data starts is not refused: the answer says "No data for …". Relative periods keep the last-quarter rule below.
- **Last-quarter rule (decision 2026-10-19):** `last quarter` is the calendar-last quarter relative to today (2026-10-19 → Q2 FY27, Jul–Sep 2026). If that quarter has no filing for the company, the period shown is the latest quarter with data, confidence is capped below `auto_proceed_threshold` (the answer asks "Is this right?"), and the interpretation line says so, naming both: "Q2 FY27 (Jul–Sep 2026) is not filed; showing Q1 FY27 (Apr–Jun 2026), the latest filed." `last year` is the last completed fiscal year; `last N years` the N fiscal years ending there.
- **Basis comparison:** "consolidated vs standalone" (either order) is `comparison: basis`: both bases are retrieved and shown, never a clarification.
- **Output:** `(periods, comparison, confidence)`. Always echoed as "Q1 FY26 (Apr–Jun 2025)".

### `query/qtype.py`
- **Forward-looking (decision 2026-10-20):** a `forward_words` match makes the question `narrative` whatever else it says ("why will margins rise" included) and sets `QuestionKind.forward`. A narrative question needs no metric.
- **Process:** match `question_types` in the order of `question_type_order` (explanation, narrative, comparison, lookup; the first type with a match wins, so "what was the reason for…" is an explanation); a comparison word, two named periods or a multi-year period also give `comparison`; if none and a metric was found → `default_question_type_with_metric` (lookup); if none, no metric but a section hint or unavailable source → `default_question_type_with_section_hint` (narrative); section hints → `sections`. A narrative question needs no metric.

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
- **Basis precedence (decision 2026-10-19):** (1) a basis named in the question (`basis_words`); (2) the metric's `preferred_basis` (`metrics.yaml`; HDFC cost-to-income is standalone); (3) the entity's `default_basis` (Netweb: standalone); (4) `defaults.basis`. The step used is recorded in `defaults_applied`; the answer always states the basis and offers the other one when it has a value.
- **Output:** `Intent | Clarification`.

### `query/planner.py`
- **Input:** `Intent`.
- **Process:** expand into a `Plan`: metric values and their input facts for each period (+ comparison periods); narrative `SearchRequest`s (isin, the **exact periods** of the source filings (`SearchRequest.periods`, e.g. the quarters inside an H1 or FY question; fiscal years are not used to pick a quarter), sections from hints or default `[results_notes, segment_notes, audit_opinion, auditor_report, press_release]` (annual-report sections are not indexed in v1, see M08), query text = question + metric name + synonyms) — only for `explanation` and `narrative` types.
- **Forward-looking questions:** `Intent.horizon` holds the future periods named (not filed, so not searched); `Intent.periods` are the last actuals (the named periods that are filed, else the latest with data, confidence `default_period_confidence`); `Intent.search_quarters` are the latest `query.retrieval.guidance_quarters` quarters ending with the latest filed one. The planner searches the calls of `search_quarters` (transcripts first) and then the results text of the latest quarter, and never the horizon's quarters. Numbers are the last actuals.
- **Source order by period type** (period components from `core.fiscal.components_of`):
  - **Quarter (Q):** (1) transcripts for that quarter (`source_types=["transcript"]`); then (2) the results text (`source_types=["results"]`: notes to the results, segment notes, audit-opinion declaration, notes/auditor pages of the results PDF) of that quarter's filing, only to fill the remaining `max_chunks_in_prompt` slots if transcript hits don't fill them (or none exist).
  - **Half-year / nine-month (H, 9M):** transcripts for **every quarter inside the period** (H1 = Q1 + Q2 calls, 9M = Q1–Q3), not just the latest; the results text of the filings that cover the period fills remaining slots.
  - **Fiscal year (FY):** all four quarterly transcripts, then the results text of the Q4/annual filing.
  - **Annual reports are not searched in v1** (deferred, M08): the planner never builds an annual-report `SearchRequest`. A question that needs contingent liabilities, key audit matters, CARO, full accounting policies or MD&A (see `section_hints`) gets `Plan.unavailable_sources` naming them, and the answer says that source is not indexed instead of guessing.
  - Within a source type, results are ranked by relevance (RRF), not by quarter; each hit keeps the quarter (`period_label`) it came from. Explicit `section_hints` go straight to the matching results sections.
- **Output:** `Plan` (logged as JSON, including the source order).

### Reranking (moved here from M08, decision 2026-10-19)
- M08 `search` returns the RRF-fused list. The retriever asks it for `k_candidates` (> `max_chunks_in_prompt`) and then selects the prompt chunks with **maximal marginal relevance (MMR)** over the candidates' dense vectors (`mmr_lambda` balances relevance against similarity to chunks already chosen), so that four near-identical analyst questions do not crowd out the passage that answers them. Real case: HDFC "asset quality" for Q1 FY27 returns four analysts' question chunks.
- **MMR re-embeds** the candidates' texts with the M08 embedder on CPU (`SearchHit` carries no vector; M08 is unchanged; decision 2026-10-19). **No cross-encoder is built in v1:** `narrative.retrieval.reranker_enabled` stays false (M08 refuses true) and no model is downloaded. New settings `query.retrieval.k_candidates` and `mmr_lambda` are added with this module. Transcript-first ordering by period type (above) is applied to the MMR result per source group.

### `query/retriever.py`
- **Process:** execute the plan via `metrics.query`, `facts.current_view`, `narrative.search`; include `not_computed` metrics with their reasons.
- **Output:** `Evidence`, with local aliases assigned: facts/metrics `F1..Fn`, chunks `N1..Nn` (capped by `max_facts_in_prompt`, `max_chunks_in_prompt`).

### `query/formatter.py`
- **Process:** format values for display (`defaults.display_unit`, `display_decimals`, percent / days / times); also produce all acceptable string forms of each value for the numeric check.

### `query/numbers_block.py`
- **Forward-looking:** a line under the header says "These are reported actuals for <periods>; they are not management guidance for <horizon>." and each title carries "(actual)".
- **Process:** deterministic template: one line per metric/period with value, change, basis and alias; `not_computed` items shown with reason; empty evidence → "No data for … (missing: …)". Under a value, always: its **restatements** (old → new value, filings), its **basis_change** events (event and citation), its **validation warnings** and a basis fallback, all read from the metric's `details`. A lookup of a raw concept (e.g. `cfo`) shows the current fact and, from the `restatements` table, the earlier value and the filings; the filings' reason for a restatement is not stored, so none is invented.
- **Output:** markdown text. **This is the only place numbers enter the answer.**

### `query/answerer.py`
- **Claims, not prose (2026-10-20):** the model is asked (JSON schema, `response_format`) for `{"claims": [{text, cites, quote}]}`; `{"claims": []}` means it found nothing. Every passage in the prompt is labelled with what it is, its period and its speaker and role (`[N2] (earnings call; period Q1 FY27 (Apr–Jun 2026); speaker X; role analyst, HSBC) …`), and the prompt states the period asked. A reply that is not valid claims (twice) gives the figures only and the note "The language model's reply could not be read as claims…".
- **Guidance:** a forward-looking question uses `prompts/v2/guidance.txt` (what management said about plans, outlook, targets; the actuals are context only). No passages retrieved -> "Management guidance is not in the indexed filings." without an LLM call; the model may write the same sentence (no citation needed) and it is also what remains if the post-check drops every sentence.
- **Process:** if question type needs explanation and chunks exist → render `explain.txt` with numbers block (as context, aliases only) + chunks + rules (cite aliases; no arithmetic; no new numbers; say "the filings do not explain this" if passages don't) → LLM. If no chunks → skip LLM; the answer says "The filings do not explain this." and a note says no passage was usable. If LLM unavailable → numbers block + note.
- **Output:** a `Draft` (status `llm`, `no_chunks`, `llm_unavailable`, `unavailable_source` or `not_needed`, the model text, notes). The language-model server is pinged once per question: down → figures only, with the note "The language model server is not reachable…".

### `query/postcheck.py`
- **Process (claims, 2026-10-20; replaces the sentence rule):** a claim is kept only if all of these hold, and the first failure is the recorded reason:
  1. `no_citation` / `unknown_alias` / `no_passage`: it cites existing aliases and at least one passage (N..); a figure alone is no claim.
  2. `quote_too_short` / `quote_not_in_passage`: its `quote` (≥ `query.answer.min_quote_chars`) is a verbatim substring of a **cited** passage (whitespace collapsed; typographic quotes and dashes made plain).
  3. `number_not_in_quote`: every number in `text` is in the quote or equals the displayed value of a cited [F] figure. The earlier rule "numbers printed in the passage are allowed" is gone. Years and labels (FY26, Q1, 9M) are not numbers.
  4. `causal_word_not_in_quote`: a causal word in `text` (`causal_words`: due to, because, driven by, on account of, led to, …) needs a causal word in the quote.
  5. Period binding (not for forward-looking questions): `passage_other_period` (a cited passage whose period is outside the periods asked) and `quote_other_period` (the quote names a period outside them: "year ended March 31, 2026", "quarter ended June 30, 2025", "as at March 31, 2026", "Q4 FY26", "FY25").
  6. Speaker: `management_not_speaking` (text says management, but a cited passage is not a management turn), `figure_attributed_to_management` (text says management and cites an [F] figure), `analyst_not_named` (a cited passage is an analyst's turn and the text does not say analyst).
  Dropped claims are recorded with their reasons. When none is kept the answer says "The filings do not explain this." (guidance: "Management guidance is not in the indexed filings.").
- **Display:** each kept claim is a bullet with its citations and, under it, its quote as a block quote.
- **Output:** final `Answer` with `citations` mapped back to real IDs (fact IDs, metric value IDs, chunk IDs → filing + page) and `warnings`. **Each chunk citation records its `source_type`** (`transcript` | `results`), the **quarter/period it came from** (`period_label`, e.g. Q1 FY27) and, for transcripts, the speaker, role and firm, so the answer can say e.g. "(earnings call, CFO)" vs "(results notes, p. 9) or (results filing, audit-opinion declaration)".

### `query/service.py`
- **Does:** `ask(question) -> Answer | Clarification`; writes `query_log` (question, intent, plan, retrieved IDs, prompt version, model, answer, cited IDs, post-check result). `feedback(query_id, correct: bool, note)`.

### `query/cli.py`
- `finrag ask "question"`, `finrag ask --explain-plan "question"` (prints intent and plan, no LLM call), `finrag routing test` (runs routing cases).

## Tests and acceptance criteria
1. **Routing set** `tests/m09_query/routing_cases.yaml` (`finrag routing test` runs it, rules only, against the real database): 81 questions (28 marked `held_out: true`) with expected `{isin, metrics, periods, question_type, ...}` or `clarify`. Tune only against the non-held-out cases. Pass rate reported for tuning and held-out cases separately; target ≥90% correct on each, and **0 wrong-confident** (wrong intent above `auto_proceed_threshold`).
2. Unknown metric → clarification listing nearest registry metrics, no LLM answer.
3. LLM classifier returning an ID not in the shortlist → discarded, clarification returned.
4. Numbers block matches `metric_values` exactly; LLM output never adds a number that survives post-check unless it is in the claim's quote or equals a cited figure.
5. Post-check drops a claim with no citation, one citing an unknown alias, one whose quote is not in a cited passage, and one with a number that is neither in its quote nor a cited figure.
6. No chunks retrieved → answer contains numbers block + "The filings do not explain this." and no LLM call is made.
7. `not_computed` metric → answer shows the reason (e.g. blocked by `cash_ties_to_bs`).
8. LLM server down (`ping()` fails or connection refused) → numbers-only answer with a warning, no crash. Mocked HTTP tests also cover: `response_format` rejected → one retry with the schema in the prompt; invalid JSON twice → discarded.
9. Every `ask` writes one `query_log` row with plan and citations.
10. Basis default is stated in every answer.
11. Planner: a quarterly "why" question produces a transcript-first plan with the results text of that quarter as the fill; an H1 question searches the Q1 and Q2 transcripts; a 9M question the Q1–Q3 transcripts; an FY question all four quarterly transcripts; no plan ever contains an annual-report request; the plan JSON shows the order and the quarters.
12. With both transcript and results-text chunks available for a quarterly question, transcript chunks are ranked into the prompt first; results-text chunks appear only to fill remaining slots. With no transcripts indexed, the results text is used and the answer notes that no transcript was available. A question on contingent liabilities, key audit matters, CARO, accounting policies or MD&A returns `unavailable_sources` and an answer that says the source is not indexed (nothing invented).
13. Every chunk citation in `Answer.citations` carries `source_type` and the quarter it came from (and speaker/role/firm for transcripts); a multi-quarter answer shows different quarters on different citations.
14. Real-answer checks on the real database (golden, LLM faked): Gabriel FY26 `cfo` shows the current 345.27 crore (3,452.74 mn), the earlier 443.56 crore and both filings, and says no reason is given in the stored filings; HDFC cost-to-income with no basis is standalone (38.00 %, FY26) with the consolidated figure offered; Netweb FY26 vs FY25 growth carries the `basis_change` note; Gabriel Q1 FY27 PAT shows 108.13 crore = 65.38 continuing + 42.74 associates (associates share 39.53 %; 1,081.26 = 653.83 + 427.43 mn) and, since no Gabriel transcript is indexed, "the filings do not explain this".
15. **Period not filed yet:** "What is Netweb's revenue for FY28?", "Gabriel operating margin Q2 FY27", "HDFC NII H1 FY28" and a comparison naming FY27 return a `period_not_available` clarification naming the period and the latest filed; "Gabriel FY19 revenue" is answered ("No data"). Never confidence 1.0 for an unavailable period.
16. **Guidance:** "What is Netweb's capex plan for FY28?" is a narrative question over the calls of Q4 FY26 and Q1 FY27 (no FY28 search), the numbers are labelled actuals, and with nothing retrieved the answer says management guidance is not in the indexed filings.
17. **Evidence-bound claims:** quote must be a verbatim substring of a cited passage; a number in the text must be in the quote or a cited figure; a causal word in the text needs a causal word in the quote; the quote is shown under each sentence.
18. **Period binding:** with Q1 FY27 asked, a claim whose quote says "year ended March 31, 2026" (or any other period) is dropped.
19. **No auditor text for "why":** the default results search leaves out `auditor_report` and `audit_opinion`; a question about the audit still searches them. With nothing usable left the answer is the figures plus "The filings do not explain this."
20. **Speaker attribution:** a question about what management said searches management turns only; "management" is said only of management turns and never of a figure; an analyst's turn is called an analyst's.
21. **Lookup default:** "HDFC GNPA latest" returns the figure only, no search and no LLM call. "growth" resolves to the year-on-year growth metric. NIM is not computable (below), so its clarification stays.
23. **Exploratory search (2026-10-20):** a question with no metric and no question-type cue is read as exploratory when it keeps at least `query.intent.explore_min_content_words` words after the company, period, basis, starters and `explore_stopwords` are removed, and none of them is part of a metric's name ("Sharpe ratio" stays an unknown metric; "how is HDFC doing?" stays a clarification). The plan searches the filed results text of the period, every section including the audit text, with no transcript search. After retrieval the best hit's dense (cosine) score must reach `query.retrieval.explore_min_dense_score`, else the answer is a `no_narrative_match` clarification (logged, no LLM call).
24. **No Figures block** when the intent names no metric (the interpretation line then also leaves out the basis).
25. **Hedges:** a claim whose quote contains a `hedge_words` word must keep a hedge word of its own, else it is dropped (`hedge_not_kept`); the month of May is not a hedge.
26. **Note:** when the model's claims were all removed, the "no earnings-call transcript" note says "... and no usable passage was found in the results text of the filing" instead of claiming the explanation uses the results text.
22. **Replays** of the three live answers of 2026-10-19 (`test_step9_evidence.py`, `test_golden_m09.py`): the Gabriel PAT answer produces no 280.12 mn, 153.16 mn or "due to" claim; the HDFC management answer attributes no analyst turn to management; "HDFC GNPA latest" makes no LLM call.

## Known gap: NIM
Net interest margin = net interest income / average interest-earning assets. The taxonomy has no interest-earning-assets concept (checked 2026-10-20: only `bank_interest_earned`, `bank_interest_expended`, `total_assets`), so NIM cannot be computed from stored facts and "NIM" keeps its unknown-metric clarification (which offers `nii`). Needs a new concept (and an M04/M05 mapping) before a metric can be added.

## Out of scope
Multi-company comparison, conversational memory across questions, shenanigan explanations.
