# Literature Collection Pipeline — Implementation Details

> Last updated: 2026-06-12
> Primary files: `skills/literature/core/llm_collector.py`, `skills/literature/core/search.py`

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    llm_collect_literature()                  │
│  Entry point; runs up to 3 rounds with adaptive queries     │
│  Stops early if target_accepted FULLY_ACCEPTED papers hit   │
├─────────────────────────────────────────────────────────────┤
│  For each round:                                            │
│                                                             │
│  ① llm_generate_queries_adaptive()  —— Flash (12 queries)  │
│           │                                                 │
│  ② Parallel source search (ThreadPoolExecutor, 6 workers)   │
│     ├─ semantic_scholar     search_semantic_scholar()       │
│     ├─ springer_nature      search_springer_nature()        │
│     ├─ arxiv                search_arxiv()                  │
│     ├─ pubmed               search_pubmed_as_source()       │
│     ├─ biorxiv              search_biorxiv()                │
│     └─ europe_pmc           search_europe_pmc()             │
│           │                                                 │
│  ③ Retry zero-candidate sources with LLM-simplified queries │
│           │                                                 │
│  ④ llm_rank_articles()      —— Pro (rank + score)           │
│           │                                                 │
│  ⑤ Per-candidate: extract → filter → accept/reject          │
│     ├─ llm_extract_paper_details()  —— Flash                │
│     ├─ _recover_almost_accepted()   —— regex rescue         │
│     └─ filter_decision (rule-based tiering)                 │
│                                                             │
│  ⑥ Stop early if target_accepted FULLY_ACCEPTED papers hit  │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Model Routing: Flash vs Pro

Two DeepSeek models for different task profiles:

| Model | Role | Why |
|-------|------|-----|
| `deepseek-v4-flash` | Query generation, paper extraction | High-volume, structured JSON, speed-critical |
| `deepseek-v4-pro` | Ranking, judgment calls | Reasoning-intensive, quality-critical |

Constants in `llm_collector.py`:
```python
_MODEL_FLASH = "deepseek-v4-flash"
_MODEL_PRO   = "deepseek-v4-pro"
```

### Task assignment:

| Task | Model | Max Tokens | Temperature |
|------|-------|-----------|-------------|
| `generate_queries` | Flash | 4096 | 0.3 |
| `extract_paper_details` | Flash | 4096 | 0.2 |
| `rank_articles` | **Pro** | 16384 | 0.2 |
| `retry_queries` | Flash | 512 | 0.3 |

Pro handles only `rank_articles` — it scores relevance and sorts candidates. The filter decision (FULLY_ACCEPTED / DATA_ONLY / CODE_ONLY / REJECTED) is rule-based (no LLM call), determined by counting found accessions and code URLs from the extraction step.

---

## 3. Round Structure (Adaptive Multi-Round Search)

Up to 3 rounds, each with a different query-generation strategy:

| Round | Label | Strategy | `per_source_mult` |
|-------|-------|----------|-------------------|
| 1 | `standard` | Standard biological discovery queries | 1× |
| 2 | `explore_uncovered` | Different diseases/tissues/angles not covered in round 1 | 2× |
| 3 | `wider_narrower` | Both broader keywords AND specific atlas names | 3× |

Each round:
1. Generates 12 queries (2 per source for 6 sources) via `llm_generate_queries_adaptive()`
2. Runs all 6 sources in parallel via `ThreadPoolExecutor(max_workers=6)`
3. Per-source result limit: `max(3, max_results // 6) * per_source_mult`, capped at 10
4. Hard deadline per round: 600 seconds (10 minutes)
5. Search deadline per source call: 60 × num_sources seconds

**Early stopping**: If `target_accepted` (default 5) FULLY_ACCEPTED papers are found after round ≥ 2, the loop exits early.

### Round-specific prompt instructions:

- **Round 1**: Default biological discovery queries
- **Round 2**: "Try DIFFERENT biological angles" — specific diseases, specific tissues, developmental biology, aging, spatial biology
- **Round 3**: "Try WIDER (short general terms) and NARROWER (specific dataset IDs like GSE\d+, named atlases like Tabula Sapiens)"

### Anti-method keywords (all rounds)

**STRICTLY FORBIDDEN** keywords:
```
tool, package, pipeline, software, method, algorithm,
framework, platform, resource, workflow, protocol
```

Also avoided:
```
integration, clustering, imputation, normalization,
embedding, representation learning, batch correction
```

---

## 4. Parallel Source Search

