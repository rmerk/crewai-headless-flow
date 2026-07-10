# HITL escalation reaches humans through an `escalation.py` channel seam

## Status
Accepted.

When a human-feedback gate fired, the Flow blocked on stdin `input()`. In an unattended (no-TTY) run that degrades to `EOFError` → full-run abort, which made conditional HITL useless for autonomy: every trigger killed the run it was supposed to safeguard.

`src/crewai_headless_flow/escalation.py` — deliberately parallel to `hitl_policy.py` — makes "how the question reaches a human" pluggable behind one tiny contract:

```
handler.ask(request: EscalationRequest) -> str | None
```

The raw answer string feeds the Flow's existing parsing; `None` means "no answer available" and routes into the exact path `EOFError` took before — record a `no-input` feedback entry, `_mark_human_abort`, park resumably via the aborted-checkpoint machinery. All action parsing (`_parse_human_feedback_action`, advanced actions, instruction capture) stays in `flow.py` untouched; `hitl_policy` still decides *whether* to ask, `escalation` only decides *how*.

Channels (config: `human_feedback.escalation`, dotted-overridable via `--override-human-feedback escalation.channel=...`):

- **stdin** (default) — byte-for-byte the old behavior; the builtin `input` is looked up at call time so existing `patch("builtins.input")` tests keep working.
- **file** — writes `pending_approval.json` into the run dir (ADR-0004) and returns `None`, parking the run. The operator answers by adding an `"answer"` field and re-running with `--resume-state-file`; resume replays the gate and the handler consumes the answer (renaming to `answered_approval.json`). No polling, no long-lived process — the already-tested park/resume machinery is the approval loop.
- **command** — runs a configured argv with the request JSON on stdin and reads the answer from the first non-empty stdout line, with `timeout_seconds` and `on_timeout: abort|proceed`. This is where Slack/email/webhook notification plugs in **without the platform growing network code**, preserving offline testability (tests inject a fake runner).

Known limitation, on purpose: the follow-up prompts for `capture_instructions` and `target-tasks` remain raw stdin `input()` — they degrade gracefully on EOF and are documented as stdin-channel features. Routing multi-round dialogs through file/command channels is future work.

See `docs/plans/2026-07-10-phase-1-unattended-reliability.md` and `docs/architecture/autonomy-gap-analysis.md` (Gap 4).
