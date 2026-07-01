"""Agent Curator (Stage 1) — LLM-driven data format detection & conversion.

Three-layer pipeline:
  1. curator.py        → AgentCurator.curate()        — LLM produces curation_plan.json
  2. executor.py       → CurationExecutor.run()        — executes steps → curated.h5ad
  3. validator.py      → validate_curated_h5ad()       — post-execution quality checks

Architecture:
    curator_runner.py  →  run_agent_curator()  — iterate all papers
    curator.py         →  AgentCurator          — LLM format detection per paper
    executor.py        →  CurationExecutor      — deterministic step execution
    validator.py       →  validate_curated_h5ad — anti-hallucination checks
"""
