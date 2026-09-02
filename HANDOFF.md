# auto-pr — handoff

Last updated: 2026-09-02. Read this first in any new session.

## What this is

A PR review agent. Learning goal is agent internals, scale and eval — not
shipping a product. Repo structured so each decision can be explained.

Model: `claude-haiku-4-5-20251001`.

---

## Current state

**Works, verified:**
- `python -m agent.cli review <pr-url> --dry-run` runs end to end
- Fetch PR -> fetch diff -> clone head SHA -> LangGraph pipeline -> ground ->
  anchor -> payload -> cleanup
- FastAPI + SQLite worker: rows persist, state machine runs, findings stored
- Workspace cleanup holds even on an uncaught exception (verified: no leaked
  `/tmp/auto-pr-*` after a crash mid-`agent_step`)
- Error taxonomy classifies correctly (401 -> `PermanentError`, no retry)
- 19 tests pass: `python -m pytest tests/ -q`

**Architecture:**
- LangGraph orchestrates the *pipeline*; the model loop is a node
  (`agent_step` -> `execute_tools` -> `agent_step`). No LangChain, no
  `create_react_agent`. See `docs/langgraph-architecture.md`.
- `agent/` = loop, tools, prompts. `gh/` = GitHub + diff + anchoring.
  `core/` = models + errors. `storage/` = sqlite. `api/` + `worker/`.

---

## THE OPEN QUESTION

**No live run has ever produced a single `grounded` finding.**

| Run | findings | grounded | dropped | tokens_in |
|---|---|---|---|---|
| 1 | 3 | 0 | 3 | 26.7k |
| 2 | 6 | 0 | 6 | 133.9k |

Cause (diagnosed): the model emitted **descriptions** of code, not copies of it.

```
BAD  "Added lines in .gitignore: '+.gitignore'"
BAD  "read_file does not call os.path.realpath()"
```

The gate is NOT broken — `tests/test_grounding.py` proves `grounded` is
reachable with a verbatim span. The prompt was the problem.

**The prompt has since been rewritten** (evidence-is-code + examples,
diff-only scope, severity definitions, untrusted-content boundary).
**It has not been re-run.** That is step 1 below.

---

## Fixes applied 2026-09-02 (not yet validated live)

1. **Path jail** (`agent/tools.py`) — `_resolve()` resolves both sides then
   checks containment; symlinks skipped. `../`, absolute paths and symlink
   escapes all refused. Was a live vulnerability.
2. **Prompt rewritten** (`agent/nodes.py`) — scope limited to diffed code;
   evidence reframed as "the code itself, copied character for character" with
   GOOD/BAD examples; absence-findings quote the code that lacks the guard;
   severity definitions; "an empty findings list is a correct answer".
3. **Prompt-injection boundary** — tool results reach the model wrapped in
   `<untrusted_content source="...">`. The grounding corpus keeps the RAW text
   so evidence still matches.
4. **Summary gated** (`gh/anchor.py`) — the model's summary is only published
   if at least one finding survives grounding. Previously it published an
   unverified summary while dropping every finding supporting it.
5. **`_extract_json`** prefers a fenced block, falls back to outer braces.
6. **`started_at`** column + additive migration, set on claim. Queue wait is
   now separable from execution time (a run showed 732s wall vs 38.8s recorded).

---

## Next steps, in order

1. **Re-run PR #1 dry-run.** Expect: fewer findings, most about
   `agent/tools.py`, and `grounded > 0`. If still 0, the prompt is not the
   whole story — inspect `corpus_json` in the run row.
2. **Make a real test PR.** PR #1 is a dump-everything PR with `.pyc` files and
   docs — there is no ground truth for it.
   ```
   git checkout -b test/pr-001 && git apply fixtures/pr-001.diff
   git commit -am "known bugs" && git push -u origin test/pr-001
   ```
   Ground truth = 2 findings: `read_file` loses its `repo_root` sandbox;
   `search_code` gains an `os.system` backdoor.
3. **Bound the context.** 134k input tokens on a tiny repo. The scope rule in
   the new prompt should help; verify, then consider restricting tools to files
   in the diff.
4. **First real POST** (drop `--dry-run`) once a run produces an inline comment.

Deferred and correct to defer: webhook + GitHub App auth, coalescing,
idempotency enforcement, blast radius (`find_references`/`find_tests`),
frontend.

---

## Known issues

- `.pyc` files under `agent/__pycache__/` are tracked in git. Fix:
  `git rm -r --cached agent/__pycache__` and commit.
- `agent/claude.md` still says "No frameworks (LangGraph, LlamaIndex). Raw loop
  only." That is now false — LangGraph is in use. Update it or tools will read
  contradictory instructions.
- No frontend. Deliberate: GitHub is the UI. A read-only runs/trace viewer is
  the only one worth building, and only once grep stops scaling.

## Commands

```
python -m pytest tests/ -q
python -m agent.cli review <pr-url> --dry-run
uvicorn api.main:app --port 8000
python -m worker.main
```

## Docs

| File | Contents |
|---|---|
| `docs/pipeline.md` | the 17-step runtime pipeline + build order |
| `docs/langgraph-architecture.md` | node graph, state, what LangGraph won't do |
| `docs/backend.md` | layout, error taxonomy, claim query, sqlite pragmas |
| `docs/b1-core-review.md` | core review path + hard requirements H1-H6 |
| `docs/v0-e2e.md` | the three slices |
| `docs/roadmap-app.md` | GitHub App milestones A/B/C |
| `docs/issue-002.md` | why findings must be machine-checkable |