Six sources searched in parallel per round:

```python
_unified_sources = [
    ('semantic_scholar', search_semantic_scholar, min(per_source_limit, 10), (0, 1)),
    ('springer_nature',  search_springer_nature,  min(per_source_limit, 10), (2, 3)),
    ('arxiv',            search_arxiv,            min(per_source_limit, 10), (4, 5)),
    ('pubmed',           search_pubmed_as_source, min(per_source_limit, 10), (6, 7)),
    ('biorxiv',          search_biorxiv,          min(per_source_limit, 10), (8, 9)),
    ('europe_pmc',       search_europe_pmc,       min(per_source_limit, 10), (10, 11)),
]
```

Each tuple: `(source_name, search_function, max_results, query_indices)`.

`ThreadPoolExecutor(max_workers=6)` launches all simultaneously. Each `_search_one_source()` handles:
1. Running both assigned queries
2. Enriching results with full-text (source-specific pathways)
3. Deduplication (DOI, source+ID, or title-based)

### Enrichment by source (search phase):

| Source | Enrichment path |
|--------|----------------|
| `pubmed` | Europe PMC full text → DOI page → Unpaywall OA |
| `arxiv` | DOI page → arXiv PDF full text |
| `biorxiv` | Europe PMC full text → DOI page |
| `springer_nature` | OA API metadata (`skip_pdf=True`) → DOI page fallback |
| `semantic_scholar` / `google_scholar` | DOI page → Europe PMC → Springer Nature API fallback |
| `europe_pmc` | DOI page → full text sections |

### Enrichment by source (extraction phase):

| Source | Enrichment path |
|--------|----------------|
| `pubmed` | Europe PMC full text → DOI page → Unpaywall OA PDF |
| `arxiv` | DOI page → arXiv PDF full text |
| `biorxiv` | bioRxiv PDF → DOI page → Europe PMC |
| `springer_nature` | Springer Nature PDF (via proxy) → **Springer Nature HTML (always tried)** → DOI page |
| `semantic_scholar` | DOI page → Europe PMC → Springer Nature PDF → **Springer Nature HTML** |
| `europe_pmc` | Europe PMC full text → DOI page |

---

## 5. Search Source Implementations

### 5.1 PubMed (`search_pubmed` / `search_pubmed_as_source`)

**Three-level fallback search** in `search_pubmed()`:

| Attempt | Strategy | Filters | retmax |
|---------|----------|---------|--------|
| 1 | Original query + `fft[Filter]` | Free full text + exclude Reviews/Meta/Editorial | `max_results` |
| 2 | Original query (no fft) | Exclude Reviews/Meta/Editorial | `max_results` |
| 3 | Original query (lite) | Only exclude Reviews | `max_results × 2` |

Excludes: `Review[pt]`, `Systematic Review[pt]`, `Meta-Analysis[pt]`, `Editorial[pt]`, `Comment[pt]`.

`search_pubmed_as_source()` enriches each PMID:
1. Europe PMC full text (preferred — includes full text sections for OA)
2. DOI page content (via `parse_doi`)
3. Unpaywall OA PDF (if still no full text)

### 5.2 arXiv (`search_arxiv`)

- Restricted to `cat:q-bio.*`
- Excludes titles with "Review" or "Survey"
- Auto-simplifies long queries (remove stop words, keep top 5 meaningful terms)
- Always includes "single-cell" signal
- Two-level retry: if first returns 0, retries with top 3 terms
- Rate limit: 10s between calls
- `fetch_arxiv_article()`: downloads PDF + `pypdf` extraction (up to 30,000 chars), with [15s, 25s] timeout retry

### 5.3 Semantic Scholar (`search_semantic_scholar`)

- Requires `SEMANTIC_SCHOLAR_API_KEY`
- Rate limit: 0.5s
- Fetches 3× max_results then post-filters:
  - Requires single-cell keywords: `single-cell`, `scRNA-seq`, `snRNA-seq`, `scATAC-seq`, `spatial transcriptom`, `10x genomics`, `cell atlas`, `cell type`, `cellxgene`
  - Excludes bulk keywords: `microarray`, `bulk RNA-seq`, `TCGA`, `GEO microarray`, `Affymetrix`
- Bypasses system proxy (`trust_env = False`)

### 5.4 bioRxiv/medRxiv (`search_biorxiv`)

- Via Europe PMC with `SRC:PPR` + `JOURNAL:bioRxiv OR JOURNAL:medRxiv`
- Two-level retry: simplified query if full query returns 0
- `fetch_biorxiv_article()`: PDF download → DOI page fallback

