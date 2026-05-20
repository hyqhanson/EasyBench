---
name: literature
description: >-
  Parse scientific literature (PDFs, URLs, DOIs) to extract GEO, SRA, and
  cellxgene accessions, metadata, and candidate datasets for downstream omics analysis.
  Supports benchmark-type aware extraction to prioritize relevant datasets.
version: 0.1.1
author: OmicsClaw
license: MIT
tags: [literature, pdf-parsing, geo, sra, cellxgene, pubmed, data-download, benchmark]
metadata:
  omicsclaw:
    domain: literature
    requires:
      bins:
        - python3
      env: []
      config: []
    emoji: "📄"
    homepage: https://github.com/OmicsClaw/OmicsClaw
    os: [macos, linux, windows]
    install:
      - kind: pip
        package: pypdf
        bins: []
    trigger_keywords:
      - parse paper
      - literature
      - GEO accession
      - SRA accession
      - cellxgene
      - download dataset
      - PDF extract
      - PubMed
      - DOI
      - benchmark
---

# Literature Parsing Skill

## Purpose

Parse scientific literature (PDFs, URLs, DOIs) to extract GEO、SRA 和 cellxgene 数据集引用，生成元信息并下载候选数据源。

## Methodology

### 1. Input Processing

Accepts multiple input types:
- **URL**: PubMed、bioRxiv、期刊文章链接或 cellxgene 页面
- **DOI**: Digital Object Identifier (e.g., 10.1038/s41586-021-03569-1)
- **PubMed ID**: PMID (e.g., 33234567)
- **PDF**: 上传的科学论文
- **Text**: 原始文本包含数据集引用

### 2. Metadata Extraction

Extracts structured information:
- **GEO Accessions**: GSE、GSM、GPL
- **SRA Accessions**: SRP、SRR、SRS、ERP、ERS、DRP、DRS
- **cellxgene datasets**: dataset slugs or URLs
- **Organism**: Species
- **Tissue**: Tissue or organ
- **Technology**: Sequencing platform
- **Benchmark Relevance**: Score based on specified benchmark type

### 3. Data Download

Downloads candidate data sources:
- GEO: supplementary files from NCBI GEO FTP
- SRA: metadata XML and optional SRA run files
- cellxgene: page-derived candidate files such as `.h5ad`, `.cxg`, `.zip`
- Organizes results under `data/<accession>/`
- Generates JSON metadata for each source

### 4. Error Handling

- **Retry with fallbacks**: network retries and HTML listing parsing
- **Partial results**: preserves successfully downloaded artifacts even if some sources fail
- **Logging**: prints progress and failure details

## Output

- **data/GSE*/**, **data/SRP*/**, **data/<cellxgene_slug>/**: downloaded or candidate files
- **output/literature-parse_*/report.md**: extraction and download report
- **output/literature-parse_*/extracted_metadata.json**: structured metadata

## Usage

```bash
python skills/literature/literature_parse.py \
  --input "https://pubmed.ncbi.nlm.nih.gov/12345" \
  --benchmark-type integration \
  --output output/literature_results

python skills/literature/literature_parse.py \
  --input "10.1038/s41586-021-03569-1" \
  --input-type doi \
  --benchmark-type spatial \
  --output output/literature_results

python skills/literature/literature_parse.py \
  --input paper.pdf \
  --input-type file \
  --benchmark-type multiome \
  --output output/literature_results
```

## Integration

After extraction, the bot can suggest downstream OmicsClaw skills based on:
- data modality (single-cell, spatial, bulk)
- organism and tissue
- available file formats

## Dependencies

- pypdf: PDF text extraction
- requests: HTTP requests
- beautifulsoup4: HTML parsing
