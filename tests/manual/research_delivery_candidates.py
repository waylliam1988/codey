"""Experimental delivery checks for 4A/4B; never imported by production."""

DELIVERY_PROMPT = (
    "Before submitting, check the original task for every explicitly requested deliverable. "
    "If the user requested retaining new structured knowledge, include the corresponding supported "
    "knowledge_updates; describing a candidate in prose does not save it. Do not add knowledge_updates "
    "when the task does not need them."
)


def four_b_instruction(arm: str) -> str:
    prompts = {
        "U": "Submit the frozen original candidate under the existing contract.",
        "R": "Reread the original task, original draft and exact read evidence, then revise once before submitting.",
        "S": "Check the original task and exact evidence for omissions, then revise once before submitting.",
        "V": "Review the original task and exact evidence for support and omissions, provide bounded feedback, then revise once before submitting.",
    }
    if arm not in prompts:
        raise ValueError("unknown 4B arm")
    return prompts[arm]