### 5.5 Europe PMC (`search_europe_pmc`)

- Query includes `GEO OR GSE OR ArrayExpress OR SRA OR cellxgene OR GitHub`
- Requires `OPEN_ACCESS:Y`
- `fetch_europe_pmc_fulltext()`: full text XML sections for OA articles

### 5.6 Springer Nature (`search_springer_nature`)

- Requires `SPRINGER_NATURE_API_KEY` or `SPRINGER_NATURE_OA_API_KEY`
- Meta API first → OA API fallback
- Returns papers from Nature-branded journals

---

## 6. Springer Nature Full-Text HTML Extraction

Located in `search.py`: `fetch_springer_nature_fulltext_html()`.

### HTTP fetch strategy:
```
URLs to try:
  1. https://link.springer.com/article/{doi}
  2. https://www.nature.com/articles/{doi_suffix}

Per-URL connection strategies (first success wins):
  1. Direct (no proxy, SSL verify)
  2. Direct (no proxy, no SSL verify)
  3. Via university proxy (if configured via env var or keyring)
```

### HTML body extraction:
1. Searches for article body markers: `data-article-body="true"` → `c-article-body` → `article-body` → `<article lang="en" id="main">` → `<article id="main">` → `<article class="app-masthead">` → any `<article>`
2. Finds closing `</article>` by depth-counting through nested tags
3. Strips scripts, styles, nav, SVG → strips HTML tags → cleans whitespace
4. Skips past initial noise to first "Abstract"
5. Truncates to 80,000 chars

### ⚠️ Critical fix (2026-06-12): Supplementary section pre-extraction

**Problem**: Nature pages put Data Availability and Code Availability at the END of the `<article>` body. The 80,000-char truncation cuts them off entirely. For paper `10.1038/s41586-026-10326-9` (Nature), the Data Availability section starts at ~80,026 chars in the cleaned text — just past the old truncation point.

**Root cause confirmed**: The main `<article>` spans from position 127,509 to 658,880 in the HTML. Data availability (`id="data-availability-section"`) is at position 359,209 (inside the article). The cleaned text is over 80,000 chars, so the section gets truncated.

**Solution**: Before stripping HTML tags, search `body_html` for section IDs:

```python
_critical_section_ids = [
    'data-availability-section',
    'code-availability-section',
]
```

For each found section:
1. Walk back to the start of the containing `<div>` tag
2. Extract ~6,000 chars of HTML
3. Clean tags and HTML attribute fragments
4. Append as `SUPPLEMENTARY_SECTIONS` after the truncated main body
5. Final return limit: 100,000 chars (was 80,000)

**Verified on** DOI `10.1038/s41586-026-10326-9` (Nature):
- ✅ Data Availability: "All processed data (including fragment files, counts matrices...)"
- ✅ Code Availability: "All analysis code is available at https://github.com/GreenleafLab/HDMA"
- ✅ GitHub: https://github.com/GreenleafLab/shareseq-pipeline

---

## 7. University Proxy Configuration

`_get_proxy_url()` in `search.py`:

Priority:
1. `UNIVERSITY_PROXY` environment variable (direct URL)
2. Windows Credential Manager via `keyring`:
   - Service: `OmicsClaw.FudanProxy`
   - Username: `24110720041`
   - Constructs: `http://{user}:{password}@libproxy.fudan.edu.cn:8080`

Used by both `fetch_springer_nature_fulltext_html()` (HTML article access) and `fetch_springer_nature_pdf()` (PDF download). The proxy enables access to paywalled Nature articles through the Fudan University library subscription.

**Verified working** (2026-06-12): Proxy returns 200 with 753KB HTML for Nature articles, including full Data/Code Availability sections.

---

## 8. Paper Extraction (`llm_extract_paper_details`)

Model: **Flash** | Max tokens: 4096 | Temperature: 0.2

Extracts structured metadata from paper text (up to 16,000 chars of input):

