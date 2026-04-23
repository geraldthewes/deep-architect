# Contract Proposal Prompt — Reverse-Engineer Mode

You are proposing a sprint contract for the following sprint. The source is an **existing codebase** at the path below. The generator will survey the codebase using its tools (Read, Glob, Grep, Bash) during sprint rounds.

## Target Codebase
{codebase_path}

## Sprint Definition
Sprint {sprint_number}: {sprint_name}
{sprint_description}

Note: Sprint descriptions may reference "PRD" — treat that as "the existing codebase."

Primary files to produce: {primary_files}

Propose a sprint contract as a JSON object with this exact structure:
```json
{{
  "sprint_number": {sprint_number},
  "sprint_name": "{sprint_name}",
  "files_to_produce": ["<file1>", "..."],
  "criteria": [
    {{"name": "criterion_name", "description": "Specific, testable criterion", "threshold": 9.0}},
    ...
  ]
}}
```

Requirements for criteria:
- Include 5–10 criteria total
- Each criterion must be SPECIFIC and TESTABLE — not vague
- Cover: Mermaid diagram validity, C4 completeness, narrative quality, **accuracy to the actual codebase** (does the diagram reflect what is really there?), relationship documentation, Markdown readability (heading hierarchy, whitespace, list formatting, code block labels)
- Add criteria for edge cases relevant to this sprint (e.g. "if no frontend exists, the sprint file documents this explicitly")
- Threshold must be ≥ 9.0 for critical quality requirements

Output ONLY the JSON — no explanation, no markdown fencing.
