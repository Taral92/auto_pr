# auto-pr on LangGraph

## The one decision that matters

LangGraph orchestrates the **review pipeline**. It does NOT replace the
**model loop**.

- WRONG: `create_react_agent(...)` — hands the tool-calling loop to the
  framework. Your budget governor, grounding gate and iteration accounting all
  get replaced by framework equivalents you did not design.
- RIGHT: LangGraph is the state machine over the 17 pipeline steps. The agent
  loop is ONE node, still calling the `anthropic` SDK directly.

## No LangChain

```
langgraph
langgraph-checkpoint-sqlite
anthropic          # unchanged, still the model client
pydantic
python-dotenv
```

Do NOT install `langchain`, `langchain-core` as a direct dep, or
`langchain-anthropic`. No `ChatAnthropic`, no `AgentExecutor`, no LCEL.
(`langchain-core` will arrive transitively — that is fine, do not import it.)

---

## State

Single `TypedDict`, one run per graph execution.

```python
class ReviewState(TypedDict, total=False):
    # input
    run_id: str
    owner: str
    repo: str
    number: int
    dry_run: bool

    # fetched
    head_sha: str
    diff: str
    diff_bytes: int

    # workspace
    workspace: str            # temp dir path

    # context
    system_prompt: str
    prompt_sha: str

    # agent loop
    messages: list[dict]
    corpus: list[dict]        # {source, text} — diff + every tool_result
    iterations: int
    raw_output: str

    # post-processing
    findings: list[dict]
    grounding: dict           # {grounded, near, ungrounded}
    anchoring: dict           # {inline, summary, dropped}
    payload: dict             # the GitHub review body

    # budgets
    tokens_in: int
    tokens_out: int
    started_at: float
    budget_breach: str | None # "iterations" | "tokens" | "wall_clock" | None

    # outcome
    status: str               # running|degraded|published|too_large|failed
    error: str | None
```

`corpus` is the grounding gate's input. Append to it in BOTH `fetch_pr`
(the diff) and `execute_tools` (every result). This is the whole reason the
gate works — do not let it drift.

---

## Nodes

| Node | Does | Writes |
|---|---|---|
| `fetch_pr` | GET pull + diff | `head_sha`, `diff`, `diff_bytes`, seeds `corpus` |
| `materialize` | clone head into temp dir | `workspace` |
| `assemble_context` | system prompt + tool schemas + diff | `system_prompt`, `prompt_sha`, `messages` |
| `agent_step` | ONE model call | appends to `messages`, `tokens_*`, `iterations` |
| `execute_tools` | run requested tools | tool_results into `messages` + `corpus` |
| `parse_findings` | strict schema, one repair | `findings` |
| `ground` | evidence vs `corpus` | verdicts, `grounding` |
| `anchor` | hunk parse -> inline vs summary | `anchoring`, `payload` |
| `publish` | POST review (skip if `dry_run`) | `status` |
| `too_large` | body-only "diff too large" | `status="too_large"` |
| `degrade` | annotate partial reason | `status="degraded"` |
| `fail` | record error | `status="failed"`, `error` |

## Edges

```
START -> fetch_pr

fetch_pr -> [conditional]
    diff_bytes > CAP  -> too_large -> publish -> END
    else              -> materialize

materialize -> assemble_context -> agent_step

agent_step -> [conditional]
    stop_reason == tool_use  -> execute_tools
    else                     -> parse_findings

execute_tools -> [conditional]
    budget_breach is not None -> degrade -> parse_findings
    else                      -> agent_step        # <-- the loop

degrade -> parse_findings
parse_findings -> ground -> anchor -> publish -> END
```

The `agent_step -> execute_tools -> agent_step` cycle IS your existing loop.
Same shape as `agent/loop.py`, just expressed as edges.

---

## Five things LangGraph will NOT do for you

1. **`finally` semantics.** A crash in a node does not unwind. Your temp dir
   stays on disk. Fix: wrap `graph.invoke()` in a real `try/finally` in
   `review_pr()` and delete the workspace there. Do NOT make cleanup a node
   and assume it runs.
2. **Your iteration cap.** `recursion_limit` counts graph super-steps, not
   model calls. They are not the same number. Keep `state["iterations"]` and
   check it yourself. Set `recursion_limit` generously as a backstop only.
3. **The grounding gate.** Your logic. A node.
4. **Anchoring.** Your hunk parser. A node.
5. **A queue.** LangGraph is not a job queue. The `jobs` table and worker from
   `docs/v0-e2e.md` still exist and are unchanged.

---

## Checkpointing — draw the boundary

Two stores, two jobs. Do not merge them.

| Store | Owns |
|---|---|
| `SqliteSaver` (LangGraph) | in-flight graph state, per-node resume |
| `jobs` / `runs` / `findings` tables (yours) | queueing, history, the UI's data |

- `thread_id` = `run_id`
- On worker restart, a job in `running` can be resumed:
  `graph.invoke(None, config={"configurable": {"thread_id": run_id}})`
- Write the final summary to your own `runs` table at `publish`. The UI reads
  your tables, never the checkpointer.

---

## Migration order

1. Keep `review_pr()` signature identical. Only its internals change.
2. Move `agent/loop.py` logic into `agent_step` + `execute_tools`. Delete
   `state.py` (LangGraph state replaces it).
3. `grounding.py` and `anchor.py` become nodes. Their functions stay pure —
   node wrappers just read/write state.
4. Slice 1 CLI unchanged: `python -m agent.cli review <url> --dry-run`
5. Do not touch `api/` or `web/` in this migration.

## Done when

- `--dry-run` produces byte-identical output to the pre-migration version
- Killing the worker mid-run and re-invoking with the same `thread_id` resumes
  instead of restarting
- `graph.get_graph().draw_mermaid()` renders the pipeline

## Honest costs

- Debugging shifts from stack traces to inspecting graph state.
- `langchain-core` arrives transitively; dependency surface grows a lot.
- Node boundaries are now API. Splitting one later means a state migration.
