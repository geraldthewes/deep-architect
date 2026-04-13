# Contract Proposal Prompt
<!-- Bootstrap: adversarial-dev CONTRACT_NEGOTIATION_GENERATOR_PROMPT + PRD §5.3 -->

You are proposing a sprint contract for the following sprint.

## PRD
{prd}

## Sprint Definition
Sprint {sprint_number}: {sprint_name}
{sprint_description}

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
- Cover: Mermaid diagram validity, C4 completeness, narrative quality, PRD accuracy, relationship documentation, Markdown readability (heading hierarchy, whitespace, list formatting, code block labels)
- Add criteria for edge cases relevant to this sprint
- Threshold must be ≥ 9.0 for critical quality requirements

Output ONLY the JSON — no explanation, no markdown fencing.
