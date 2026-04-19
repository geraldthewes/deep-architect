# Review Wiki

A Karpathy-style persistent knowledge base built from architecture critique reviews.
Rather than letting critique findings rot in one-off report files, this wiki accumulates patterns over time — so each new review compounds on the last.

## Purpose

- **Pitfalls** grow more complete with each project: repeated antipatterns become clearer, evidence accumulates.
- **Strengths** are remembered so the team can repeat what works.
- **Backlog/Roadmap** tracks ideas that aren't urgent today but shouldn't be forgotten.
- **Non-Issues** document false positives so future reviewers don't re-raise them.
- **Rejected Alternatives** preserve the "why not" rationale so the same ground isn't re-covered.

## How to Maintain

Use `/triage_critique <path-to-critique-report.md>` to process a report interactively.
The `review-wiki` skill handles all file operations: upsert, dedup detection, index and log updates.

To add an individual entry outside of a triage session, describe what you want to add and the `review-wiki` skill will handle it (e.g., "add this pattern to the pitfalls wiki").

## Structure

```
knowledge/review-wiki/
├── README.md               ← this file
├── index.md                ← flat keyword catalog (one line per topic)
├── log.md                  ← append-only triage session log
├── pitfalls/               ← antipatterns and recurring error patterns
├── strengths/              ← what's well-done (auto-captured from ✅ sections)
├── backlog/                ← roadmap ideas and future improvements (pre-ticket)
├── non-issues/             ← reported issues rejected as false positives
└── rejected-alternatives/  ← considered-and-rejected alternatives with rationale
```

## Topic File Format

Each entry (e.g., `pitfalls/uncaught-exception-in-async-worker.md`) follows this schema:

```markdown
---
topic: Uncaught exception handling in async workers
category: pitfall  # pitfall | strength | backlog | non-issue | rejected-alternative
first_seen: YYYY-MM-DD
last_updated: YYYY-MM-DD
occurrences:
  - report: knowledge/research/YYYY-MM-DD-project-critique.md
    severity: critical        # critical | high | medium | low | n/a
    reason: "One-sentence reason this classification was chosen"
    ticket: PROJ-0042         # optional — present only when a ticket was also created
---

# Topic Title

## Pattern
What the antipattern / strength / idea looks like in practice.

## Recommendation
For pitfalls: how to fix/avoid.
For strengths: why it works well; keep doing this.
For backlog: what to implement and why.
For non-issues: why this is not a problem in our context.
For rejected-alternatives: why this was considered and rejected.

## References
- External: [software-backend-wiki](https://github.com/geraldthewes/software-backend-wiki)
- OWASP Top 10: https://owasp.org/www-project-top-ten/ (for security pitfalls)
- Related tickets: PROJ-XXXX

## Occurrences
- **YYYY-MM-DD** — `knowledge/research/YYYY-MM-DD-project-critique.md` (critical)
  Reason: One-sentence reason this classification was chosen.
```

## Re-Triage

Running `/triage_critique` on the same report a second time, or on a newer report that has an overlapping pattern, appends a new entry under `occurrences:` in frontmatter and a new bullet under `## Occurrences` in the body. It does NOT create a duplicate file.

## External References

- Karpathy LLM Wiki pattern: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- Software Backend Wiki (backend code patterns): https://github.com/geraldthewes/software-backend-wiki
- OWASP Top 10 (security patterns): https://owasp.org/www-project-top-ten/
- Beads (optional distributed issue tracker): https://github.com/gastownhall/beads
  → Beads is an alternative ticket backend if you want dependency-graph tracking and agent-optimized workflows. To migrate existing markdown tickets to beads, run `bd init` and `bd import`.
