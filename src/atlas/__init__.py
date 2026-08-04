"""Atlas - an evaluation-driven multi-agent platform for technical due diligence.

Atlas answers a question an enterprise actually pays for: *"we're about to
acquire / adopt / depend on this codebase - what's wrong with it?"* A
supervisor dispatches specialist agents (security, architecture, dependency
risk, delivery signals) that analyse a repository in parallel and return
evidence-backed findings, which are merged into one report.

The domain is a vehicle. The point of this repository is the *engineering
around* the agents, which is what separates a demo from a system:

    context engineering   what to keep, what to drop, what to summarise
    memory hierarchy      working / episodic / semantic / procedural, and
                          an A/B harness that MEASURES whether memory helps
    protocols             MCP (agent <-> tools) and A2A (agent <-> agent)
    orchestration         LangGraph state graph, parallel fan-out/fan-in,
                          per-subagent context isolation, checkpointing
    pattern science       ReAct / Plan-Execute / Reflexion / ReWOO
                          implemented AND benchmarked against each other
    evaluation            fixture repos with planted ground truth, so
                          precision/recall are real numbers, plus an
                          LLM-judge with bias controls and 3-tier CI gates

Everything runs offline: the default model is a deterministic scripted
chat model, so `pytest`, the benchmarks and the demo need no API key.

Package layout:
    domain/         findings, evidence, reports - provider-neutral types
    llm/            model protocol + deterministic scripted model
    context/        token budgets and context compaction
    memory/         four memory tiers + measurement harness
    tools/          repository analysis tools
    mcp_layer/      MCP server (expose tools) and client (consume tools)
    a2a/            agent cards and capability discovery
    agents/         supervisor + specialist agents
    patterns/       the four reasoning patterns, pluggable
    graph/          LangGraph orchestration
    evals/          golden datasets, judges, scoring
    observability/  logging, tracing, metrics, cost
    api/            FastAPI surface with streaming
"""

__version__ = "0.1.0"
