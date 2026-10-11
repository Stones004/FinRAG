# M08 — Narrative Indexing and Search

**Package:** `src/finrag/narrative/` · **Depends on:** M02, M03 · **Tests:** `tests/m08_narrative/`

## Purpose
Turn annual reports (and narrative text blocks in results filings) into section-aware, page-cited chunks, and search them with filters + BM25 + embeddings. Every chunk traces back to a filing and page.

## Inputs
- `processed/<filing_id>/pages.jsonl` from M03.
- XBRL text blocks from M03 (notes to results).
- `config/sections.yaml` → `narrative_sections` (added by this module).

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
```
Starting patterns; extend from real reports.

## Files

### `narrative/sectioner.py`
- **Input:** pages, section patterns.
- **Process:** detect heading lines (line-level: short, matches pattern, often uppercase/title-case); a section runs until the next detected heading; pages before any match → `unknown`. Also use the report's table of contents if detectable (page numbers next to section names) to confirm boundaries.
- **Output:** `list[SectionSpan(section, start_page, end_page)]`.

### `narrative/chunker.py`
- **Input:** pages + spans.
- **Process:** split text within each section into chunks of `narrative.chunk_tokens` with `chunk_overlap_tokens`, breaking on paragraph/sentence boundaries; never cross a section boundary; drop chunks under `min_chunk_tokens` only if they're page headers/footers (detect repeated lines across pages and strip them first). Each chunk keeps the page where it starts (and end page if it spans).
- **Output:** `chunks` rows: `chunk_id`, filing_id, isin, fiscal_year, section, page, seq, text, token_count.

### `narrative/embedder.py`
- **Process:** load `narrative.embedding_model` locally (sentence-transformers); embed in batches of `embedding_batch_size`; normalise vectors.
- **Output:** vectors saved to `indexes/<isin>/vectors.npy` + `ids.json`. Interface `Embedder.embed(texts) -> ndarray` so it can be mocked.

### `narrative/bm25_index.py`
- **Process:** tokenise (lowercase, keep numbers and hyphenated terms); build `rank_bm25` per ISIN; persist to `indexes/<isin>/bm25.pkl`.

### `narrative/vector_index.py`
- **Process:** `numpy` cosine search (or FAISS if `vector_backend: faiss`); supports a pre-filter list of chunk IDs.

### `narrative/search.py`
- **Input:** `SearchRequest(isin, query_text, sections=None, fiscal_years=None, filing_types=None, k=final_k)`.
- **Process:** (1) SQL filter on `chunks` → candidate IDs; (2) BM25 top `top_k_bm25` within candidates; (3) dense top `top_k_dense` within candidates; (4) reciprocal rank fusion with `rrf_k`; (5) optional reranker; (6) return `final_k`.
- **Output:** `list[SearchHit(chunk_id, section, page, fiscal_year, text, scores)]`.
- **Rule:** never return a chunk outside the filters, even if the result list is empty.

### `narrative/runner.py`
- **Process:** for filings with pages and no chunks: section → chunk → write `chunks` → update BM25 and vectors for that ISIN (incremental append).
- **Commands:** `finrag narrative index [--isin X]`, `finrag narrative search --isin X "query" [--section mdna --fy FY25]`.

## Tests and acceptance criteria
1. Synthetic annual-report fixture: sections detected with correct page ranges.
2. No chunk crosses a section boundary; every chunk has a page.
3. Repeated page headers/footers are stripped.
4. Filter test: a query matching text in company A never returns company B's chunks; FY filter respected.
5. Keyword test: an exact term (e.g. a product name) is found via BM25 even with a mocked embedder returning random vectors.
6. Paraphrase test (optional, marked slow): uses the real model on a fixture.
7. Re-indexing the same filing creates no duplicate chunks.
8. Retrieval eval: `tests/m08_narrative/retrieval_cases.yaml` (question → expected chunk IDs on fixtures); report recall@k.

## Out of scope
Answer generation (M09).