| Field | Type | Description |
|-------|------|-------------|
| `gse_ids` | list | GEO dataset accessions |
| `sra_ids` | list | SRA accessions |
| `cellxgene_ids` | list | CZ CELLxGENE collection IDs (UUIDs) |
| `doi` | str | Paper DOI |
| `pmid` | str | PubMed ID |
| `arxiv_ids` | list | arXiv IDs |
| `github_repos` | list | GitHub repository URLs |
| `figshare_links` | list | Figshare links |
| `zenodo_data` | list | Zenodo dataset DOIs/URLs |
| `zenodo_code` | list | Zenodo code/software DOIs/URLs |
| `data_format` | list | h5ad, mtx, rds, loom, seurat, other |
| `num_samples` | str | Number of biological samples |
| `num_cells` | str | Number of cells |
| `organism` | str | Species |
| `tissue` | str | Tissue/organ |
| `technology` | str | Sequencing technology |
| `data_quality_signals` | list | raw_counts, processed_counts, both, unknown |
| `data_origin` | str | author_collected, public_reanalysis, unclear |
| `first_hand_data` | bool | Author collected/generated the data |
| `benchmark_relevance_score` | int | 0-10 for benchmark suitability |
| `reason` | str | Explanation |
| `methods_summary` | str | Methods summary |
| `code_snippets` | str | Relevant code snippets |

### Key prompt instructions:
- Look for "Data Availability" and "Code Availability" sections — where accessions appear
- Distinguish Zenodo by content: dataset files → `zenodo_data`; software → `zenodo_code`
- Biological discovery papers scored higher than method papers even without code
- Method papers need BOTH data AND code for high score

---

## 9. Ranking (`llm_rank_articles`)

Model: **Pro** | Max tokens: 16384 | Temperature: 0.2

Three-tier rubric:

| Tier | Rank | Criteria |
|------|------|----------|
| Tier 1 | 1-2 | Biological discovery: collected original data, biological analysis, deposited data to public repos |
| Tier 2 | 3-5 | Clear data/code but computational method; OR biological discovery with data but no explicit accessions |
| Tier 3 | 6+ | Review, purely methodological, unclear data/code, or only reanalyzes public data |

---

## 10. Filter Decision (Acceptance Tiering)

Rule-based (no LLM), determined by counting found accessions and code URLs:

### Tier definitions:

| Acceptance | Condition |
|------------|-----------|
| `FULLY_ACCEPTED` | Has GSE/SRA/cellxgene/Zenodo_data **AND** GitHub/Zenodo_code/Figshare |
| `DATA_ONLY` | Has data accessions but no code repositories |
| `CODE_ONLY` | Has code repositories but no data accessions |
| `REJECTED` | Neither data nor code |

### Special cases:

#### Zenodo overlap detection
When the **same** Zenodo DOI appears in both `zenodo_data` AND `zenodo_code`, and there's no other evidence (no GSE/SRA/cellxgene/GitHub/Figshare), the paper is likely a method/tool paper. `has_data` is downgraded to `False` unless `first_hand_data=true` or `relevance >= 7`.

This fixed the case where method papers (e.g., "scater") were wrongly accepted as FULLY_ACCEPTED because their single Zenodo DOI appeared in both data and code lists.

#### first_hand_data inference
When NO explicit accessions found BUT:
- `first_hand_data == true` (LLM says authors collected the data)
- `benchmark_relevance_score >= 7`

→ Accepted as `DATA_ONLY` with `gse_ids = ['INFERRED_DATA']`. Reason annotated: "(inferred from first_hand_data + high relevance score)".

This catches papers (especially Nature/Springer) where full text was incomplete during extraction but the LLM could determine from the abstract that authors generated their own data.

---

## 11. Recovery (`_recover_almost_accepted`)

Regex-based rescue pass after initial filtering:

### DATA_ONLY → FULLY_ACCEPTED:
1. Re-grep `raw_text` for GitHub URLs (`https://github.com/...`)
2. Re-grep for Zenodo code DOIs (`10.5281/zenodo.XXXXX`)
3. If still no code: search GitHub by paper title (first 5 keywords)
4. Guard: don't upgrade if Zenodo overlap AND relevance < 7

### CODE_ONLY → FULLY_ACCEPTED:
1. Re-grep for GSE patterns (`GSE\d{4,}`)
2. Re-grep for SRA/BioProject (`SRP|SRR|PRJNA|PRJEB|ERP|DRP`)
3. Re-grep for cellxgene UUIDs
4. Re-grep for ArrayExpress (`E-MTAB-\d+`, etc.)

---

## 12. JSON Repair (`_repair_truncated_json`)

LLM responses can be truncated at token limits. The repair function:

1. **Balances brackets**: walks JSON char-by-char, tracking `{}`/`[]` nesting. Closes unclosed brackets.
2. **Handles unclosed strings**: appends closing `"` if string was cut mid-value.
3. **Strips trailing commas**: removes `,` before `}` or `]`.
4. **Strips incomplete key-value pairs** (`_strip_trailing_incomplete`): removes patterns like `"key":` with no value.

