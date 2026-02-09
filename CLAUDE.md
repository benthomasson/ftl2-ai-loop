# CLAUDE.md — ftl2-ai-loop

## What This Is

An AI reconciliation loop for infrastructure automation. Like a Kubernetes controller, but the controller logic is an LLM instead of hardcoded Go. The AI observes infrastructure state via FTL2 modules, decides what to do, executes, and iterates until convergence.

Over time, the AI writes deterministic rules for recurring patterns. The system progressively self-hardens: AI handles everything at first, then rules take over routine cases, and the AI focuses on novel situations.

## Architecture

```
observe (FTL2 modules) → check rules → [match? → deterministic action]
                                        [no match? → AI decides → execute → optionally write rule]
```

### Core Components

- `observe()` — Runs FTL2 modules to gather current state (command, stat, service, etc.)
- `check_rules()` — Tests loaded rules against current state. If a rule matches, execute its action without calling the AI.
- `decide()` — Pipes current state + desired state to `claude -p`. Returns a JSON decision with actions to take and optionally a rule to save.
- `execute()` — Runs the decided FTL2 module calls.
- `reconcile()` — The main loop tying it all together.

### LLM Interface

Uses `claude -p` (Claude Code pipe mode) via `asyncio.create_subprocess_exec`. No SDK dependency. The prompt includes current state, desired state, existing rules, and recent action history.

### Rules

Rules are Python files in `rules/` with two async functions:
- `condition(state: dict) -> bool` — Does this rule apply?
- `action(ftl) -> None` — What FTL2 modules to call.

The AI generates these. They use the same FTL2 module calls as any FTL2 script.

## Running

```bash
# Standalone via uv
uv run ftl2_ai_loop.py "ensure /tmp/demo exists as a directory"

# With inventory for remote hosts
uv run ftl2_ai_loop.py "nginx installed and running" -i inventory.yml

# Dry run — observe and decide but don't execute
uv run ftl2_ai_loop.py "PostgreSQL 16 with mydb database" --dry-run

# As installed package
ftl2-ai-loop "nginx serving example.com with TLS"
```

## Key Files

| File | Purpose |
|------|---------|
| `ftl2_ai_loop.py` | Main script — observe, decide, execute, learn loop |
| `rules/` | AI-generated rules (Python files with condition/action) |
| `examples/` | Example usage scripts |

## FTL2 Module Reference

The AI decides which modules to call. Common ones:

| Module | Use |
|--------|-----|
| `command` | Run a command, get stdout |
| `shell` | Run shell command (supports pipes, redirects) |
| `file` | Create/remove files and directories |
| `copy` | Copy content to a file |
| `template` | Render Jinja2 template to file |
| `stat` | Check if file exists, get metadata |
| `service` | Start/stop/restart services |
| `dnf` / `apt` | Install/remove packages |
| `user` | Create/modify users |

All Ansible modules are available via FQCN (e.g., `community.general.slack`).
