from __future__ import annotations

import sys

from deep_architect.review_action_harness import (
    AgentConfig,
    ClaudeSDKAgent,
    CodingAgent,
    OpencodeAgent,
    ReviewFinding,
    ValidationConfig,
    create_agent,
    is_valid_finding,
    main,
    parse_markdown_finding,
    process_findings,
)

__all__ = [
    "AgentConfig",
    "ClaudeSDKAgent",
    "CodingAgent",
    "OpencodeAgent",
    "ReviewFinding",
    "ValidationConfig",
    "create_agent",
    "is_valid_finding",
    "main",
    "parse_markdown_finding",
    "process_findings",
]


if __name__ == "__main__":
    sys.exit(main())
