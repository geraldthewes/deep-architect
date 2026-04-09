# Contract Review Prompt
<!-- Bootstrap: adversarial-dev CONTRACT_NEGOTIATION_EVALUATOR_PROMPT -->

Review this proposed sprint contract. Your job is to tighten it adversarially.

- Make vague criteria specific and measurable
- Add adversarial edge cases the generator might miss
- Raise thresholds where quality is critical
- Reject criteria that cannot be objectively tested

If the contract is already sufficiently rigorous (all criteria specific and testable,
thresholds appropriate, edge cases covered), output exactly:

    APPROVED

Otherwise output a revised JSON contract with the same structure as the proposal.
Output ONLY "APPROVED" or the revised JSON — nothing else, no explanation.

## Proposed Contract
{contract_json}
