# M08 — Narrative Indexing and Search

**Package:** `src/finrag/narrative/` · **Depends on:** M02, M03 · **Tests:** `tests/m08_narrative/`

## Purpose
Turn earnings call transcripts, annual reports (and narrative text blocks in results filings) into section-aware, page-cited chunks, and search them with filters + BM25 + embeddings. Every chunk traces back to a filing and page.

## Source policy
- **Transcripts** (`earnings_call_transcript`) are the default narrative source for quarterly "why" questions. All transcript pages are indexed.
- **Annual reports** are sectioned in full, but **only sections listed in `narrative.annual_report_sections_include` are chunked and indexed**. Annual reports are 130–680 pages and mostly irrelevant; the whitelist keeps MD&A, directors' report, auditor report, CARO, notes, related-party and contingent sections (the last four are needed for the later shenanigans work). Skipped pages are never dropped silently: see `SkipSummary`.
- `unknown` is never whitelisted (unless a user adds it to the list); unknown spans are skipped and listed so patterns can be improved.
- Results-filing text blocks (`source_type = results`) are not subject to the annual-report whitelist.

## Inputs
- `processed/<filing_id>/pages.jsonl` from M03. For transcript PDFs M03 also stores per-line span info (`lines: [{text, bold, font, size}]`) so the font fallback can run without re-opening the PDF.
- XBRL text blocks from M03 (notes to results).
- `config/sections.yaml` → `narrative_sections` and `transcript_markers` (added by this module).
- `settings.narrative.annual_report_sections_include`, `settings.narrative.log_skipped_sections`.

### `sections.yaml` additions
```yaml
narrative_sections:
  mdna:              ["management discussion and analysis", "management's discussion"]
  directors_report:  ["directors' report", "board's report"]
  auditor_report:    ["independent auditor's report", "auditor's report"]
  caro:              ["annexure to the independent auditor", "companies (auditor's report) order"]
  notes:             ["notes to the financial statements", "notes forming part"]
  risk:              ["risk management", "risks and concerns"]
  corporate_governance: ["report on corporate governance"]
  related_party:     ["related party transactions", "related party disclosures"]
  contingent:        ["contingent liabilities"]

transcript_markers:
  # Derived 2026-10-11 from 4 real transcripts in data/inbox/:
  #   INE040A01034__earnings_call_transcript__Q1FY27__consolidated.pdf  (HDFC Bank)
  #   INE040A01034__earnings_call_transcript__Q4FY26__consolidated.pdf  (HDFC Bank)
  #   INE0NT901020__earnings_call_transcript__Q1FY27__standalone.pdf    (Netweb)
  #   INE0NT901020__earnings_call_transcript__Q4FY26__standalone.pdf    (Netweb)
  # Two issuers only, both with the moderator-led layout below. Re-check with every new issuer; do not assume it generalises.
  start:             '(?i)earnings\s+conference\s+call"?\s*$'         # title block; everything before it is the exchange cover letter (excluded)
  footer_line:       '^\s*Page\s+\d+\s+of\s+\d+\s*$'                 # stripped
  speaker_line:      '^(?P<speaker>[A-Z][A-Za-z.''-]*(?:\s+[A-Z][A-Za-z.''-]*){0,3}):(?:\s+(?P<text>\S.*))?$'   # "Name: text"; first-name-only speakers occur ("Pranav:", "Omkar:")
  not_speaker:       ["Sub", "SUB", "Scrip Code", "Scrip code", "Date", "Encl", "Regd. Office", "CIN", "Email", "Website", "MANAGEMENT", "MODERATOR"]
  generic_speakers:  { moderator: ["Moderator"], management: ["Management"] }   # "Management:" occurs as a label in Netweb Q4 FY26
  participant_block: '^(MANAGEMENT|MODERATOR):\s+(?:MR\.|MS\.|DR\.)'   # cover block: "MANAGEMENT: MR. NAME <dash> ROLE <dash> COMPANY", continued on indented lines
  participant_sep:   '\s*[–—\-\ufffd]+\s*'                        # the dash is extracted as U+FFFD / "--" / lost depending on PDF
  analyst_intro:     '(?is)question\s+is\s+from\s+the\s+line\s+of\s+(?P<name>[A-Z][^,.]*?)(?:\s+from\s+(?:the\s+)?|\s*,\s*)(?P<firm>[^.]*?)\s*(?:\.|Please go ahead|$)'
  qa_start:          '(?i)question\s+is\s+from\s+the\s+line\s+of'     # first match (in any speaker's turn) starts Q&A, at the start of that turn
  qa_end:            '(?i)(?:hand|handing)\s+(?:the\s+)?(?:conference|call|floor)\s+(?:back\s+)?over\s+to\s+.{0,80}closing|concludes\s+this\s+conference'
  management_roles:  ["ceo", "cfo", "managing director", "chief", "head", "president", "director", "chairman", "investor relations"]
```
Notes from the four files (confirmed by reading them):
- **Cover letter first.** Each PDF starts with the company's 1–2 page letter to the exchanges (HDFC's also carries a link to its website); the transcript proper begins at the title block ("<Company> Q1 FY27 Earnings Conference Call"). Cover pages are excluded from chunks.
- **Speaker lines** are `Name:` followed by text; names may be first-name-only. `Moderator:` is the operator; at Netweb the host (an analyst firm, listed under `MODERATOR:` in the cover block) also speaks as host in prepared remarks and **asks questions as an analyst** in Q4 FY26 ("Seema Nayak from ICICI Securities").
- **The analyst intro is not always said by the Moderator.** At Netweb the IR adviser (listed as management) reads "The next question is from the line of …"; intro text can also appear in an unlabeled turn. Scan every turn in Q&A.
- **Intro variants seen:** `First/The first/Next/The next question is from the line of <Name> from <Firm>`; `<Name>, <Firm>` (comma, no "from"); `<Name> from -- who is a Retail Investor` (no firm); first-name-only (`Pranav from Bernstein`); firm wrapping onto a second line (join whitespace before matching); the same analyst re-introduced later for a follow-up (`Jatin Kalra` twice).
- **Label loss at Netweb (text layer):** the PDF's text often drops the speaker label for continuing paragraphs, and some pages list labels stacked at the top (`Moderator: / Seema Nayak: / Sanjeev Sancheti: / Sanjay Lodha:`) with the paragraphs below unlabeled. HDFC transcripts label every turn. See attribution rules below.
- **Page furniture:** `Page N of M` footers (Netweb) and repeating header text (company name, call date) that can sit on the same line as a label (`Moderator:    HDFC Bank Limited`). Strip repeated header text before matching labels.

