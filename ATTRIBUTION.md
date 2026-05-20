# Attribution

EasyBench is based on [**OmicsClaw**](https://github.com/TianGzlab/OmicsClaw)
( Apache 2.0 ), a multi-omics analysis platform created by the **OmicsClaw Team**.

## What is original OmicsClaw code

The following directories and files originate from the OmicsClaw project and
are used under the Apache 2.0 license:

- `omicsclaw/` — Core framework (runtime, autoagent, session, registry, etc.)
- `skills/singlecell/` — Single-cell analysis skills (sc-preprocessing, sc-clustering, ...)
- `skills/spatial/` — Spatial transcriptomics skills
- `skills/bulkrna/` — Bulk RNA-seq analysis skills
- `skills/genomics/` — Genomics analysis skills
- `skills/proteomics/` — Proteomics analysis skills
- `skills/metabolomics/` — Metabolomics analysis skills
- `skills/literature/` — Literature parsing core (except llm_collector.py)
- `skills/data/` — Shared data resources
- `bot/` — Chat bot
- `knowledge_base/` — Domain knowledge skill definitions
- `scripts/` — Build & maintenance scripts
- `docs/` — Documentation
- `templates/` — Skill templates
- `tests/` — Test suite

## What EasyBench adds / modifies

EasyBench extends OmicsClaw with a **benchmark pipeline** for reproducibility
and evaluation.  The following files were created from scratch for EasyBench:

- `skills/orchestrator/benchmark_dispatch/` — Literature & dataset collection
- `skills/orchestrator/reproduce_paper/` — Paper reproduction automation
- `skills/orchestrator/benchmark_suite/` — 5-stage pipeline orchestration
- `skills/orchestrator/reproducibility_evaluation/` — Reproducibility metrics
- `skills/orchestrator/benchmark_evaluation/` — Benchmark evaluation
- `skills/literature/core/llm_collector.py` — LLM-powered literature search

Modified from original OmicsClaw sources:

- `omicsclaw.py` — Added `benchmark-suite` pipeline alias
- `skills/literature/core/search.py` — Created for PubMed integration
- `skills/literature/core/steps.py` — Created for paper step extraction
- `skills/literature/core/downloader.py` — Enhanced for h5ad download
- `skills/literature/core/extractor.py` — Enhanced metadata extraction

## License

EasyBench as a whole is distributed under the **Apache 2.0** license.
See [LICENSE](LICENSE) for the full text.
