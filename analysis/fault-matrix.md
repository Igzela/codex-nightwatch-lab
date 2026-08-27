# Nightwatch fault matrix

The matrix is the contract exercised by the unit, fake-Codex, and recovery
tests. `BLOCKED` and `FAILED` are terminal until a deliberate `resume`; neither
path silently starts a new thread.

| Case | Expected state | Expected action | Forbidden action | Automated evidence |
|---|---|---|---|---|
| 5h usage limit | `WAIT_QUOTA` | Persist structured reset, increment generation, revalidate later | Resume before a fresh quota read | `test_quota_waits_revalidates_then_resumes_same_thread` |
| Weekly usage limit | `WAIT_QUOTA` | Preserve `weekly` window and reset | Treat as a temporary 429 | `test_weekly_limit_is_distinguished` |
| Temporary 429 | `RETRY_BACKOFF` then bounded retry | Exponential bounded backoff | Retry forever or immediately spin | `test_transient_errors_are_bounded_backoff_not_immediate_retry` |
| Capacity overload | `RETRY_BACKOFF` then bounded retry | Same transient budget as capacity errors | Consume quota in an unbounded loop | Same transient test |
| Network disconnect | `RETRY_BACKOFF` then bounded retry | Same transient budget as network errors | Mark `DONE` from a partial stream | Same transient test |
| Auth failure | `FAILED` | Record auth error and stop | Retry authentication forever | `test_auth_failure_never_loops` |
| Codex crash with durable thread | bounded exact-thread recovery | `codex exec ... resume <EXACT_ID>` | Start a new thread or use `resume --last` | `test_codex_crash_resumes_exact_thread` |
| Codex crash before thread capture | `BLOCKED` | Preserve evidence and require intervention | Guess a session identity | `test_malformed_jsonl_and_missing_thread_fail_closed` |
| Malformed JSONL | `BLOCKED` | Preserve malformed-event evidence | Interpret malformed output as success | Same malformed test |
| Missing `thread.started` ID | `BLOCKED` | Stop with state-integrity error | Resume by recency or `--last` | Same missing-thread test |
| Nightwatch crash/restart | recovered original run | Reconcile child PID, Git, generation, and exact thread | Create a replacement goal/thread | `test_nightwatch_restart_preserves_thread_without_new_start` |
| Corrupt durable state | fail closed (`StateIntegrityError`) | Refuse to mutate or continue | Reconstruct state from chat/TUI guesses | `test_corrupt_state_fails_closed` |
| Wrong repository root | `BLOCKED` | Refuse provider launch | Run against a different checkout | `test_wrong_repo_and_git_conflict_are_blocked_before_provider` |
| Git conflict | `BLOCKED` | Require conflict resolution | Ask Codex to continue on ambiguous index | Same Git preflight test |
| Non-descendant Git head after verification | `BLOCKED` | Refuse unsafe recovery | Assume the new history is compatible | `test_changed_repo_head_is_rejected_after_verified_commit` |
| Duplicate quota event | one generation / one decision | Deduplicate event evidence and use a lease | Trigger multiple resumes | `test_duplicate_quota_events_classify_once_and_lease_is_single_flight` |
| Duplicate timer/poller wake | one generation / one claim | Atomic `(run_id,generation)` claim under file lock | Send two resume turns | Same single-flight test |
| Quota still exhausted after reset | `WAIT_QUOTA` | Store a later reset and poll again | Send any continuation turn | `test_quota_revalidation_still_exhausted_never_claims_or_resumes` |
| Resume immediately hits quota again | next `WAIT_QUOTA` generation | Reuse exact thread and schedule next reset | Reset generation without evidence | `test_quota_hit_again_is_a_new_generation_and_eventually_reuses_thread` |
| Task blocker | `BLOCKED` | Persist blocker and stop | Treat model's “done” wording as completion | `test_blocker_is_not_done` |
| Model says done but verification fails | `BLOCKED` after bounded correction budget | Run commands and retain failed evidence | Enter `DONE` | `test_done_but_verification_failure_cannot_become_done` |

## Release invariants

1. A resume always has the exact durable thread ID and never uses `--last` on
   the normal path.
2. A quota recovery always performs a fresh provider read before claiming a
   resume.
3. A quota generation has at most one atomic resume claim.
4. Auth and unknown provider errors do not loop forever.
5. Unknown or corrupt state fails closed.
6. Verified progress is derived from commands, not model percentages.
7. Every automatic recovery is represented in `events.jsonl` and the
   generation run log.
8. No Nightwatch path adds privilege escalation or sandbox bypass flags.
9. Log/event redaction is applied before persistence.
