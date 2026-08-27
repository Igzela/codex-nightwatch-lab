# nightwatch

`nightwatch` supervises one non-interactive OpenAI Codex CLI thread inside one Git repository. It persists the exact thread ID, quota wait, milestones, verification evidence, and every automatic recovery decision under `.nightwatch/`.

```bash
nightwatch run "完成当前仓库规划中的所有剩余任务，并逐阶段验证，直到所有验收条件满足"
nightwatch status
nightwatch report
```

Automatic recovery uses `codex exec resume <EXACT_THREAD_ID>`. Normal recovery never uses `resume --last`; no automatic privilege escalation or sandbox bypass is enabled. If quota cannot be freshly revalidated, state remains waiting or becomes blocked.

For the unattended path—safe to close the terminal after the command returns—use:

```bash
nightwatch run --service "完成当前仓库规划中的所有剩余任务，并逐阶段验证，直到所有验收条件满足"
```

It creates durable `NEW` state, installs a repo-bound user service, reloads the
user systemd manager, and starts it. The service runs the same supervisor and
continues waiting for and revalidating quota while you are away. It restarts only
after an abnormal supervisor crash; a `BLOCKED`, `FAILED`, or `STOPPED` result is
left stopped for inspection rather than retried forever. This requires a running
user systemd manager (`systemctl --user`).

Alternatively, install the launcher with `nightwatch install`. To install (but
not start) a repo-bound user service, run `nightwatch install --service --repo
"$PWD"`, then enable it with `systemctl --user daemon-reload && systemctl --user
enable --now nightwatch.service`. The service is user-level only and does not
require root. `nightwatch uninstall` removes only files carrying Nightwatch's
install marker.

See `../analysis/architecture-decision.md` and `../analysis/final-report.md` for the design and release evidence.
