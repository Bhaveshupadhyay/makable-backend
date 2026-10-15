"""The AI edit agent's wiring. The logic is in `nodes`.

```
START → classify ──Tier 2 rule──────────────────────────────► finish
          │
        generate ◄──rejected, attempts left (repair prompt)── check
          └──────────────────────────────────────────────────► check ──ok / escalated / out of attempts──► finish
```
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.ai_edit.nodes import check, classify, finish, generate
from app.agents.ai_edit.state import EditRun, EditState

type EditGraph = CompiledStateGraph[EditState, EditRun, EditState, EditState]


def _finished_or_generate(state: EditState) -> str:
    """After `classify` and `check`: finished once there's a result, else (another) model call."""
    return "finish" if state.get("result") else "generate"


def _build() -> StateGraph[EditState, EditRun, EditState, EditState]:
    graph = StateGraph(EditState, context_schema=EditRun)
    graph.add_node("classify", classify)
    graph.add_node("generate", generate)
    graph.add_node("check", check)
    graph.add_node("finish", finish)
    graph.add_edge(START, "classify")
    graph.add_conditional_edges("classify", _finished_or_generate, ["generate", "finish"])
    graph.add_edge("generate", "check")
    graph.add_conditional_edges("check", _finished_or_generate, ["generate", "finish"])
    graph.add_edge("finish", END)
    return graph


_GRAPH = _build()


def compile_edit_graph(checkpointer: BaseCheckpointSaver[str]) -> EditGraph:
    """The graph, saving to `checkpointer`. Compiling takes under a millisecond."""
    return _GRAPH.compile(checkpointer=checkpointer)
