"""The create agent's wiring. The logic is in `nodes`.

```
START → ask_name (interrupt: the site's name, unless it has one) → setup (the repo with the starter files)
          ▲                                                           │ name taken (a few times)
          └───────────────────────────────────────────────────────────┘
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

from app.agents.create_site.nodes import ask_name, commit, finish, fix, plan, setup, verify, write
from app.agents.create_site.state import CreateRun, CreateState

type CreateGraph = CompiledStateGraph[CreateState, CreateRun, CreateState, CreateState]


def _step(state: CreateState) -> str:
    return state["step"]


def _build() -> StateGraph[CreateState, CreateRun, CreateState, CreateState]:
    graph = StateGraph(CreateState, context_schema=CreateRun)
    for name, node in (
        ("ask_name", ask_name),
        ("setup", setup),
        ("plan", plan),
        ("write", write),
        ("verify", verify),
        ("fix", fix),
        ("commit", commit),
        ("finish", finish),
    ):
        graph.add_node(name, node)
    graph.add_edge(START, "ask_name")
    graph.add_conditional_edges("ask_name", _step, ["setup"])
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
