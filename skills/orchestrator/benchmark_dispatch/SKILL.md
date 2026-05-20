---
name: benchmark-dispatch
description: >-
  Entry point for benchmark-type driven workflows. Accepts a benchmark type
  (integration, spatial, clustering, etc.) and orchestrates data collection,
  literature parsing, and analysis setup.
version: 0.1.0
author: OmicsClaw
license: MIT
tags: [benchmark, orchestrator, workflow, data-collection, literature]
metadata:
  omicsclaw:
    domain: orchestrator
    requires:
      bins:
        - python3
      env: []
      config: []
    emoji: "🎯"
    homepage: https://github.com/OmicsClaw/OmicsClaw
    os: [macos, linux, windows]
    install:
      - kind: pip
        package: requests
        bins: []
    trigger_keywords:
      - benchmark
      - integration benchmark
      - spatial benchmark
      - clustering benchmark
      - annotation benchmark
      - trajectory benchmark
      - multiome benchmark
      - run benchmark
      - benchmark workflow
---

# Benchmark Dispatch Skill

## Purpose

Provides a unified entry point for running benchmark-type driven analyses. 
Automatically collects relevant data and literature based on the specified 
benchmark type, then sets up the analysis workflow.

## Methodology

### 1. Benchmark Type Selection

Supports the following benchmark types:
- **integration**: Multi-dataset integration and batch correction
- **matching**: Cell/spot matching across modalities
- **clustering**: Unsupervised clustering evaluation
- **annotation**: Cell type annotation and classification
- **denoising**: Noise reduction and data cleaning
- **imputation**: Missing value imputation
- **batch_correction**: Batch effect removal
- **trajectory**: Pseudotime and trajectory inference
- **celltype**: Cell type identification
- **spatial**: Spatial transcriptomics analysis
- **multiome**: Multi-omics data integration

### 2. Data Collection

- Generates targeted search queries for the benchmark type
- Performs PubMed search to discover real benchmark papers
- Parses article titles and abstracts to extract GEO/SRA/cellxgene accessions
- Applies relevance scoring to prioritize datasets
- Downloads or prepares data for analysis

### 3. Workflow Planning

- Creates a stage-by-stage analysis plan
- Identifies required OmicsClaw skills
- Estimates computational requirements
- Generates execution scripts

### 4. Analysis Setup

- Organizes data in standard formats
- Prepares configuration files
- Sets up evaluation metrics
- Creates reproducible analysis notebooks

## Output

- **workflow_plan.json**: Detailed workflow specification
- **data_summary.md**: Summary of collected datasets, literature, and download status
- **literature/**: Parsed literature results with metadata
- **downloaded_data/**: Candidate dataset downloads and metadata
- **scripts/**: Generated analysis scripts and notebooks

## Usage

```bash
# Run integration benchmark
python skills/orchestrator/benchmark_dispatch/benchmark_dispatch.py \
  --benchmark-type integration \
  --output benchmark_integration_results

# Run spatial benchmark with specific query
python skills/orchestrator/benchmark_dispatch/benchmark_dispatch.py \
  --benchmark-type spatial \
  --query "human brain cortex" \
  --output benchmark_spatial_results

# Process specific literature for clustering benchmark
python skills/orchestrator/benchmark_dispatch/benchmark_dispatch.py \
  --benchmark-type clustering \
  --input "https://pubmed.ncbi.nlm.nih.gov/12345" \
  --output benchmark_clustering_results
```