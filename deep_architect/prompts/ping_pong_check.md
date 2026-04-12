# Ping-Pong Detection Prompt
<!-- Bootstrap: PRD §5.4 ping-pong criteria -->

Compare these two rounds of critic feedback and estimate their semantic similarity.
A score of 0.0 means the issues are completely different; 1.0 means they are identical.

High similarity (> 0.85) combined with minimal score improvement indicates the generator
and critic are stuck in a loop with no meaningful progress.

## Previous Round Feedback
{previous_summary}

## Current Round Feedback
{current_summary}

Output a JSON object only:
```json
{{"similarity_score": <float 0.0-1.0>, "reasoning": "<one sentence explanation>"}}
```
