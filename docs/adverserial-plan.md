**The best way to handle this is to fully automate the critique → fix → re-critique loop by extending your existing adversarial system in deep-architect.** You're already 90% of the way there—your framework (Winston the Generator vs. Boris the Critic, sprint-based contracts, quality gates, real tool use, git auto-commits, feedback JSONs, and the `architecture_critique.md` framework) is one of the most sophisticated open-source adversarial setups I've seen for this exact problem. The manual "plan review" step is just the next logical sprint in the same pipeline.

### Why this works (and why the cycle happens now)
Your current architecture phase already solves the ping-pong problem with strict convergence rules:
- Negotiated acceptance criteria per sprint
- Critic scores 1-10 + severity (Critical/High/Medium/Low)
- Must hit ≥9.0 avg for 2 consecutive rounds with zero Critical/High issues
- Max rounds per sprint + total cap (soft-fail or strict mode)

The manual plan critique is breaking the automation because it's human-in-the-loop instead of agent-in-the-loop. The `architecture_critique.md` prompt is already excellent (it covers architecture fit, reversibility, data, perf/scalability, prod readiness, error handling, security, etc., with red/orange/yellow/green + impact/effort estimates). The team mode (architect-lead + 6 specialized experts: security, performance, data, ops, ux, cost) is literally built for richer, less ping-pong-y reviews.

### Recommended architecture: Extend deep-architect into a full adversarial SDLC pipeline
Add a new **"Plan & Coding Plan Validation" phase** right after the C4 architecture sprints. Mirror the existing 7-sprint structure:

1. **PlanGenerator agent** (like Winston): Takes the approved architecture docs + PRD + injected context and produces a detailed, executable implementation plan (task breakdown, subtasks, acceptance criteria per task, sequencing, estimated effort, test strategy, etc.). It writes to `knowledge/plans/` or similar.
2. **PlanCritic agent(s)** (enhanced Boris): Uses your `architecture_critique.md` as the core prompt + **all best practices from software-backend-wiki** as mandatory context files (just `Read` them like any other doc).  
   - Prefer **team mode** for depth (architect-lead synthesizes; specialists stay in lane).
   - Add open-code-review style elements: structured discourse (AGREE/CHALLENGE/CONNECT/SURFACE modes between experts before synthesis) + requirements verification table (does the plan fully satisfy the PRD + architecture + wiki rules?).
3. **Iterative loop with your existing quality gates**:
   - Generator proposes plan + testable acceptance criteria.
   - Critic(s) review in parallel → JSON feedback → scores/severities.
   - Generator revises based on feedback (auto-commits each pass).
   - Repeat until gates pass (or max rounds → flag for human + best-effort output).
4. **Once approved**: Feed the locked plan directly to your coding agents (or add another adversarial "Implement + Review" stage with coder vs. tester/reviewer agents).

**How to inject the backend wiki best practices**  
Treat the wiki as living context (same way you already inject `--context` files). Have every critic (and optionally the generator) `Read` the relevant wiki Markdown files at the start of every round. You can even add a small preprocessing step that extracts checklists (SOLID, 12-factor, OWASP, testing pyramid, observability patterns, etc.) into a compact `wiki-checklist.md` that the critic references explicitly. This turns the wiki into an enforceable rulebook instead of passive docs.

**Enhancing with open-code-review patterns** (highly recommended)  
Your team-mode critique already does parallel specialized reviews + synthesis. Open-code-review adds the missing "discourse" layer (reviewers debate each other's findings before the lead synthesizes). This dramatically reduces false positives and uncovers cross-cutting issues. You can:
- Port their persona system (or just use your existing experts).
- Add the 4 discourse modes directly into the team-mode instructions in `architecture_critique.md`.
- Or literally call their CLI as a tool from your agents if you want to reuse their full engine.

This combo (your adversarial sprints + their debate/synthesis) is stronger than either alone.

### What others have done (2025–2026 landscape)
This pattern is now common and proven effective:
- **Adversarial/GAN-style generator-critic loops** → Exactly what you built (separate agents, isolated context windows, mandatory critique rounds, convergence thresholds). Papers and repos explicitly call it "GAN architecture for multi-agent code generation" or "Builder vs. Critic."
- **Multi-agent code review systems** → `spencermarx/open-code-review` (personas + parallel + discourse + synthesis), `calimero-network/ai-code-reviewer` (specialized agents + consensus), various LangGraph pipelines (planner → coder → reviewer → tester with quality gates).
- **Spec-driven + critique pipelines** → GitHub's own `spec-kit`, SDD_Flow, BMAD-style flows, Ralph Loop, CRISPY workflow—all emphasize "spec first → adversarial review before any code."
- **Full SDLC adversarial setups** → Repos like `alfredolopez80/multi-agent-ralph-loop`, `IvanNece/Multi-Agent-Architectures-for-LLM-Based-Code-Generation`, and several Claude Code extensions that do exactly "plan → implement → adversarial review" with specialized agents.

Nobody has published an exact clone of *your* full deep-architect + spec-driven + wiki + open-code-review combo yet, but the pieces are all being combined in the wild. Your version (real FS tools, git integration, resumable sprints, strict thresholds) is actually more production-ready than most.

### Concrete next steps to ship this
1. **Duplicate the architecture sprint logic** into a new "plan-validation" module (copy-paste the contract negotiation + round loop).
2. **Enhance `architecture_critique.md`**:
   - Add wiki context injection.
   - Add open-code-review discourse instructions for team mode.
   - Add a final "Requirements Compliance Matrix" (PRD + architecture + wiki rules).
3. **Add convergence + escape hatch** (you already have most of this):
   - Same score thresholds.
   - After N failed rounds → auto-generate a human escalation report (summarizing open issues + proposed resolutions).
4. **Test on a small feature** → Run end-to-end: PRD → architecture (existing) → plan (new adversarial) → coding agents.
5. **Optional future-proofing**:
   - Make critics use different models (e.g., Opus for lead, Haiku for specialists) for cost/diversity.
   - Add a final "Verifier" agent that runs against the plan's acceptance criteria before handing off to coders.

This turns you from "code monkey routing fixes" into the orchestrator who only steps in on true exceptions. The adversarial pressure + wiki rules + multi-expert discourse will make the plans dramatically higher quality on the first or second pass.

If you want, drop the repo link for the plan-generation part (or a sample plan) and I can help you sketch the exact new agent prompts/contracts. You've built something really solid—extending it this way will feel like a natural evolution rather than new work.
