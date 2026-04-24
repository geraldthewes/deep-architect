# Architecture Decision Records — deep-architect

This directory contains ADRs capturing the key architectural decisions made during implementation of the adversarial C4 architecture harness.

## Index

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-001](ADR-001-claude-agent-sdk-no-framework.md) | Use Claude Agent SDK with No Agent Framework | Accepted |
| [ADR-002](ADR-002-system-cli-binary-env-vars.md) | Resolve Claude CLI from System PATH; Auth/Endpoint via Environment Variables | Accepted |
| [ADR-003](ADR-003-asymmetric-tool-access.md) | Asymmetric Tool Access — Generator Writes, Critic Reads Only | Accepted |
| [ADR-004](ADR-004-generator-session-persistence.md) | Generator Session Persistence Within Sprint, Reset on Failure | Superseded by ADR-021 |
| [ADR-005](ADR-005-dual-llm-call-patterns.md) | Two LLM Call Patterns — Pydantic-AI for Contracts, SDK for File Operations | Accepted |
| [ADR-006](ADR-006-exit-criteria.md) | Multi-Layer Exit Criteria — Score + Severity + Consecutive Rounds + Ping-Pong Detection | Accepted |
| [ADR-007](ADR-007-seven-fixed-sprints.md) | Seven Fixed Sprints with Pre-Defined C4 Progression | Accepted |
| [ADR-008](ADR-008-prompts-as-markdown-files.md) | Prompts Stored as .md Files, Loaded at Runtime | Accepted |
| [ADR-009](ADR-009-config-split-toml-vs-env.md) | TOML Config for Behavior; Environment Variables for Auth and Endpoints | Accepted |
| [ADR-010](ADR-010-git-detection-auto-commit.md) | Auto-Commit After Each Generator Pass via Git Status Detection | Accepted |
| [ADR-011](ADR-011-resume-via-progress-json.md) | Sprint-Level Resume via progress.json | Accepted |
| [ADR-012](ADR-012-structured-output-json-schema.md) | Structured Critic Output via JSON Schema (output_format) | Accepted |
| [ADR-013](ADR-013-disallowed-tools-hallucination-guard.md) | Disallowed Tools List as Hallucination Guard | Accepted |
| [ADR-014](ADR-014-extended-thinking-disabled.md) | Extended Thinking Disabled for All Agent Calls | Accepted |
| [ADR-015](ADR-015-dual-retry-layers.md) | Two Independent Retry Layers — Agent Retry and Round Retry | Accepted |
| [ADR-016](ADR-016-preflight-check.md) | Preflight Check Before Starting the Main Loop | Accepted |
| [ADR-017](ADR-017-final-mutual-agreement.md) | Final Mutual Agreement Round After All Sprints | Accepted |
| [ADR-018](ADR-018-run-stats-context-variable.md) | Token/Cost Tracking via RunStats and Context Variables | Accepted |
| [ADR-019](ADR-019-severity-blocking.md) | Critical/High Severity Issues Block Sprint Completion Regardless of Score | Accepted |
| [ADR-020](ADR-020-allow-extra-files-flag.md) | Permissive Extra Files via allow_extra_files Sprint Flag | Accepted |
| [ADR-021](ADR-021-stateless-session-per-turn.md) | Stateless Session Per Turn — Generator Session Reset Every Round | Accepted |
| [ADR-022](ADR-022-per-agent-timeout-sigterm.md) | Per-Agent Timeout and Graceful SIGTERM Handling | Accepted |
| [ADR-023](ADR-023-keep-best-rollback.md) | Keep-Best Hill Climbing with Rollback on Score Regression | Accepted |
| [ADR-024](ADR-024-soft-fail-sprint.md) | Soft-Fail Sprint Completion — Accept Best-Effort Result Instead of Halting | Accepted |
| [ADR-025](ADR-025-critic-rescue-fallback.md) | Critic Rescue Fallback for Empty Structured Output | Accepted |
| [ADR-026](ADR-026-circuit-breaker-pattern.md) | Circuit Breaker Pattern for Model Communication Failures | Accepted |
| [ADR-027](ADR-027-yolo-sprint-checkpoint-mode.md) | Sprint-by-Sprint Checkpoint Mode (`--yolo` Escape Hatch) | Accepted |

## Key Themes

**No framework** — ADR-001: The harness is plain async Python using the Claude agent SDK directly. No LangGraph, LangChain, or CrewAI.

**Security by configuration** — ADR-002, ADR-009: Secrets stay in environment variables; behavior config stays in TOML. The disallowed tools list (ADR-013) prevents model hallucinations from escaping the sandbox.

**Adversarial quality gates** — ADR-003, ADR-006, ADR-019: Strict separation of generator/critic roles enforced by asymmetric tool access. Exit criteria combine numeric scoring, hard severity blocks, and ping-pong detection.

**Resilience** — ADR-022, ADR-023, ADR-024, ADR-021, ADR-011, ADR-015, ADR-016: Per-agent timeout with automatic retry, SIGTERM graceful shutdown, keep-best rollback on score regression, soft-fail sprint acceptance, two retry layers, stateless-session per turn, sprint-level resume, and preflight validation make the harness robust to transient failures including laptop hibernation.

**Cost optimization** — ADR-005, ADR-014, ADR-018: Cheaper pydantic-ai calls for no-tool operations; extended thinking disabled; full cost accounting via RunStats.
