# nightwatch

`nightwatch` supervises one non-interactive OpenAI Codex CLI thread inside one Git repository. It persists the exact thread ID, quota wait, milestones, verification evidence, and every automatic recovery decision under `.nightwatch/`.

```bash
nightwatch run "完成当前仓库规划中的所有剩余任务，并逐阶段验证，直到所有验收条件满足"
nightwatch status
nightwatch report
```

Automatic recovery uses `codex exec resume <EXACT_THREAD_ID>`. Normal recovery never uses `resume --last`; no automatic privilege escalation or sandbox bypass is enabled. If quota cannot be freshly revalidated, state remains waiting or becomes blocked.

Install the launcher after validation with `nightwatch install`. To create a
repo-bound user service, run `nightwatch install --service --repo "$PWD"`, then
optionally enable it with `systemctl --user enable --now nightwatch.service`.
The service is user-level only and does not require root. `nightwatch uninstall`
removes only files carrying Nightwatch's install marker.

See `../analysis/architecture-decision.md` and `../analysis/final-report.md` for the design and release evidence.
