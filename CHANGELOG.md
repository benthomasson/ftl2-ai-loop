# ftl2-ai-loop Development History

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
