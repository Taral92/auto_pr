# auto-pr — runtime pipeline

One PR = one pass through steps 1-17. Nothing persists between runs except
traces and job rows.

## 0. Install (once per org)
- User installs the GitHub App
- Store `installation_id` + repo list
- No clone, no index, nothing else

## 1. Webhook in
- `POST /webhook`
- Verify `X-Hub-Signature-256` HMAC
- Filter: `pull_request`, action in (opened, synchronize, reopened)
- Insert job row (SQLite)
- Return `202`
- Budget: < 500ms. NEVER call the model here.

## 2. Coalesce (before insert)
- Mark queued jobs for same `(repo, pr)` with older `head_sha` -> superseded
- In-flight job with older sha -> set cancel flag
- One review per PR state, not per push

## 3. Worker claims
- Poll SQLite
- Conditional UPDATE: `state=running, leased_until=now+N`
- Lease > max wall-clock budget, or the job runs twice

## 4. Auth
- App private key -> RS256 JWT (10 min max)
- JWT -> installation access token (1 hr)
- Refresh on use. A long review outlives its token.

## 5. Fetch diff
- `GET /repos/{o}/{r}/pulls/{n}`, `Accept: application/vnd.github.v3.diff`
- Cap size. Too large -> post "too large to review", stop. Don't blow context silently.

## 6. Materialize workspace
- Sandbox: no network, agent gets read-only mount
- `git init` -> `remote add origin <token url>` -> `fetch --depth=1 origin <head_sha>` -> `checkout FETCH_HEAD`
- `repo_root` is now unambiguously the POST-change tree. Say so in the prompt.
- Register cleanup in `finally`

## 7. Blast radius  (this is what replaces a vector DB)
- Parse diff -> changed files + changed symbols
- Per symbol, exact lookup:
  - references / callers
  - imports + importers
  - tests naming it
- Output: ranked file list + reason per file
- grep + AST. No embeddings. Exact and current at head_sha.

## 8. Assemble context
- Stable cached prefix: system prompt, tool schemas, enums
- Then: diff, blast-radius summary
- Explicit: "files you read are the post-change tree"

## 9. Agent loop  (existing shape, unchanged)
- model call -> tool_use -> dispatch -> tool_result -> repeat
- Tools: `read_file`, `search_code`, `find_references`, `find_tests`
- Sandbox rules: path jail, per-call timeout, output truncation
- Tool errors return as `tool_result` content, NOT exceptions

## 10. Budget governor (concurrent)
- iterations / tokens / wall-clock / cost
- Exhausted -> stop, mark `degraded`, continue to 11 with what exists
- Never go silent

## 11. Parse
- Strict schema. One repair attempt. Then fail.
- No first-`{`-to-last-`}` slicing.

## 12. Grounding gate
- Corpus = diff + every `tool_result` from this run
- exact substring -> `grounded`
- whitespace-normalized match -> `near`
- no match -> `ungrounded`, drop, count
- Enforce min span length (~1 full line) or `os.system` passes for free

## 13. Anchor
- grounded AND span is in the diff -> compute line + side -> inline comment
- grounded but only in a read file -> summary body item
- ungrounded -> dropped
- Why this step exists: GitHub 422s on a comment anchored off-diff

## 14. Dedupe
- Fingerprint = `(path, normalized_evidence_hash, category)`
- Skip anything already posted on this PR
- Survives rebases better than line numbers

## 15. Publish
- Idempotency key = `(repo, pr, head_sha, prompt_sha)`
- Already published -> skip (GitHub redelivers, guaranteed)
- `POST /repos/{o}/{r}/pulls/{n}/reviews`
- Publish even with 0 findings, so "clean" is distinguishable from "crashed"

## 16. Record
- trace: steps, tools, tokens, timings, `prompt_sha`
- counts: grounded / near / ungrounded, inline / summary / dropped
- job state -> published | degraded | dead-letter

## 17. Cleanup
- Destroy workspace in `finally`
- Nothing customer-owned persists

## Later (async, different clock)
- Webhook on comment resolved / dismissed / thread outdated
- Label -> eval corpus -> scorer -> prompt registry

---

# Build order

**A. Core — posts a real comment**
- Steps 5, 6, 9, 11, 12, 13, 15
- Hardcode one PR URL, PAT auth, run by hand
- Done when: a real inline comment appears on a real PR

**B. App shell**
- Steps 0, 1, 3, 4
- App registration, HMAC, SQLite jobs, worker process, smee.io for local dev

**C. Quality**
- Step 7 (blast radius) + step 8
- The retrieval work. Only worth it once A posts comments.

**D. Hardening**
- Steps 2, 10, 14, 16
- Coalesce, budgets, dedupe, trace

**E. Loop closes**
- Feedback webhook, eval corpus, scorer

Do not reorder. B before A costs days of JWT/tunnel work with nothing posted.
