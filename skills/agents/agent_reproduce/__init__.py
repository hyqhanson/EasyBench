"""AgentReproduce (Stage 2) — Monitor-Fix loop for paper reproduction.

Architecture:
    runner.py  →  run_agent_reproduce()  — per-paper orchestration
    monitor.py →  analyze_log()          — real-time error detection
    fix.py     →  AgentFix               — diagnosis + repair (skill lib + LLM)
"""
