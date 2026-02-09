# ftl2-ai-loop

AI reconciliation loop for FTL2. Describe the desired state of your infrastructure in natural language, and the AI observes current state, decides what to do, executes FTL2 module calls, and iterates until convergence.

Over time, the AI writes deterministic rules for recurring patterns. The system progressively self-hardens: AI handles everything at first, rules take over routine cases, and the AI focuses on novel situations. Cost converges toward zero.

## Quick Start

Requires Python 3.13+ and [Claude Code](https://claude.ai/code) installed (`claude -p` is the LLM interface).

```bash
# Run directly from GitHub — no install needed
uvx --from "git+https://github.com/benthomasson/ftl2-ai-loop" \
    ftl2-ai-loop "ensure /tmp/demo exists as a directory"
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
ftl2-ai-loop "nginx installed and running"

# Dry run — observe and decide but don't execute
ftl2-ai-loop "PostgreSQL 16 with mydb database" --dry-run

# Remote hosts via inventory
ftl2-ai-loop "nginx installed and running" -i inventory.yml

# With secret bindings (reads from environment variable)
ftl2-ai-loop "start a new linode server named hello-ai" \
    -s community.general.linode_v4.access_token=LINODE_TOKEN \
    -s community.general.linode_v4.root_pass=LINODE_ROOT_PASS

# Track resources across runs with state file
ftl2-ai-loop "ensure my-server exists on linode" --state-file state.json

# Custom rules directory
ftl2-ai-loop "nginx installed and running" --rules-dir my-rules/

# Limit iterations
ftl2-ai-loop "complex setup" --max-iterations 5
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

## Rules

Rules are Python files the AI writes to handle recurring patterns. Each rule has a `condition()` function that checks current state and an `action()` function that makes FTL2 module calls.

```python
# rules/ensure_nginx.py
"""Install and start nginx when it's missing.
Created: 2026-02-09 12:00 UTC by ftl2-ai-loop.
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

## Programmatic Usage

```python
import asyncio
from ftl2_ai_loop import reconcile

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

## Known Limitations

- **Multi-host orchestration**: The AI can create remote servers but configuring them requires inventory management, host targeting (`ftl.hostname.module()`), and SSH setup. This workflow is not yet fully supported.
- **Rule quality**: AI-generated rules can reference nonexistent observer keys (creating always-true conditions), use wrong module syntax, or target the wrong host. Rule review and lifecycle management is not yet implemented.
- **Background processes**: FTL2's shell module blocks until all child processes exit. Background daemons must be started with `setsid ... < /dev/null &` — `nohup &` alone is insufficient.
- **`copy` module**: Does not support the `content` parameter. Use `shell` with `echo` or heredoc instead.

## Requirements

- Python 3.13+
- [Claude Code](https://claude.ai/code) installed and configured (`claude` CLI available in PATH)
- [FTL2](https://github.com/benthomasson/ftl2) (installed automatically as dependency)
