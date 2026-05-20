---
name: reproducibility-evaluation
description: >-
  Evaluates reproduce-paper results using benchmark metrics, generates
  comparison reports, and suggests new metrics for reproducibility workflows.
version: 0.1.0
author: OmicsClaw
license: MIT
tags: [reproducibility, evaluation, metrics]
metadata:
  omicsclaw:
    domain: orchestrator
    requires:
      bins:
        - python3
      env: []
      config: []
    emoji: "📊"
    homepage: https://github.com/OmicsClaw/OmicsClaw
    os: [macos, linux, windows]
    install:
      - kind: pip
        package: []
        bins: []
    trigger_keywords:
      - reproducibility evaluation
      - reproducibility metrics
      - compare reproduction
      - reproduction report
---

# Reproducibility Evaluation Skill

## Purpose

Evaluate reproducibility results from `reproduce-paper` workflows and
translate them into metrics, ranked comparison reports, and new
metric recommendations.

## Methodology

1. Load reproduce-paper result artifacts (`result.json`).
2. Extract reproducibility outcomes and benchmark metadata.
3. Compute a set of reproducibility metrics, including repository discovery,
   clone success, environment build success, and run success.
4. Compare multiple reproduce-paper results when provided.
5. Generate a reproducibility evaluation report.

## Output

- `reproducibility_metrics.json`: Computed metric values for each evaluated result.
- `reproducibility_report.md`: Human-readable comparison report.
- `metrics_catalog.json`: Benchmark metric catalog saved for self-documentation.

## Usage

```bash
python skills/orchestrator/reproducibility_evaluation/reproducibility_evaluation.py \
  --result-files reproduce_test/reproducibility/result.json \
  --output reproducibility_evaluation_output
```

```bash
python skills/orchestrator/reproducibility_evaluation/reproducibility_evaluation.py \
  --result-files reproduce_a/result.json reproduce_b/result.json \
  --output reproducibility_evaluation_output \
  --include-suggestions
```

```bash
python skills/orchestrator/reproducibility_evaluation/reproducibility_evaluation.py \
  --output reproducibility_evaluation_output \
  --generate-metrics-catalog
```