Starting patterns only; extend from real transcripts. Regexes are applied to whitespace-normalised turn text and must be tested against all four files plus a synthetic fixture.

## Files

### `narrative/sectioner.py`
- **Input:** pages, section patterns, `source_type`.
- **Process (annual reports):** detect heading lines (line-level: short, matches pattern, often uppercase/title-case); a section runs until the next detected heading; pages before any match → `unknown`. Also use the report's table of contents if detectable (page numbers next to section names) to confirm boundaries. **All sections are detected**, whitelisted or not.
- **Output:** `list[SectionSpan(section, start_page, end_page)]`, plus a `SkipSummary` (below) built by `partition()`.
- **Whitelist:** `partition(spans, include) -> (kept_spans, SkipSummary)`; spans whose `section` is not in `narrative.annual_report_sections_include` go to the summary with `reason = not_whitelisted`. `unknown` spans (not matching any pattern) are skipped by default and listed with `reason = unknown_section` and their page ranges.
- **`SkipSummary`:** per filing, list of `{section, start_page, end_page, reason}` (`reason` ∈ `not_whitelisted` | `unknown_section`) plus totals (pages kept / skipped / total). Written to `processed/<filing_id>/narrative_summary.json` when `narrative.log_skipped_sections` is true and also logged; with it false, skipped pages are still excluded but only the totals are kept. Pages must always be accounted for: kept + skipped = total.

