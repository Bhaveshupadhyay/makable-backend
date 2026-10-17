"""The create agent's wiring. The logic is in `nodes`.

```
START → brief (the model: what to ask about the site, ≤ 3) ◄─┐
          ├─ a question → ask_brief (interrupt) ─────────────┘
          │ ready (names to suggest)
        ask_name (interrupt: the site's name, unless it has one) ◄──┐
          │                                                         │ not a name: answered, asked again
        read_name (the user's answer: a clicked option, or the model) ─┤
          │ a name                                                  │
        setup (the repo with the starter files) ────────────────────┘ name taken (a few times)
          │
        plan (the files, and what each is for)
          │
        write (one file per step) ◄─┐
          ├─ more files ────────────┘
        verify (imports, exports, packages, icons, syntax) ⇄ fix (≤ 2 rounds)
          │
        commit (all files, one commit; finished even if cancelled) → finish
```
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.create_site.nodes import (
    ask_brief,
    ask_name,
    brief,
    commit,
    finish,
    fix,
    plan,
    read_name,
    setup,
    verify,
    write,
)
from app.agents.create_site.state import CreateRun, CreateState

type CreateGraph = CompiledStateGraph[CreateState, CreateRun, CreateState, CreateState]


def _step(state: CreateState) -> str:
    return state["step"]


def _build() -> StateGraph[CreateState, CreateRun, CreateState, CreateState]:
    graph = StateGraph(CreateState, context_schema=CreateRun)
    for name, node in (
        ("brief", brief),
        ("ask_brief", ask_brief),
        ("ask_name", ask_name),
        ("read_name", read_name),
        ("setup", setup),
        ("plan", plan),
        ("write", write),
        ("verify", verify),
        ("fix", fix),
        ("commit", commit),
        ("finish", finish),
    ):
        graph.add_node(name, node)
    graph.add_edge(START, "brief")
    graph.add_conditional_edges("brief", _step, ["ask_brief", "ask_name"])
    graph.add_conditional_edges("ask_brief", _step, ["brief", "ask_name"])
    graph.add_conditional_edges("ask_name", _step, ["read_name", "setup"])
    graph.add_conditional_edges("read_name", _step, ["ask_name", "setup"])
    graph.add_conditional_edges("setup", _step, ["ask_name", "plan"])
    graph.add_edge("plan", "write")
    graph.add_conditional_edges("write", _step, ["write", "verify"])
    graph.add_conditional_edges("verify", _step, ["fix", "commit"])
    graph.add_edge("fix", "verify")
    graph.add_edge("commit", "finish")
    graph.add_edge("finish", END)
    return graph


_GRAPH = _build()


def compile_create_graph(checkpointer: BaseCheckpointSaver[str]) -> CreateGraph:
    return _GRAPH.compile(checkpointer=checkpointer)
