# Open work

## Done

1. Postgres verified against a real database — two workers, no double-claim,
   lease recovery on `kill -9`.
2. Hermetic evals. The runner loads `evals/fixtures/*/` and calls
   `review_local()` — no GitHub, no token, no network. A previous live-PR eval
   read its own answer key out of `evals/cases/`; that can no longer happen,
   and a contamination guard test stops it regressing.
3. First honest result, and the two bugs it surfaced:
   - grounding could not match multi-line evidence (diff prefixes)
   - anchoring had the same bug one layer down, so grounded findings were
     demoted to summary
   Both fixed by sharing one post-image parser.

```
sandbox-escape  tp 2  fp 0  fn 0  precision 1.0  recall 1.0
                groundedness 1.0  inline_rate 1.0  net +24 min/PR
```

50 tests pass.

## 4. Ship it  ← NEXT

Nothing has ever reached GitHub. Every run so far was `--dry-run` or local.

- [ ] Create the App: Contents **Read**, Pull requests **Read & write**,
      event **Pull request**
- [ ] `npx smee-client --url https://smee.io/<channel> --target http://localhost:8000/webhook`
- [ ] Webhook URL → the smee channel; set a secret → `GITHUB_WEBHOOK_SECRET`
- [ ] `base64 -i key.pem | tr -d '\n'` → `GITHUB_APP_PRIVATE_KEY`
- [ ] Install on `auto_pr`, open a PR

Verify, in this order — each catches a different failure:

- [ ] webhook returns 202 in under a second (it must not call the model)
- [ ] a real **inline** comment appears on the changed line
- [ ] push again → the older run is superseded, exactly one review exists
- [ ] redeliver the webhook from GitHub's UI → no duplicate comment
      (exercises `delivery_id UNIQUE` and the review-body marker)
- [ ] `kill -9` the worker mid-review → another worker reclaims after the lease

## 5. Second fixture

One fixture and two defects is not a benchmark.

- [ ] a **negative control**: a diff with no defect. Any published finding is
      a false positive. Without this you cannot tell a careful reviewer from a
      quiet one.
- [ ] a non-security fixture — every case so far is a security bug, where the
      model's priors are strongest.

## 6. Deploy

Two processes, one Postgres, `/healthz` as the check. Fly.io or a VM.
Decide whether production uses Supabase or its own Postgres; the compose `db`
service is dev-only either way.
