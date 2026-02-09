# CLAUDE.md — ftl2-ai-loop

## What This Is

An AI reconciliation loop for FTL2. Describe the desired state in natural language, and the AI observes infrastructure state via FTL2 modules, decides what actions to take, executes them, and iterates until convergence. Over time it writes deterministic Python rules for recurring patterns — the system progressively self-hardens.

## Architecture

```
observe (FTL2 modules) → check rules → [match → execute rule action → next iteration]
                                        [no match → AI decides → execute → optionally write rule]
```

### Core Components in `ftl2_ai_loop.py`

| Function | Purpose |
|----------|---------|
| `observe()` | Runs FTL2 modules to gather current state into a dict |
| `load_rules()` | Loads Python rule files from `rules/` via `importlib.util` |
| `check_rules()` | Tests rule conditions against state, executes matching action (normal mode) |
| `find_matching_rule()` | Finds first matching rule without executing (used in dev mode) |
| `execute_rule()` | Runs a single rule's action, returns `(success, detail)` |
| `build_prompt()` | Constructs the LLM prompt with state, desired state, rules, history, answers, rule results |
| `decide()` | Calls `claude -p` with the prompt, parses JSON response |
| `build_review_prompt()` | Constructs the pre-fire rule review prompt (dev mode) |
| `review_rule()` | Calls `claude -p` to approve/deny a rule before it fires (dev mode) |
| `ask_user()` | Prompts the user for input when the AI needs information |
| `execute()` | Runs FTL2 module calls from the AI decision |
| `reconcile()` | Main loop: observe → rules → decide → execute → learn |
| `cli()` | Argument parser and entry point |

### LLM Interface

Uses `claude -p` (Claude Code pipe mode) via `asyncio.create_subprocess_exec`. No anthropic SDK dependency. The AI returns a JSON object with:
- `converged` — whether the desired state is achieved
- `reasoning` — explanation of what it sees
- `actions` — list of `{"module": "name", "params": {...}}` to execute
- `observe` — optional additional observations to request next iteration
- `rule` — optional rule to save for future use
- `state_ops` — optional state file operations
- `ask` — optional question to pause and ask the user (with optional `options` array)

### Rules

Rules are Python files in `rules/` with two async functions:
- `condition(state: dict) -> bool` — Does this rule apply?
- `action(ftl) -> None` — What FTL2 modules to call (using `await ftl.module_name(**params)`)

Rules are checked before the AI. If a rule matches, its action runs and the AI is skipped. Failed rules (exceptions or `ftl.errors` growth) fall through to the AI. Consecutive rule runs are limited to 1 to prevent infinite loops from always-true conditions.

In dev mode (`--dev`), the AI reviews rules before they fire — it sees the rule source code and current state, and approves or denies. After execution, results are tracked in `rule_results` and included in the next iteration's prompt so the AI can evaluate effectiveness.

### Ask User

The AI can return an `"ask"` field to pause the loop and prompt the user. The `ask_user()` function prints the question, shows numbered options if provided, reads from stdin, and resolves number picks. The answer is stored in `user_answers` and included in subsequent prompts. The AI should set `"actions": []` when asking — don't act and ask in the same response.

### Secret Bindings

Secrets are injected via FTL2's `secret_bindings` mechanism. CLI format: `-s community.general.linode_v4.access_token=LINODE_TOKEN` maps the env var `LINODE_TOKEN` to the `access_token` parameter of the `community.general.linode_v4` module.

### State File

Optional `--state-file state.json` tracks resources and hosts across runs. The AI can read `_state_file` from observations and issue `state_ops` to add/remove entries.

## Running

```bash
# Via uvx from GitHub (no install)
uvx --from "git+https://github.com/benthomasson/ftl2-ai-loop" \
    ftl2-ai-loop "ensure /tmp/demo exists as a directory"

# Local development
uv run ftl2_ai_loop.py "ensure /tmp/demo exists as a directory"

# With options
ftl2-ai-loop "nginx running" -i inventory.yml --dry-run --max-iterations 5
ftl2-ai-loop "create linode" -s community.general.linode_v4.access_token=LINODE_TOKEN

# Dev mode — AI reviews rules before they fire
ftl2-ai-loop "nginx running" --dev --rules-dir my-rules/
```

## FTL2 Module Reference

FTL2 uses Ansible-compatible modules. Common ones:

| Module | Use |
|--------|-----|
| `command` | Run a command, get stdout |
| `shell` | Run shell command (supports pipes, redirects) |
| `file` | Create/remove files and directories |
| `copy` | Copy files (does NOT support `content` param) |
| `stat` | Check if file exists, get metadata |
| `service` | Start/stop/restart services (Linux only) |
| `dnf` / `apt` | Install/remove packages |
| `user` | Create/modify users |

FQCN modules (e.g., `community.general.linode_v4`, `community.general.homebrew`) are accessed via dot notation on the `ftl` object.

## Known Issues and Constraints

- **`copy` module**: Does not support `content` parameter. Use `shell` with heredoc instead.
- **Background processes**: `nohup &` hangs the shell module. Must use `setsid ... < /dev/null &` for daemons.
- **Platform awareness**: The AI prompt includes guidance for macOS vs Linux module selection (homebrew vs dnf/apt, no service module on macOS).
- **Rule quality**: AI-generated rules can reference nonexistent observer keys (always-true conditions) or use wrong syntax. Use `--dev` for AI-assisted pre-fire review. Automatic lifecycle management (rewrite, delete, disable) is not yet implemented.
- **Multi-host**: Creating remote servers works, but configuring them requires inventory management and host targeting (`ftl.hostname.module()`) which is not yet fully supported.
- **Module failures**: FTL2 module failures don't raise Python exceptions. `check_rules()` detects failures by comparing `len(ftl.errors)` before and after execution.

## File Structure

```
ftl2_ai_loop.py          # Everything — observe, decide, execute, learn, CLI
pyproject.toml           # Package config, hatchling build, ftl2 dependency
examples/
  nginx_example.py       # Programmatic usage with custom observers
rules/
  .gitkeep               # AI-generated rules go here
```
