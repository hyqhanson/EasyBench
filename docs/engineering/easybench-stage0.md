# EasyBench Stage 0 Notes

Stage 0 is the discovery and landing-zone step for EasyBench. It starts from a
benchmark type, searches for candidate single-cell papers and datasets, and
materializes accepted papers into per-paper data and code folders for later
pipeline stages.

## Main Entry Points

- `skills/orchestrator/benchmark_suite/benchmark_suite.py`: `run_stage_dispatch()` is the Stage 0 runner used by the full benchmark suite.
- `skills/orchestrator/benchmark_dispatch/benchmark_dispatch.py`: standalone Stage 0 CLI and orchestration logic.
- `skills/literature/core/llm_collector.py`: LLM-assisted literature search, extraction, ranking, and audit trail.
- `skills/literature/core/downloader.py`: GEO, SRA, Zenodo, cellxgene, GitHub, and generic code/data download helpers.

## Data Flow

1. The user supplies `--benchmark-type` such as `integration`, with optional
   `--query`, `--input`, `--use-llm`, and `--no-download`.
2. `generate_search_queries()` creates benchmark-specific search queries.
3. `collect_benchmark_data()` chooses one discovery path:
   - `--input`: parse a provided DOI, URL, or text directly.
   - `--use-llm`: call `llm_collect_literature()` to search multiple sources and extract data/code signals.
   - default: search PubMed and fall back to mock examples if no candidates are found.
4. LLM results are classified as `FULLY_ACCEPTED`, `DATA_ONLY`, `CODE_ONLY`, or `REJECTED`.
5. `save_accepted_papers()` persists only `FULLY_ACCEPTED` papers:
   - writes `paper_metadata.json`
   - attempts to extract `experimental_protocol.json`
   - downloads data into `benchmark_data/{benchmark_type}_{run_name}/{paper_slug}/data/`
   - clones or downloads code into `benchmark_code/{benchmark_type}_{run_name}/{paper_slug}/`

## Outputs

The full suite writes Stage 0 artifacts under:

```text
output_dir/
  00_benchmark_dispatch/
    literature/
      llm_results.json
      llm_audit.json
      llm_{identifier}/extracted_metadata.json
    workflow_plan.json
    data_summary.md
  .checkpoint_00
```

Downloaded paper assets are intentionally outside the run output directory:

```text
benchmark_data/{benchmark_type}_{run_name}/{paper_slug}/
benchmark_code/{benchmark_type}_{run_name}/{paper_slug}/
```

## Current Caveats

- The README describes a Stage 0.5 AgentScanner path, and the code contains
  `run_stage_preflight()` / `run_stage_curate()`, but the current
  `run_benchmark_suite()` sequence still calls Stage 0, Stage 1 process data,
  Stage 2 reproduce, Stage 3, and Stage 4 directly.
- Agent-specific imports are intentionally lazy in `benchmark_suite.py` so
  lightweight environments can inspect help text and run Stage 0 without
  requiring heavy single-cell packages such as `anndata`.
- Local handoff notes and API setup scratch files should not be committed.
  Use environment variables for keys, and rotate any credential that was ever
  written into a repository file.
