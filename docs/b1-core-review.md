# B1 — Core review path (posts a real comment)

Goal: `python -m agent.cli review <pr-url>` posts a real inline review on a real
GitHub PR. No server, no App, no database.

## Entry point

```
python -m agent.cli review https://github.com/{owner}/{repo}/pull/{n} [--dry-run]
```

- `--dry-run` prints the review payload instead of POSTing. Build this FIRST.
  You will iterate many times and must not spam a real PR.
- Auth: `GITHUB_TOKEN` env var (PAT). `repo` scope for private, `public_repo` for public.

Core function must be importable and server-agnostic:

```
def review_pr(owner: str, repo: str, number: int, token: str, dry_run: bool = False) -> ReviewResult
```

B3 will call this from a worker. Do not couple it to argv.

---

## Step 1 — Fetch PR metadata

`GET /repos/{owner}/{repo}/pulls/{number}`

Need: `head.sha`, `base.sha`, `head.ref`.

## Step 2 — Fetch the diff

Same endpoint, header `Accept: application/vnd.github.v3.diff`.
Returns raw unified diff text.

- Cap it. Suggest 400 KB.
- Over cap -> post a body-only comment "diff too large to review", exit clean.

## Step 3 — Clone the head SHA

Use the PR ref, not the SHA — it always works and does not depend on
server-side reachability settings:

```
git init
git remote add origin https://x-access-token:<TOKEN>@github.com/{owner}/{repo}.git
git fetch --depth=1 origin pull/{number}/head
git checkout FETCH_HEAD
```

- Into `tempfile.mkdtemp()`.
- Assert `git rev-parse HEAD` == `head.sha`. If not, the PR moved mid-run — abort.
- Delete the directory in a `finally`. Non-negotiable.
- Never log the token. Redact the remote URL before printing anything.

`repo_root` is now unambiguously the POST-change tree.

## Step 4 — System prompt change

Add, verbatim intent:

> The repository you can read with tools is the code AFTER this diff is applied.
> Cite added code from the diff or from the files; both agree.

This retires the pre/post ambiguity from run `2026-08-30T21-15-09`.

## Step 5 — Schema (fold in from issue-002)

`Finding`:
- `severity`: enum `blocker | should_fix | nit`
- `category`: enum `correctness | security | performance | maintainability | test_gap`
- `file`: str
- `title`, `description`, `recommendation`: str
- `evidence`: str — **verbatim span, copied exactly.** Not a summary.
- REMOVE `line` (model guesses it wrong; we compute it)
- REMOVE `confidence` (uncalibrated, unused)

## Step 6 — Run the loop

Unchanged. `repo_root` = the temp clone.
Collect every `tool_result` content — the grounding gate needs them.

## Step 7 — Grounding gate

Corpus = the diff text + every `tool_result` content from this run.

Per finding:
- `evidence` is an exact substring of any corpus entry -> `grounded`
- matches only after collapsing whitespace runs + normalising line endings -> `near`
- no match -> `ungrounded`

Keep `grounded`. Drop the rest. Count all three.

Minimum span: at least one full line, or ~20 chars. Otherwise `os.system`
passes the gate for free.

## Step 8 — Anchoring  (the part that will break)

GitHub returns **422** if you anchor a comment to a line not in the diff.
So compute anchorable lines yourself.

Parse the diff:
- For each file, read hunk headers `@@ -a,b +c,d @@`
- Walk the hunk body, tracking a new-file line counter starting at `c`
- ` ` (context) -> counter++, line IS anchorable
- `+` (added)   -> counter++, line IS anchorable
- `-` (removed) -> counter unchanged, NOT anchorable on RIGHT

Result: `anchorable: dict[path, set[int]]`

Then per grounded finding:
- Locate the `evidence` span inside the diff text
- Derive `(path, new_line_number)` from the match position
- `new_line_number in anchorable[path]` -> **inline comment**
- Otherwise (matched only in a read file, not the diff) -> **summary body item**
- Never silently drop a grounded finding just because it cannot be anchored

## Step 9 — Publish

```
POST /repos/{owner}/{repo}/pulls/{number}/reviews
{
  "commit_id": "<head.sha>",
  "event": "COMMENT",
  "body": "<summary + findings that could not be anchored>",
  "comments": [
    {"path": "...", "line": 42, "side": "RIGHT", "body": "..."}
  ]
}
```

