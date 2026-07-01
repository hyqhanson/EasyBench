"""Agent Preflight (Stage 0.5) — LLM-driven protocol-code-data matching.

AgentScanner reads experimental_protocol.json, scans data/ and benchmark_code/
directories, and calls the LLM to produce execution_plan.json.

Architecture:
    runner.py  →  run_agent_preflight()  — iterate all papers
    scanner.py →  AgentScanner           — single-paper analysis
"""