### `narrative/transcript_sectioner.py`
- **Input:** transcript pages, `transcript_markers`.
- **Process:**
  1. Drop cover-letter pages (before `start`) and `footer_line`s; strip repeated header text. Cover-letter text is kept for the link-only check (M02).
  2. Read the cover participant block (`participant_block`, `participant_sep`) → name → role/company map (`MANAGEMENT` → management, `MODERATOR` → moderator).
  3. Segment into turns by `speaker_line` (excluding `not_speaker`). Stacked label runs (several `Name:` lines with no text, followed by unlabeled paragraphs) are assigned in order to the following unlabeled paragraphs only if label and paragraph counts match; otherwise try the font fallback below, and only then flag them as unattributed (`speaker = NULL`).
  3b. **Font fallback (needs verification against the Netweb PDFs).** Where the text layer lost a label, use span information from the PDF (PyMuPDF exposes per-span `flags` and `font`): speaker names in these transcripts are expected to be **bold**. A line whose first span is bold and whose bold text ends in `:` is treated as a speaker label even if the plain-text `speaker_line` pattern missed it or the label sat apart from its paragraph; the label is then attached to the paragraph that follows it. Same rule as before: if a turn still cannot be attributed, report it as unattributed — do not guess. **Unverified assumption:** it is not yet confirmed that the Netweb PDFs carry bold label spans (or that HDFC's do); check by inspecting spans in the four inbox files before relying on this, and record the result in PROGRESS. If bold is not present, this step is a no-op.
  4. Split sections: `prepared_remarks` = from `start` to the turn containing the first `qa_start` match; `qa` = from that turn up to the closing hand-over (`qa_end`); `closing_remarks` = everything after the hand-over (management's closing words, which often contain guidance such as "we expect…"). The operator's sign-off ("that concludes this conference") is excluded as an operator turn. No `qa_start` match → everything is `prepared_remarks` with a warning in the summary. No `qa_end` match → no `closing_remarks` section (nothing is guessed), and a warning is recorded.
  5. **Roles** (`speaker_role` ∈ management | analyst | moderator), first rule that applies:
     a. label `Moderator` → moderator; label `Management` → management (speaker kept as "Management");
     b. a name found by an `analyst_intro` match → **analyst from that intro onward** (matched by full name, or by first name when the intro gave only a first name), with the firm stored. This overrides the participant list (the Netweb host is a moderator until she is introduced as an analyst);
     c. a name in the participant block → its listed role;
     d. any other speaker before the first analyst intro → management; any other speaker after it who is not an analyst → management only if they answer an analyst turn, else NULL;
     e. otherwise `speaker_role = NULL`, listed in the summary. Never guess.
  6. An unlabeled turn directly after an analyst intro is that analyst's question (seen at Netweb Q4 FY26 for the first question).
  7. **QA unit** = from an analyst intro through all that analyst's turns, any management replies, and any "we'll wait for your second question"-style turns, ending at the next analyst intro or `qa_end`. A re-introduced analyst starts a new unit. Operator turns (label `Moderator`) and intro sentences are excluded from unit text but the firm is kept. A host who speaks in the prepared remarks (listed under `MODERATOR:`) is kept as a `moderator`-role unit.
- **Output:** `list[TranscriptUnit(section, speaker, speaker_role, firm, start_page, end_page, text)]`. For a QA unit, `speaker`/`speaker_role`/`firm` are the questioner's; answerers are named in the text prefix `Answer (<name>, <role>):` (or `Answer (unattributed):` where labels were lost). Prepared-remarks units are one per management turn. Closing remarks are one unit per management turn with `section = closing_remarks`; they are never attached to a question.
- **Summary:** the per-filing `narrative_summary.json` also lists unattributed turns, NULL roles, and label-count mismatches, so patterns can be improved.

### `narrative/chunker.py`
- **Input:** pages + kept spans (annual reports) or `TranscriptUnit`s (transcripts).
- **Process:** split text within each section into chunks of `narrative.chunk_tokens` with `chunk_overlap_tokens`, breaking on paragraph/sentence boundaries; never cross a section boundary; drop chunks under `min_chunk_tokens` only if they're page headers/footers (detect repeated lines across pages and strip them first). Each chunk keeps the page where it starts (and end page if it spans). **Transcript QA units are never split mid-unit when they fit in `chunk_tokens`; a longer unit is split on answer-turn boundaries first, and every resulting chunk repeats the question text as a prefix, so a question always stays with its answer.** Only whitelisted spans (annual reports) reach the chunker.
- **Output:** `chunks` rows: `chunk_id`, filing_id, isin, fiscal_year, section, `source_type` (`annual_report` | `transcript` | `results`), `speaker`/`speaker_role` (transcript chunks only, else NULL; the analyst's `firm` is kept in chunk metadata), page, seq, text, token_count.

### `narrative/embedder.py`
- **Process:** load `narrative.embedding_model` locally (sentence-transformers); embed in batches of `embedding_batch_size`; normalise vectors.
- **Output:** vectors saved to `indexes/<isin>/vectors.npy` + `ids.json`. Interface `Embedder.embed(texts) -> ndarray` so it can be mocked.

### `narrative/bm25_index.py`
- **Process:** tokenise (lowercase, keep numbers and hyphenated terms); build `rank_bm25` per ISIN; persist to `indexes/<isin>/bm25.pkl`.

### `narrative/vector_index.py`
- **Process:** `numpy` cosine search (or FAISS if `vector_backend: faiss`); supports a pre-filter list of chunk IDs.

### `narrative/search.py`
- **Input:** `SearchRequest(isin, query_text, sections=None, fiscal_years=None, filing_types=None, source_types=None, k=final_k)`.
- **Process:** (1) SQL filter on `chunks` → candidate IDs; (2) BM25 top `top_k_bm25` within candidates; (3) dense top `top_k_dense` within candidates; (4) reciprocal rank fusion with `rrf_k`; (5) optional reranker; (6) return `final_k`.
- **Output:** `list[SearchHit(chunk_id, section, source_type, speaker, speaker_role, firm, page, fiscal_year, period_label, text, scores)]`. `period_label` is the quarter/period of the source filing (joined from `filings`), e.g. `Q1 FY27` for a transcript, so callers can tag each citation with the quarter it came from.
- **Rule:** never return a chunk outside the filters, even if the result list is empty.

### `narrative/runner.py`
- **Process:** for filings with pages and no chunks: section (transcript sectioner for `earnings_call_transcript`; sectioner + whitelist partition for `annual_report`) → chunk → write `chunks` → write the skip summary → update BM25 and vectors for that ISIN (incremental append).
- **Commands:** `finrag narrative index [--isin X]`, `finrag narrative search --isin X "query" [--section mdna --fy FY25 --source transcript]`, `finrag narrative skipped --filing FIL-… ` (prints the skip summary).

## Tests and acceptance criteria
1. Synthetic annual-report fixture: sections detected with correct page ranges.
2. No chunk crosses a section boundary; every chunk has a page.
3. Repeated page headers/footers are stripped.
4. Filter test: a query matching text in company A never returns company B's chunks; FY filter respected.
5. Keyword test: an exact term (e.g. a product name) is found via BM25 even with a mocked embedder returning random vectors.
6. Paraphrase test (optional, marked slow): uses the real model on a fixture.
7. Re-indexing the same filing creates no duplicate chunks.
8. Retrieval eval: `tests/m08_narrative/retrieval_cases.yaml` (question → expected chunk IDs on fixtures); report recall@k.
9. **Whitelist:** on the synthetic annual-report fixture (which contains whitelisted sections, non-whitelisted sections such as `corporate_governance` and `risk`, and an unmatched front section), every section is detected with correct page ranges, but `chunks` contains only whitelisted sections.
10. **Skip summary:** the per-filing summary lists each skipped span with section, page range and `reason = not_whitelisted`; `unknown` spans appear in it with `reason = unknown_section`; kept + skipped pages equal the total page count (nothing silently dropped). With `log_skipped_sections: false` the skipped pages are still not indexed and totals are still recorded.
11. Changing `annual_report_sections_include` in settings changes which sections are indexed (test via settings override; no section names hardcoded in code).
12. **Transcript sections:** on a synthetic transcript fixture, everything before the first `qa_start` match is `prepared_remarks`, the Q&A is `qa`, and text after the closing hand-over is `closing_remarks` (never inside `qa`; indexed and searchable by `sections=["closing_remarks"]`); cover-letter pages and `Page N of M` footers are excluded.
13. **Q&A integrity:** each question is chunked together with its answer; no chunk contains an answer without its question text; a long answer split across chunks repeats the question prefix.
14. **Speaker metadata:** transcript chunks carry `speaker` and `speaker_role` (`management` / `analyst` / `moderator`); roles follow the attribution rules, an unresolvable role is stored as NULL and listed in the summary; non-transcript chunks have both NULL.
15. `source_type` is set on every chunk, and `SearchRequest.source_types=["transcript"]` returns only transcript chunks.
16. **Real-file checks (golden, `@pytest.mark.golden`, skipped if absent):** on the four inbox transcripts, Q&A starts at the first analyst intro; analyst names/firms extracted from the intros match a hand-checked list (including `Pranav`, `Chi Ho Wong`, `Vatsal Aggarwal` with no firm, and a firm wrapped over two lines); `Moderator` turns get role moderator; the Netweb host (`Seema Nayak`) is moderator in prepared remarks and analyst in the Q4 FY26 Q&A; turns whose label was lost are reported in the summary rather than guessed.
17a. Font fallback (synthetic PDF fixture with bold labels): a turn whose plain-text label was lost is attributed from the bold span at line start ending in `:`; with no bold span it is reported as unattributed, not guessed. Verification on the real Netweb PDFs is a golden check.
17. Analyst intro read by a non-moderator (IR adviser) still starts a QA unit; an analyst re-introduced later starts a new unit.
18. Fixtures for 9–15, 17 are generated by `tests/fixtures/make_*.py` (no real transcript or report committed); the synthetic transcript layout is a test fixture only and not evidence of any real transcript format.

## Out of scope
Answer generation (M09).
