from __future__ import annotations

import sys

from deep_architect.coding_agents import (
    ClaudeSDKAgent,
    CodingAgent,
    CodingAgentConfig,
    OpencodeAgent,
    create_agent,
)
from deep_architect.review_action_harness import (
    FindingStatus,
    ReviewFinding,
    has_action_taken,
    is_valid_finding,
    main,
    parse_markdown_finding,
    process_findings,
    read_action_taken,
    write_action_taken,
)

__all__ = [
    "ClaudeSDKAgent",
    "CodingAgent",
    "CodingAgentConfig",
    "FindingStatus",
    "OpencodeAgent",
    "ReviewFinding",
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
