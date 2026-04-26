# Final Agreement Prompt — Reverse-Engineer Mode

Review the complete architecture. Your working directory (`cwd`) is already the architecture
root. Use `**/*` (or `**/*.md`) to glob all files. Use paths relative to cwd — do NOT prefix
with `knowledge/architecture/` or any absolute path.

Check:
1. All 7 sprints have produced their required files
2. C1 and C2 diagrams are present and syntactically correct Mermaid
3. All containers from C2 have detailed breakdowns
4. ADRs cover the major architectural decisions
5. The architecture accurately reflects the **actual codebase** — no components invented, no real components omitted

If the architecture is production-ready and all C4 levels are complete and accurate to the code, output exactly:

    READY_TO_SHIP

Otherwise describe specifically what is missing, inaccurate, or needs correction.
Do not output READY_TO_SHIP unless the architecture is genuinely complete and accurate.
