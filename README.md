# ftl2-iac-loop

AI reconciliation loop for FTL2. Describe the desired state of your infrastructure in natural language, and the AI observes current state, decides what to do, executes FTL2 module calls, and iterates until convergence.

Over time, the AI writes deterministic rules for recurring patterns. The system progressively self-hardens: AI handles everything at first, rules take over routine cases, and the AI focuses on novel situations. Cost converges toward zero.

## Quick Start

Requires Python 3.13+ and [Claude Code](https://claude.ai/code) installed (`claude -p` is the LLM interface).

```bash
# Run directly from GitHub — no install needed
uvx --from "git+https://github.com/benthomasson/ftl2-iac-loop" \
    ftl2-iac-loop "ensure /tmp/demo exists as a directory"
```

```
Desired state: ensure /tmp/demo exists as a directory
Rules loaded: 0
Max iterations: 10

=== Iteration 1 ===
Observing...
Checking rules...
Asking AI...
  Reasoning: The desired state requires /tmp/demo to exist as a directory.
  AI requested 1 additional observation(s)
Executing 1 action(s)...
  → file(path='/tmp/demo', state='directory')
    ok (changed=True)

=== Iteration 2 ===
Observing...
Checking rules...
Asking AI...
  Reasoning: /tmp/demo already exists as a directory. No further actions needed.

Converged after 2 iteration(s).
```

## How It Works

```
Observe (FTL2 modules) → Check rules → [match → deterministic action]
                                        [no match → AI decides → execute → optionally write rule]
```

1. **Observe** — Runs FTL2 modules to gather current infrastructure state
2. **Check rules** — Tests deterministic rules against state (fast, free, no AI call)
3. **Decide** — If no rule matches, pipes state + desired state to Claude via `claude -p`
4. **Execute** — Runs the FTL2 module calls the AI decided on
5. **Learn** — AI optionally writes a Python rule for recurring patterns

The AI can also request additional observations for the next iteration — it verifies its own work before declaring convergence.

## Usage

```bash
# Basic — describe what you want
ftl2-iac-loop "nginx installed and running"

# Dry run — observe and decide but don't execute
ftl2-iac-loop "PostgreSQL 16 with mydb database" --dry-run

# Remote hosts via inventory
ftl2-iac-loop "nginx installed and running" -i inventory.yml

# With secret bindings (reads from environment variable)
ftl2-iac-loop "start a new linode server named hello-ai" \
    -s community.general.linode_v4.access_token=LINODE_TOKEN \
    -s community.general.linode_v4.root_pass=LINODE_ROOT_PASS

# Track resources across runs with state file
ftl2-iac-loop "ensure my-server exists on linode" --state-file state.json

# Custom rules directory
ftl2-iac-loop "nginx installed and running" --rules-dir my-rules/

# Limit iterations
ftl2-iac-loop "complex setup" --max-iterations 5

# Dev mode — AI reviews rules before they fire
ftl2-iac-loop "nginx installed and running" --dev --rules-dir my-rules/

# Continuous mode — re-reconcile every 60 seconds (default)
ftl2-iac-loop "nginx installed and running" --continuous

# Continuous with custom delay (5 minutes)
ftl2-iac-loop "nginx installed and running" --continuous --delay 300

# Incremental — plan and execute step by step
ftl2-iac-loop -f infrastructure.md --incremental --state-file state.json

# Plan only — show what would be done, save to file
ftl2-iac-loop -f infrastructure.md --plan-only -o plan.json

# Execute a saved plan
ftl2-iac-loop -f infrastructure.md --incremental --plan plan.json

# With policy enforcement
ftl2-iac-loop "configure the server" --policy policy.yml --environment prod

# Full observability — audit, prompt, review, and script logs
ftl2-iac-loop -f desired_state.md --incremental \
    --audit-log audit.json \
    --prompt-log prompts/ \
    --review-log reviews/ \
    --script-log scripts/
```

### CLI Options

| Flag | Description |
|------|-------------|
| `desired_state` | Natural language description of desired state |
| `-i, --inventory` | Inventory file for remote hosts |
| `--max-iterations` | Maximum reconciliation iterations (default: 10) |
| `--rules-dir` | Directory for generated rules (default: `rules/`) |
| `--dry-run` | Observe and decide but don't execute |
| `--quiet` | Suppress FTL2 module output |
| `-s, --secret` | Bind a secret: `MODULE.PARAM=ENV_VAR` |
| `--state-file` | JSON state file for tracking resources across runs |
| `--dev` | Dev mode: AI reviews rules before they fire and sees results after |
| `--continuous` | Run continuously, re-reconciling after each delay period |
| `--incremental` | Plan work in increments, prompt for more after each convergence |
| `--plan-only` | Show planned increments without executing |
| `--plan` | Load a saved plan JSON file for `--incremental` (skips planning) |
| `-o, --output` | Output file for `--plan-only` to save the plan as JSON |
| `--policy` | YAML policy file to enforce before each module execution |
| `--environment` | Environment label for policy matching (e.g., `prod`, `staging`) |
| `--review-rules` | Review all rules for conflicts and issues |
| `--delay` | Seconds between runs in continuous mode (default: 60) |
| `--audit-log` | JSON file to append action history after each run |
| `--prompt-log` | Directory to write prompt/response pairs |
| `--review-log` | Directory to write self-review markdown files |
| `--script-log` | Directory to write generated FTL2 scripts |
| `--non-interactive` | Skip user prompts (auto-selects first option for plan confirmation) |
| `--ask-via-slack CHANNEL` | Post questions to a Slack channel and poll for thread replies |
| `--slack-poll-interval` | Seconds between Slack polls (default: 30) |
| `--slack-timeout` | Seconds before Slack question times out, 0 for no timeout (default: 0) |
| `-f, --file` | Read desired state from a file instead of the command line |

## Rules

Rules are Python files the AI writes to handle recurring patterns. Each rule has a `condition()` function that checks current state and an `action()` function that makes FTL2 module calls.

```python
# rules/ensure_nginx.py
"""Install and start nginx when it's missing.
Created: 2026-02-09 12:00 UTC by ftl2-iac-loop.
Trigger: nginx package not present
"""

async def condition(state: dict) -> bool:
    return state.get("nginx_service", {}).get("stdout", "") != "active"

async def action(ftl) -> None:
    await ftl.dnf(name="nginx", state="present")
    await ftl.service(name="nginx", state="started", enabled=True)
```

Rules are checked before the AI on every iteration. When a rule handles the situation, the AI is never called. As more rules accumulate, the system becomes faster and cheaper.

Rules that fail (action raises an exception or causes FTL2 module errors) automatically fall through to the AI. Rules that fire more than once consecutively without convergence are skipped so the AI can re-evaluate.

### Dev Mode

With `--dev`, the AI reviews every rule before it fires. When a rule's condition matches:

1. The AI sees the rule's source code and current state
2. It approves or denies the rule
3. If approved, the rule executes and the AI sees the result on the next iteration
4. If denied, the rule is skipped and the AI handles the situation directly

```
=== Iteration 1 ===
Observing...
Checking rules...
  Rule matched: ensure_nginx
  Reviewing rule...
  Review: The condition checks nginx_service but that observer key doesn't exist
          in the current state, causing a spurious match via default value.
  Denied — skipping rule ensure_nginx
Asking AI...
```

This catches the rule quality issues discovered during testing — always-true conditions, wrong host targeting, bad module syntax — before they cause problems. Rule execution results (approved/denied, success/failure) accumulate and are visible to the AI in subsequent iterations, so it can decide to rewrite or delete problematic rules.

## Continuous Mode

With `--continuous`, the loop doesn't exit after convergence — it waits and re-reconciles, catching drift and fixing it automatically. Each run is a fresh reconciliation with fresh observations.

```
Continuous mode: reconciling every 60s (Ctrl+C to stop)

============================================================
Run #1 — 2026-02-09 18:00:00
============================================================
Desired state: nginx installed and running
...
Converged after 2 iteration(s).

Run #1 converged. Next run in 60s...

============================================================
Run #2 — 2026-02-09 18:01:00
============================================================
Desired state: nginx installed and running
...
Converged after 1 iteration(s).

Run #2 converged. Next run in 60s...
```

This turns ftl2-iac-loop into a persistent controller. Combine with `--state-file` to track resources across runs and `--dev` to review rules as they develop.

## Incremental Mode

With `--incremental`, the AI plans the work as a series of increments, executes each one, then prompts for additional work. This is ideal for large infrastructure builds where you want to guide the process step by step.

```bash
# Build a minecraft server incrementally
ftl2-iac-loop -f MINECRAFT_INSTALLATION_GUIDE.md \
    --incremental \
    --state-file .ftl2-state.json \
    --script-log scripts/ \
    --review-log reviews/ \
    -s community.general.linode_v4.access_token=LINODE_TOKEN
```

The AI first creates a plan with numbered increments, then executes each one as a separate reconciliation run. After each increment converges, the AI generates a standalone FTL2 script and writes rules for patterns it wants to handle automatically.

### Plan-Only Mode

With `--plan-only`, the AI creates the plan but doesn't execute anything. Save the plan to JSON with `-o` and load it later with `--plan`:

```bash
# Plan without executing
ftl2-iac-loop -f desired_state.md --plan-only -o plan.json

# Execute a saved plan
ftl2-iac-loop -f desired_state.md --incremental --plan plan.json
```

## Policy Engine

The `--policy` flag enforces rules about what the AI loop is allowed to do. Every module execution — whether from a deterministic rule or an AI decision — is checked against the policy before running.

```bash
ftl2-iac-loop "configure the server" \
    --policy policy.yml \
    --environment prod
```

Policy file format (YAML):

```yaml
rules:
  - decision: deny
    match:
      module: "shell"
      environment: "prod"
    reason: "Use proper modules in production"

  - decision: deny
    match:
      module: "*"
      param.state: "absent"
      environment: "prod"
    reason: "No destructive actions in production"

  - decision: deny
    match:
      module: "community.general.linode_v4"
      param.state: "absent"
    reason: "Cannot destroy servers via AI loop"
```

Match conditions support fnmatch patterns:

| Condition | Matches against |
|-----------|----------------|
| `module` | Module name (e.g., `shell`, `amazon.aws.*`) |
| `host` | Target host (e.g., `prod-*`) |
| `environment` | Environment label from `--environment` |
| `param.<name>` | Specific parameter value (e.g., `param.state: absent`) |

Rules are evaluated top-to-bottom. The first matching deny rule blocks the action with `PolicyDeniedError`. If no deny rule matches, the action proceeds.

## Ask User

The AI can pause the loop to ask the user a question when it needs information it can't observe. The answer is fed back into the next iteration's context.

```
=== Iteration 1 ===
Observing...
Checking rules...
Asking AI...
  Reasoning: The desired state says "set up a web server" but doesn't specify which one.

  AI asks: Which web server should I install?
    1. nginx
    2. apache
    3. caddy
    Or type a custom answer.
  > 1
  Answer: nginx
```

The AI uses this when:
- The desired state is ambiguous
- It needs information it can't observe (domain names, preferences)
- It wants to confirm before a destructive action
- It's stuck after multiple failed attempts

### Pluggable Backends

The `ask_user` interface is pluggable — the AI loop calls `ask_user({"question": "...", "options": [...]})` and the backend handles how the question reaches a human.

| Backend | Flag | Description |
|---------|------|-------------|
| stdin | *(default)* | Interactive terminal prompt |
| non-interactive | `--non-interactive` | Auto-selects first option, no human needed |
| Slack | `--ask-via-slack CHANNEL` | Posts to Slack, polls for thread replies |

Custom backends can be passed programmatically via `reconcile(ask_user=my_backend)`.

### Slack Approvals

Post questions to a Slack channel and wait for a human to reply in the thread. No webhooks, no inbound networking — works from inside an execution environment or behind NAT.

```bash
SLACK_BOT_TOKEN=xoxb-... \
ftl2-iac-loop "ensure nginx is running" \
  --incremental \
  --ask-via-slack "#approvals" \
  --slack-poll-interval 5 \
  --slack-timeout 300
```

Requires a Slack App with bot token (`xoxb-`):
- `chat:write` scope — post messages
- `channels:history` scope — read thread replies
- Bot must be invited to the channel (`/invite @botname`)

In `--incremental` mode, plan confirmation is posted to Slack before any increments execute. Only an explicit "yes" proceeds — timeouts and "no" both reject the plan.

## Programmatic Usage

```python
import asyncio
from ftl2_iac_loop import reconcile

OBSERVERS = [
    {"name": "nginx_pkg", "module": "shell",
     "params": {"cmd": "rpm -q nginx 2>/dev/null || echo 'not installed'"}},
    {"name": "nginx_svc", "module": "shell",
     "params": {"cmd": "systemctl is-active nginx 2>/dev/null || echo 'inactive'"}},
]

async def main():
    converged = await reconcile(
        desired_state="nginx installed, running, and serving on port 80",
        observers=OBSERVERS,
        max_iterations=5,
        secret_bindings={
            "community.general.slack": {"token": "SLACK_TOKEN"},
        },
    )

asyncio.run(main())
```

See `examples/nginx_example.py` for a complete example.

## Architecture

- **LLM interface**: `claude -p` (Claude Code pipe mode) via `asyncio.create_subprocess_exec`. No SDK dependency, no API keys to manage.
- **Module system**: Uses FTL2's module system, which provides Ansible-compatible modules. All Ansible modules are available via FQCN (e.g., `community.general.linode_v4`).
- **Rules as Python**: Rules are plain Python files loaded via `importlib.util`. They use the same `await ftl.module()` syntax as any FTL2 script.
- **Secret bindings**: Secrets are injected into module calls automatically via FTL2's `secret_bindings`. The AI never sees the secret values.
- **State file**: Optional JSON file that tracks created resources and hosts across runs. The AI can read state and add/remove entries.

## Host Targeting

Actions can target remote hosts by adding a `host` field. The AI creates a server, registers it with `add_host` in `state_ops`, and then runs modules on it directly — no SSH workarounds needed.

```
=== Iteration 1 ===
Executing 1 action(s)...
  → community.general.linode_v4(label='web01', ...)
    ok (changed=True)
  Host added: web01 (203.0.113.10)

=== Iteration 2 ===
Executing 2 action(s)...
  → web01: dnf(name='nginx', state='present')
    ok (changed=True)
  → web01: service(name='nginx', state='started', enabled=True)
    ok (changed=True)
```

The `add_host` state op registers the host in FTL2's live inventory (and persists to the state file if configured). After registration, the host name can be used in the `host` field of any action.

## Known Limitations

- **SSH setup**: Host targeting requires SSH key authentication to be configured on the remote host. Pre-built images with SSH keys are the easiest path.
- **Rule quality**: AI-generated rules can reference nonexistent observer keys (creating always-true conditions), use wrong module syntax, or target the wrong host. Use `--dev` mode for AI-assisted rule review. Automatic rule lifecycle management (rewrite, delete, disable) is not yet implemented.
- **Background processes**: FTL2's shell module blocks until all child processes exit. Background daemons must be started with `setsid ... < /dev/null &` — `nohup &` alone is insufficient.


## Requirements

- Python 3.13+
- [Claude Code](https://claude.ai/code) installed and configured (`claude` CLI available in PATH)
- [FTL2](https://github.com/benthomasson/ftl2) (installed automatically as dependency)
