"""Shared building blocks for the DevOps Agent fleet.

Modules
-------
protocol   Pydantic models exchanged between local agents and the server agent.
manifest   ``devops-agent.yaml`` component manifest spec, loader and inference.
gitguard   Capability-restricted git wrappers (read-only, workspace, merge-only).
analysis   Deterministic change analysis: what configs/deps/migrations a branch changes.
compose    Docker Compose rendering for test environments and deployments.
llm        OpenAI-compatible chat client (vLLM on the server, Ollama/llama.cpp locally).
agentloop  Minimal tool-calling loop shared by both agents.
"""

__version__ = "0.1.0"
