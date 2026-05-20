---
name: reproduce-paper
description: >-
  Automate the reproducibility workflow for a research paper. Parses the paper,
  locates code repositories, prepares environment artifacts, and executes a first-
  pass reproduction test.
version: 0.1.0
author: OmicsClaw
license: MIT
tags: [reproducibility, benchmark, environment, automation, paper]
metadata:
  omicsclaw:
    domain: orchestrator
    requires:
      bins:
        - python3
        - git
      env: []
      config: []
    emoji: "🔁"
    homepage: https://github.com/OmicsClaw/OmicsClaw
    os: [macos, linux, windows]
    install:
      - kind: pip
        package: requests
        bins: []
    trigger_keywords:
      - reproduce paper
      - reproduce benchmark
      - reproducibility
      - environment automation
      - clone repository
      - run tests
---

# Paper Reproducibility Skill

## Purpose

Automate the first-pass reproduction workflow for scientific papers. This skill
bridges literature extraction with code repository discovery, environment
preparation, and basic execution checks.

## Methodology

1. **Parse the paper**
   - Accepts DOI, PubMed ID, URL, PDF, or raw text input
   - Extracts dataset accessions, organism/tissue/technology metadata
2. **Locate repository**
   - Detects GitHub/GitLab/Bitbucket repository URLs from text
   - Accepts an explicit `--repo-url` override
3. **Prepare environment**
   - Detects `requirements.txt`, `environment.yml`, `pyproject.toml`, or `setup.py`
   - Generates reproducibility artifacts in `reproducibility/`
   - Writes `commands.sh`, environment files, and logs
4. **Execute tests**
   - Runs `pytest` if tests are available
   - Otherwise attempts `python run.py` or `python main.py`
   - Captures execution logs and status

## Output

- `reproducibility/plan.json`
- `reproducibility/result.json`
- `reproducibility/commands.sh`
- `reproducibility/report.md`
- `reproducibility/logs/`
- `reproducibility/cloned_repos/`
- `reproducibility/environments/`

## Usage

```bash
python skills/orchestrator/reproduce_paper/reproduce_paper.py \
  --input "https://pubmed.ncbi.nlm.nih.gov/12345" \
  --repo-url "https://github.com/owner/repo" \
  --output reproducibility_output
```

```bash
python skills/orchestrator/reproduce_paper/reproduce_paper.py \
  --input "This paper describes an integration benchmark with GEO and SRA datasets." \
  --benchmark-type integration \
  --output reproducibility_output
```

```bash
python skills/orchestrator/reproduce_paper/reproduce_paper.py \
  --input paper.pdf \
  --input-type file \
  --output reproducibility_output \
  --no-clone
```