Critical for `rank_articles` (max_tokens=16384) — large candidate lists can still exceed this.

---

## 13. Retry Mechanism for Zero-Candidate Sources

After each round's parallel search, sources that returned 0 candidates get a retry:

1. LLM (Flash) generates 1 simplified, broad query per failing source
2. Query requirements: must include "single-cell" or "scRNA-seq" AND "GEO" or "GSE"
3. Each query capped at 150 chars
4. Retry results merged with deduplication

This recovers from overly-specific queries that returned nothing from certain APIs.

---

## 14. Key Environment Variables & API Credentials

| Variable | Used By | Purpose |
|----------|---------|---------|
| `SEMANTIC_SCHOLAR_API_KEY` | Semantic Scholar | API auth |
| `SPRINGER_NATURE_API_KEY` | Springer Nature Meta API | Journal search |
| `SPRINGER_NATURE_OA_API_KEY` | Springer Nature OA API | OA article search |
| `GITHUB_TOKEN` / `GH_TOKEN` | GitHub search | API auth (optional) |
| `UNIVERSITY_PROXY` | Springer Nature fetch | Proxy URL for paywalled articles |
| `DEEPSEEK_API_KEY` | LLM calls | DeepSeek API auth |
| `OmicsClaw.FudanProxy` (keyring) | Proxy construction | Fudan library proxy credentials |
| Unpaywall email | Unpaywall API | `24110720041@m.fudan.edu.cn` (hardcoded in `search.py`) |

---

## 15. File Structure

```
skills/literature/core/
├── llm_collector.py    ← Main coordinator: search → extract → filter → tier
├── search.py           ← 6 source search + full-text fetch (incl. Unpaywall, Springer HTML)
├── parser.py           ← URL/DOI parsing + text extraction
├── extractor.py        ← Regex extraction of accessions/code links
└── downloader.py       ← Dataset download (GEO/SRA/cellxgene)

skills/orchestrator/
├── benchmark_dispatch/  ← Entry point: calls llm_collect_literature
└── reproduce_paper/     ← Reproduction pipeline
```

---

## 16. Function Reference

### `llm_collector.py`

| Function | Model | Purpose |
|----------|-------|---------|
| `llm_collect_literature()` | — | Main entry point |
| `_llm_collect_impl()` | — | Core loop (rounds) |
| `llm_generate_queries()` | Flash | Generate 12 queries for round 1 |
| `llm_generate_queries_adaptive()` | Flash | Generate 12 queries with round-specific focus |
| `llm_rank_articles()` | **Pro** | Rank candidates |
| `llm_extract_paper_details()` | Flash | Extract structured metadata |
| `_recover_almost_accepted()` | — | Regex rescue for near-miss papers |
| `_repair_truncated_json()` | — | Fix truncated LLM JSON |
| `_strip_trailing_incomplete()` | — | Remove incomplete key-value pairs |
| `_call_llm()` | — | Unified LLM calling with audit + model routing |
| `_analysis_context()` | — | Benchmark-type-specific focus terms |
| `_has_enrichment()` | — | Check for DOI/FULL_TEXT in text |

### `search.py`

| Function | Purpose |
|----------|---------|
| `search_pubmed()` | 3-level fallback PubMed → PMIDs |
| `search_pubmed_as_source()` | Full PubMed search + enrichment → dicts |
| `search_arxiv()` | arXiv q-bio search |
| `search_semantic_scholar()` | SS with post-filtering |
| `search_biorxiv()` | bioRxiv/medRxiv via EPMC |
| `search_europe_pmc()` | EPMC with accession filters |
| `search_springer_nature()` | Springer Nature API search |
| `fetch_springer_nature_fulltext_html()` | ⚠️ HTML extraction with supp section pre-extraction |
| `fetch_springer_nature_pdf()` | PDF via proxy + pypdf |
| `fetch_arxiv_article()` | arXiv metadata + PDF |
| `fetch_europe_pmc_fulltext()` | EPMC full text XML |
| `fetch_biorxiv_article()` | bioRxiv PDF + metadata |
| `fetch_unpaywall_text()` | Unpaywall OA PDF discovery |
| `fetch_full_text_by_doi()` | Generic DOI page scraping |
| `_get_proxy_url()` | University proxy URL construction |
| `timed_search()` | Deadline-aware search wrapper |
