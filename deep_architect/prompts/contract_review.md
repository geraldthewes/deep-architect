# Contract Review Prompt
<!-- Bootstrap: adversarial-dev CONTRACT_NEGOTIATION_EVALUATOR_PROMPT -->

Review this proposed sprint contract. Your job is to tighten it adversarially.

- Make vague criteria specific and measurable
- Add adversarial edge cases the generator might miss
- Raise thresholds where quality is critical
- Reject criteria that cannot be objectively tested

Return a JSON object with exactly two fields:
- `"approved"`: `true` if the contract is already sufficiently rigorous (all criteria
  specific and testable, thresholds appropriate, edge cases covered), `false` otherwise
- `"revised_contract"`: `null` if approved, or the full revised contract object (same
  structure as the proposal) if not approved

## Proposed Contract

{contract_json}
