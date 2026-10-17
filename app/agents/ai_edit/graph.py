"""The AI edit agent's wiring. The logic is in `nodes`.

```
START → classify ──Tier 2 rule──────────────────────────────────────────────────────────► finish
          │
        load_context (the site at the base commit; the selection; the first code shown)
          │
        generate ◄──────────────────────────────────────────────┐
          │                                                     │
        check ──needContext (≤ 2)──► read_more ─────────────────┤
          │   ──ask (≤ 2)──► ask_user (interrupt: paused, saved) ──► load_context (the site as it is now)
          │   ──rejected, attempts left (repair report)─────────┘
          │   ──escalated / rejected, out of attempts──────────────────────────────────► finish
          │ ok
        preview (interrupt: the SPA tries the edits) ──failed (≤ 2): what the preview showed──► generate
          │                                          ──failed again────────────────────────────────────► finish
          │ works
        commit (the edits, as one commit on the base commit; finished even if cancelled) ──► finish
```
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.ai_edit.nodes import (
    ask_user,
    check,
    classify,
    commit,
    finish,
    generate,
    load_context,
    preview,
    read_more,
)
from app.agents.ai_edit.state import EditRun, EditState

type EditGraph = CompiledStateGraph[EditState, EditRun, EditState, EditState]


def _after_classify(state: EditState) -> str:
    return "finish" if state.get("result") else "load_context"


def _step(state: EditState) -> str:
    """After `check` and `preview`: where they said the run goes next."""
    return state["step"]


def _build() -> StateGraph[EditState, EditRun, EditState, EditState]:
    graph = StateGraph(EditState, context_schema=EditRun)
    graph.add_node("classify", classify)
    graph.add_node("load_context", load_context)
    graph.add_node("generate", generate)
    graph.add_node("check", check)
    graph.add_node("read_more", read_more)
    graph.add_node("ask_user", ask_user)
    graph.add_node("preview", preview)
    graph.add_node("commit", commit)
    graph.add_node("finish", finish)
    graph.add_edge(START, "classify")
    graph.add_conditional_edges("classify", _after_classify, ["load_context", "finish"])
    graph.add_edge("load_context", "generate")
    graph.add_edge("generate", "check")
    graph.add_conditional_edges("check", _step, ["read_more", "ask_user", "generate", "preview", "finish"])
    graph.add_conditional_edges("preview", _step, ["commit", "generate", "finish"])
    graph.add_edge("read_more", "generate")
    graph.add_edge("ask_user", "load_context")
    graph.add_edge("commit", "finish")
    graph.add_edge("finish", END)
    return graph


_GRAPH = _build()


def compile_edit_graph(checkpointer: BaseCheckpointSaver[str]) -> EditGraph:
    """The graph, saving to `checkpointer`. Compiling takes under a millisecond."""
    return _GRAPH.compile(checkpointer=checkpointer)