- `event: COMMENT`. Not `REQUEST_CHANGES` — do not block merges in v1.
- `commit_id` must be `head.sha`.
- **Publish even with zero findings.** "Reviewed, nothing found" must be
  distinguishable from "it crashed".

## Step 10 — Trace

Write `runs/*.json` as today, plus:
- `pr_url`, `head_sha`, `prompt_sha`
- `grounding`: `{grounded, near, ungrounded}`
- `anchoring`: `{inline, summary}`
- `wall_clock_s`, `tokens_in_total`, `tokens_out_total`

---

## Error cases

| Case | Behaviour |
|---|---|
| Diff over cap | body-only "too large", exit 0 |
| Clone fails | raise, post nothing |
| HEAD != head.sha | abort, PR moved mid-run |
| 422 on review POST | log which comment, retry once with it moved to body |
| Zero findings | still post |
| Any exception | temp dir still deleted (`finally`) |

## Done when

1. `--dry-run` prints a valid review payload with at least one anchored comment
2. Without `--dry-run`, a real inline comment appears on a real PR
3. Temp dir is gone afterwards — verify, don't assume
4. Trace records grounding + anchoring counts

## Test subject

Open a PR on `auto-pr` itself applying `fixtures/pr-001.diff`.
Real API, ground truth already known.

## Out of scope for B1 (see hard requirements below)

- No webhook, no App auth (JWT/installation token), no SQLite, no worker
- No blast radius / `find_references` / `find_tests`
- No coalescing, dedupe, or idempotency key
- No budget governor beyond existing `MAX_ITERATIONS`
- No frontend

## Note

`agent/claude.md` says loop.py / tools.py / state.py are hand-written only.
B1 edits `loop.py`. Resolve that line before Cursor reads both files.

---

# B1 hard requirements (NOT optional, NOT deferrable)

These are not polish. Without them B1 either fails on the first real repo or is
unsafe to point at someone else's code.

## H1 — Tool path jail

`read_file` and `search_code` must not escape `repo_root`.

- Resolve the requested path, resolve `repo_root`, assert the former is inside
  the latter after `os.path.realpath` on both.
- Reject symlinks that resolve outside.
- Violation is a **bug, not a blip**: return an error to the model, log it
  loudly, do NOT retry.

`fixtures/pr-001.diff` is literally an attack on this surface. Shipping the
tool without the jail while reviewing untrusted repos is the one thing here
that is actually dangerous.

## H2 — Tool errors return as tool_result, never as exceptions

Today an exception in a tool kills the whole job.

- `search_code` will `UnicodeDecodeError` on the first PNG in any real repo.
- `read_file` will raise on a missing path the model guessed.
- A bad regex from the model raises `re.error`.

Catch, format as an error string, return it as the `tool_result` content.
The agent should lose one iteration, not the job.

## H3 — Output truncation + ignore list

Real repos break the naive versions.

- `read_file`: cap output (suggest 60 KB). Append an explicit
  `[truncated: N more lines]` marker so the model knows it did not see all of it.
- `list_files`: skip `.git`, `.venv`, `node_modules`, `dist`, `build`,
  `__pycache__`, `*.lock`. Cap total entries.
- `search_code`: cap hit count. Skip files that fail to decode as UTF-8.

Without this, one `list_files` on a Node repo is a context bomb.

## H4 — Wall-clock and cost ceiling

`MAX_ITERATIONS = 10` does not bound spend. A single iteration can read a
5 MB file.

- Wall-clock ceiling per run (suggest 300s)
- Cumulative token ceiling per run
- On breach: stop the loop, mark the run `degraded`, publish what exists with
  the reason in the review body. Never go silent.

## H5 — Idempotency key computed and stored

Not enforced in B1 (no redelivery yet), but computed and written to the trace:

`sha256(repo + pr_number + head_sha + prompt_sha)`

Retrofitting this after you have run history is painful. Storing it now is free.

## H6 — Secret handling

- Token never appears in logs, tracebacks, or the trace file.
- Redact the remote URL before printing anything.
- `.env` stays gitignored. Verify: `git check-ignore .env` must exit 0.

---

# Genuinely deferred (sequencing, not compromise)

Cannot be built before the core posts a comment:

- Webhook endpoint, App JWT/installation auth  -> B3
- SQLite job store, worker process             -> B2
- Coalescing, dedupe, idempotency enforcement  -> B5
- Blast radius / find_references / find_tests  -> C
- Frontend                                     -> F2+

