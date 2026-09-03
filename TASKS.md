# Open work

Ordered. Do not skip ahead — each step's failure mode is cheaper to find than
the next one's.

## 1. Smoke the Postgres path  ← START HERE

None of the new SQL has run against a real Postgres. Expect typos.

```bash
docker compose up -d db
pip install -r requirements.txt
uvicorn api.main:app --port 8000     # applies schema on startup
python -m worker.main                # separate shell
```

Verify:
- [ ] `GET /healthz` returns server_version and state counts
- [ ] `POST /api/review {"pr_url": ..., "dry_run": true}` → 202 with run_id
- [ ] the row reaches `published` or `degraded`, never sticks in `running`
- [ ] `GET /api/runs/{id}` returns findings; `/trace` returns corpus
- [ ] two workers running: no row is ever claimed twice
- [ ] `kill -9` a worker mid-run → the lease expires → the other reclaims it

## 2. Wire the hermetic evals

`evals/fixtures/sandbox-escape/` and `agent/local.py` exist but the runner
still reviews live PRs — and the last live run read
`evals/cases/pr-001.json`, its own answer key. Every number from it is void.

- [ ] runner loads `evals/fixtures/*/{repo,head.diff,expected.json}`
- [ ] calls `review_local(repo_dir, diff)` instead of `review_pr`
- [ ] no token, no network, works on replay
- [ ] delete `evals/cases/*.json` once ported

## 3. First recorded eval

```bash
python -m evals.runner --record
python -m evals.runner --live --reps 5
```

`--reps 5` in replay mode is a no-op: replay is deterministic, so repetitions
only measure variance with `--live`.

Ground truth is 2 blockers in `app/files.py`. Watch for:
- `grounded > 0` — has never happened in a live run
- `inline > 0` — anchoring produces a postable comment
- `fp` on a two-hunk diff — it is inventing findings

## 4. Wire the App

Create the App (Contents: Read, Pull requests: Read & write, event: Pull
request), point the webhook at smee, install on one repo, open a PR.

- [ ] webhook 202s in under a second
- [ ] a real inline comment appears
- [ ] push again → old run superseded, exactly one review
- [ ] redeliver the webhook from GitHub's UI → no duplicate comment

## 5. Deploy

Two processes, one Postgres. Fly.io or a VM. `/healthz` as the check.
