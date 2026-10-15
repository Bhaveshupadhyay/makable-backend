from langgraph.runtime import Runtime

from app.agents.ai_edit.state import EditRun, EditState
from app.agents.ai_edit.tools.rules import tier_2_rule
from app.constants.ai_edit import AiEditOutcome


async def classify(state: EditState, runtime: Runtime[EditRun]) -> EditState:
    """Sends a request straight to Tier 2 when a rule says so, without a model call."""
    rule = tier_2_rule(runtime.context.request)
    if rule is None:
        return {}
    return {"result": {"outcome": AiEditOutcome.ESCALATED.value, "tier": 2, "reason": rule}}
