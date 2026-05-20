---
name: benchmark-suite
description: >-
  End-to-end benchmark pipeline that chains data collection, literature parsing,
  paper reproduction, and benchmark evaluation into a single resumable workflow.
version: 0.1.0
author: OmicsClaw
license: MIT
tags: [benchmark, pipeline, orchestrator, reproducibility, evaluation]
metadata:
  omicsclaw:
    domain: orchestrator
    requires:
      bins:
        - python3
        - git
      env: []
      config: []
    emoji: "🏗️"
    homepage: https://github.com/OmicsClaw/OmicsClaw
    os: [macos, linux, windows]
    install:
      - kind: pip
        package: requests
        bins: []
    trigger_keywords:
      - benchmark pipeline
      - full benchmark
      - benchmark suite
      - run benchmark
      - end-to-end benchmark
      - benchmark workflow
---

# Benchmark Suite Pipeline

## Purpose

Chains `benchmark-dispatch`, `reproduce-paper`, `sc-data-processing`, and
`reproducibility-evaluation` into a single resumable pipeline.

## Pipeline Stages

| Stage | Skill | Produces |
|---|---|---|
| 0 | benchmark-dispatch | literature, datasets, download |
| 1 | data-processing | processed h5ad via sc-preprocessing / sc-batch-integration |
| 2 | reproduce-paper | reproducibility result, plan, report |
| 3 | reproducibility-evaluation | metrics, comparison report, suggestions |

## Checkpoints

After each stage completes, a `.checkpoint_N` marker file is written to the
output directory. When `--resume` is passed, completed stages are skipped.

## Output Directory Structure

```
output_dir/
  benchmark_suite_summary.json   # pipeline-level summary
  benchmark_suite_report.md      # human-readable final report
  .checkpoint_00                 # dispatch complete
  .checkpoint_01                 # data process complete
  .checkpoint_02                 # reproduce complete
  .checkpoint_03                 # evaluation complete
  00_benchmark_dispatch/         # dispatch artifacts
  01_process_data/               # processed data artifacts
  02_reproduce/                  # reproducibility artifacts
  03_reproducibility_evaluation/ # evaluation artifacts
```

## Usage

```bash
# Full end-to-end pipeline
python skills/orchestrator/benchmark_suite/benchmark_suite.py \
  --benchmark-type integration \
  --query "single-cell integration benchmark" \
  --output benchmark_results

# Resume a previously interrupted run
python skills/orchestrator/benchmark_suite/benchmark_suite.py \
  --benchmark-type integration \
  --output benchmark_results \
  --resume

# Skip data download and reproduction execution
python skills/orchestrator/benchmark_suite/benchmark_suite.py \
  --benchmark-type spatial \
  --query "Visium" \
  --output benchmark_results \
  --no-download --no-reproduce-run

# With explicit input
python skills/orchestrator/benchmark_suite/benchmark_suite.py \
  --benchmark-type clustering \
  --input "https://github.com/owner/repo" \
  --repo-url https://github.com/owner/repo \
  --output benchmark_results
```
