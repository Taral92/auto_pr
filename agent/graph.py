from langgraph.graph import END, START, StateGraph

from .graph_state import ReviewState
from .nodes import (
    after_agent,
    after_forced,
    after_tools,
    agent_step,
    assemble_context,
    degrade,
    execute_tools,
    fail,
    forced_submit,
    ground,
)

RECURSION_LIMIT = 80


def build_graph() -> StateGraph:
    g = StateGraph(ReviewState)
    g.add_node("assemble_context", assemble_context)
    g.add_node("agent_step", agent_step)
    g.add_node("execute_tools", execute_tools)
    g.add_node("forced_submit", forced_submit)
    g.add_node("ground", ground)
    g.add_node("degrade", degrade)
    g.add_node("fail", fail)

    g.add_edge(START, "assemble_context")
    g.add_edge("assemble_context", "agent_step")
    g.add_conditional_edges(
        "agent_step",
        after_agent,
        {
            "fail": "fail",
            "execute_tools": "execute_tools",
            "forced_submit": "forced_submit",
        },
    )
    g.add_conditional_edges(
        "execute_tools",
        after_tools,
        {
            "ground": "ground",
            "forced_submit": "forced_submit",
            "agent_step": "agent_step",
        },
    )
    g.add_conditional_edges(
        "forced_submit",
        after_forced,
        {"degrade": "degrade", "ground": "ground"},
    )
    g.add_edge("degrade", "ground")
    g.add_edge("ground", END)
    g.add_edge("fail", END)
    return g


def mermaid() -> str:
    return build_graph().compile().get_graph().draw_mermaid()


if __name__ == "__main__":
    print(mermaid())
