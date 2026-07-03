from __future__ import annotations

import sys

from deep_architect.review_action_harness import (
    AgentConfig,
    ClaudeSDKAgent,
    CodingAgent,
    FindingStatus,
    OpencodeAgent,
    ReviewFinding,
    ValidationConfig,
    create_agent,
    has_action_taken,
    is_valid_finding,
    main,
    parse_markdown_finding,
    process_findings,
    read_action_taken,
    write_action_taken,
)

__all__ = [
    "AgentConfig",
    "ClaudeSDKAgent",
    "CodingAgent",
    "FindingStatus",
    "OpencodeAgent",
    "ReviewFinding",
    "ValidationConfig",
    "create_agent",
    "has_action_taken",
    "is_valid_finding",
    "main",
    "parse_markdown_finding",
    "process_findings",
    "read_action_taken",
    "write_action_taken",
]


if __name__ == "__main__":
    sys.exit(main())
