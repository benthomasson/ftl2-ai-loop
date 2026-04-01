# ftl2-iac-loop Development History

## Core Loop (Feb 9)

Observe/decide/execute reconciliation cycle. Uses `claude -p` (pipe mode) for AI decisions — no SDK dependency. Describes desired state in natural language, observes current infrastructure via FTL2 modules, AI decides what actions to take, executes them, repeats until convergence. Secret bindings and state file support from day one.

## Rule System (Feb 9)

Deterministic rules checked before calling the AI. Rules are Python files with `observe`, `condition()`, and `action()` functions. If a rule matches, it fires without an AI call (fast, free). Consecutive run limiting prevents infinite loops. Failed rules fall through to AI gracefully.

## User Interaction (Feb 9)

Ask-user feature: AI can pause and ask for clarification or present options. Dev mode (`--dev`): AI reviews each rule before it fires, approving or denying execution. Gives operators visibility into automated decisions.

## Operating Modes (Feb 9-10)

- **Default**: single reconciliation run, exit with status
- **Continuous** (`--continuous`): re-reconcile after configurable delay, carry forward observations, detect code updates between runs
- **Incremental** (`--incremental`): AI plans work in increments, prompts for additional work after each convergence
- **Plan-only** (`--plan-only`): show planned increments without executing, save/load plans as JSON

## Remote Hosts (Feb 9)

Actions can target remote hosts via inventory. Auto-registration: when cloud provisioning modules (e.g., `community.general.linode_v4`) succeed, the new host is automatically registered via `ftl.add_host()` and SSH is waited for. No manual inventory management needed for provisioning workflows.

## Self-Review (Feb 9)

Post-convergence: AI reviews what it did and writes deterministic rules for patterns it wants to handle automatically next time. Reviews happen on both success and failure. Feature requests generated alongside rules. Iteration budget warnings prevent runaway costs.

## Observability (Feb 10)

Four logging systems:
- **Audit log** (`--audit-log`): JSON action history with timestamps and results
- **Prompt log** (`--prompt-log`): every AI prompt/response pair saved to numbered files
- **Review log** (`--review-log`): self-review markdown files
- **Script log** (`--script-log`): generated FTL2 scripts

## Rule Lifecycle (Feb 10)

Broken rules tracked in `rules.json` and skipped on subsequent loads. Rule review command (`--review-rules`) audits all rules for conflicts and issues. Disabled rules supported via config.

## Script Generation (Feb 10)

After convergence, the action history is mechanically translated into a standalone FTL2 Python script. AI reviews and improves the draft. Scripts logged to `--script-log` directory. Scripts are not auto-executed — saved for manual use.

## Policy Support (Feb 11)

`--policy` and `--environment` CLI flags thread the FTL2 policy engine into the reconciliation loop. Every module execution — whether from a rule or an AI decision — is checked against policy before running. Deny rules raise `PolicyDeniedError`.

## Pluggable Ask User Backends (Feb 11)

The `ask_user` function is now a pluggable callable. The AI loop calls `ask_user({"question": "...", "options": [...]})` and doesn't care which backend handles it. Three built-in backends:

- **stdin** (default) — interactive terminal, `input()` blocks until user responds
- **non-interactive** (`--non-interactive`) — auto-selects first option when options provided, returns "(no answer)" otherwise. Used automatically in `--continuous` mode
- **Slack** (`--ask-via-slack CHANNEL`) — posts questions to a Slack channel, polls for thread replies

All entry points (`reconcile()`, `plan()`, `run_incremental()`, `run_continuous()`) accept an `ask_user` callable. Custom backends just need to implement `def backend(ask_data: dict) -> str`.

## Slack Approval Backend (Feb 11)

`make_ask_user_slack()` factory returns a configured `ask_user` callable that posts questions to Slack and polls for thread replies. Uses `urllib.request` from stdlib — zero extra dependencies.

- Posts questions via `chat.postMessage` with emoji formatting and numbered options
- Polls `conversations.replies` (GET) for thread replies
- Resolves numbered option picks (reply "1" → "yes")
- Posts acknowledgment in thread when answer received
- Configurable poll interval and timeout
- Timeout rejects plan confirmation (safe default)

Requires a Slack App with bot token (`xoxb-`), `chat:write` and `channels:history` scopes. No webhooks, no inbound networking — works from inside an EE or behind NAT.

## Plan Confirmation (Feb 11)

In `--incremental` mode, the plan is confirmed through whatever `ask_user` backend is active before any increments execute. Only an explicit "yes" proceeds — timeouts, no-answer, and "no" all reject. Non-interactive mode auto-approves (selects first option).
