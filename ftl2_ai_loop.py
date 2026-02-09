#!/usr/bin/env python3
# /// script
# dependencies = [
#     "ftl2 @ git+https://github.com/benthomasson/ftl2",
# ]
# requires-python = ">=3.13"
# ///
"""
ftl2-ai-loop — AI Reconciliation Loop

Observes infrastructure state via FTL2 modules, uses an LLM to decide
what actions to take, executes them, and iterates until convergence.
Over time, writes deterministic FTL2 rules for recurring patterns.

Usage:
    ftl2-ai-loop "nginx installed and running" -i inventory.yml
    ftl2-ai-loop "PostgreSQL 16 with mydb database" --dry-run
    uv run ftl2_ai_loop.py "ensure /tmp/demo exists as a directory"
"""
import argparse
import asyncio
import importlib.util
import json
import re
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from ftl2 import automation


# --- Observe ---


async def observe(ftl, observers: list[dict]) -> dict:
    """Gather current infrastructure state via FTL2 modules.

    Each observer is a dict with:
        name: key in the returned state dict
        module: FTL2 module to call
        params: kwargs for the module (optional)
    """
    state = {}
    for obs in observers:
        module_name = obs["module"]
        params = obs.get("params", {})
        try:
            # Support dotted module names (e.g., "community.general.slack")
            module_fn = ftl
            for part in module_name.split("."):
                module_fn = getattr(module_fn, part)
            result = await module_fn(**params)
            state[obs["name"]] = result
        except Exception as e:
            state[obs["name"]] = {"error": str(e)}
    return state


DEFAULT_OBSERVERS = [
    {"name": "hostname", "module": "command", "params": {"cmd": "hostname"}},
    {"name": "uptime", "module": "command", "params": {"cmd": "uptime"}},
    {"name": "disk", "module": "command", "params": {"cmd": "df -h /"}},
    {"name": "memory", "module": "command", "params": {"cmd": "free -m"}},
]


# --- Rules ---


def load_rules(rules_dir: str | Path) -> list[dict]:
    """Load all rule modules from the rules directory."""
    rules_path = Path(rules_dir)
    if not rules_path.exists():
        return []

    rules = []
    for rule_file in sorted(rules_path.glob("*.py")):
        spec = importlib.util.spec_from_file_location(rule_file.stem, rule_file)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if hasattr(module, "condition") and hasattr(module, "action"):
                rules.append({
                    "name": rule_file.stem,
                    "condition": module.condition,
                    "action": module.action,
                    "doc": module.__doc__ or "",
                    "path": str(rule_file),
                })
    return rules


async def check_rules(rules: list[dict], state: dict, ftl, dry_run: bool = False) -> bool:
    """Check if any rule matches the current state and execute it.

    Returns True if a rule handled the situation.
    """
    for rule in rules:
        try:
            if await rule["condition"](state):
                print(f"  Rule matched: {rule['name']}")
                if not dry_run:
                    await rule["action"](ftl)
                else:
                    print(f"  DRY RUN: would execute rule {rule['name']}")
                return True
        except Exception as e:
            print(f"  Rule {rule['name']} error: {e}")
    return False


def save_rule(rule_data: dict, rules_dir: str | Path) -> Path:
    """Save an AI-generated rule as a Python file."""
    rules_path = Path(rules_dir)
    rules_path.mkdir(parents=True, exist_ok=True)

    name = rule_data["name"]
    # Sanitize filename
    name = re.sub(r"[^a-z0-9_]", "_", name.lower())
    rule_file = rules_path / f"{name}.py"

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    description = rule_data.get("description", "")
    condition_desc = rule_data.get("condition", "")

    code = rule_data.get("code", "")
    if not code:
        # Fallback: generate a stub
        code = textwrap.dedent(f'''\
            async def condition(state: dict) -> bool:
                """Check: {condition_desc}"""
                return False  # TODO: implement

            async def action(ftl) -> None:
                """Action: {description}"""
                pass  # TODO: implement
        ''')

    header = f'"""{description}\nCreated: {timestamp} by ftl2-ai-loop.\nTrigger: {condition_desc}\n"""\n\n'
    rule_file.write_text(header + code)
    print(f"  Rule saved: {rule_file}")
    return rule_file


# --- Decide (LLM) ---


def build_prompt(current_state: dict, desired_state: str, rules: list[dict],
                 history: list[dict]) -> str:
    """Build the prompt for the LLM decision step."""
    rules_summary = ""
    if rules:
        rules_list = "\n".join(f"  - {r['name']}: {r['doc'].strip().split(chr(10))[0]}" for r in rules)
        rules_summary = f"\nExisting rules (already handled, don't duplicate):\n{rules_list}\n"

    history_summary = ""
    if history:
        recent = history[-3:]  # Last 3 iterations
        entries = []
        for h in recent:
            actions_str = json.dumps(h["actions"], indent=2)
            results_str = json.dumps(h["results"], indent=2, default=str)
            entries.append(f"Iteration {h['iteration']}:\n  Actions: {actions_str}\n  Results: {results_str}")
        history_summary = f"\nPrevious iterations (don't repeat failed approaches):\n" + "\n".join(entries) + "\n"

    state_json = json.dumps(current_state, indent=2, default=str)

    return textwrap.dedent(f"""\
        You are an infrastructure reconciliation AI. You observe the current state
        of a system and decide what FTL2 module calls to make to achieve the desired state.

        FTL2 uses Ansible modules with the same names and parameters. You MUST only use
        FTL2 modules to take actions — never use curl, pip, or other CLI tools to work
        around modules. If a module exists for the task, use it.

        Action format (for the "actions" list):
        {{"module": "dnf", "params": {{"name": "nginx", "state": "present"}}}}
        {{"module": "service", "params": {{"name": "nginx", "state": "started"}}}}
        {{"module": "file", "params": {{"path": "/tmp/test", "state": "directory"}}}}
        {{"module": "copy", "params": {{"content": "hello", "dest": "/tmp/hello.txt"}}}}
        {{"module": "command", "params": {{"cmd": "echo hello"}}}}
        {{"module": "shell", "params": {{"cmd": "ls -la /tmp"}}}}
        {{"module": "community.general.linode_v4", "params": {{"label": "myserver", "type": "g6-nanode-1", "region": "us-east", "image": "linode/ubuntu22.04", "state": "present"}}}}

        IMPORTANT: Use fully qualified collection names (FQCN) for non-builtin modules:
        - community.general.linode_v4 (not linode_v4)
        - community.general.slack (not slack)
        - community.postgresql.postgresql_db (not postgresql_db)
        - ansible.posix.firewalld (not firewalld)

        Secrets (API tokens, passwords) are injected automatically via secret_bindings.
        Do NOT read secrets from environment variables or pass them as parameters.
        Just call the module — the secret is injected by the framework.

        Current state:
        {state_json}
        {rules_summary}{history_summary}
        Desired state: {desired_state}

        Respond with ONLY a JSON object (no markdown, no explanation outside the JSON):
        {{
          "converged": true/false,
          "reasoning": "brief explanation of what you see and what needs to change",
          "actions": [
            {{"module": "module_name", "params": {{"key": "value"}}}}
          ],
          "observe": [
            {{"name": "label", "module": "module_name", "params": {{"key": "value"}}}}
          ],
          "rule": {{
            "name": "snake_case_name",
            "condition": "when this is true",
            "description": "what this rule does",
            "code": "async def condition(state):\\n    ...\\nasync def action(ftl):\\n    ..."
          }}
        }}

        Notes:
        - Set "converged" to true ONLY if the desired state is verified as achieved.
          Do not assume convergence from the existence of unrelated infrastructure.
        - "actions" is the list of module calls to make now. Empty if converged.
        - "observe" is optional: additional observations to make next iteration
          (to gather state you need but don't have yet). Use the same format as actions.
        - "rule" is optional: include if you see a pattern worth codifying as a
          permanent rule. Rule code MUST use this exact syntax for module calls:

          async def condition(state: dict) -> bool:
              return state.get("nginx_service", {{}}).get("stdout", "") != "active"

          async def action(ftl) -> None:
              await ftl.dnf(name="nginx", state="present")
              await ftl.service(name="nginx", state="started", enabled=True)
              await ftl.copy(content="<h1>Hello World</h1>", dest="/var/www/html/index.html")

          CRITICAL: In rule code, call modules as "await ftl.module_name(**params)".
          For FQCN modules use dot notation: "await ftl.community.general.linode_v4(label=..., state='present')".
          Do NOT use ftl.call(), ftl.run(), subprocess, os.system, curl, or any other method.
          Do NOT read secrets from os.environ — they are injected automatically.
        - Don't duplicate existing rules.
        - Don't repeat actions that failed in previous iterations.
    """)


def extract_json(text: str) -> str:
    """Extract JSON from LLM output, handling markdown code blocks."""
    # Try to find JSON in code blocks
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Try to find raw JSON object
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return match.group(0)
    return text.strip()


async def decide(current_state: dict, desired_state: str, rules: list[dict],
                 history: list[dict]) -> dict:
    """Ask the AI what to do via claude -p."""
    prompt = build_prompt(current_state, desired_state, rules, history)

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        error = stderr.decode().strip()
        print(f"  AI error: {error}")
        return {"converged": False, "reasoning": f"AI call failed: {error}", "actions": []}

    raw = stdout.decode().strip()
    try:
        return json.loads(extract_json(raw))
    except json.JSONDecodeError as e:
        print(f"  Failed to parse AI response: {e}")
        print(f"  Raw output: {raw[:500]}")
        return {"converged": False, "reasoning": "Failed to parse AI response", "actions": []}


# --- Execute ---


async def execute(ftl, actions: list[dict], dry_run: bool = False) -> list[dict]:
    """Execute the decided actions via FTL2 modules."""
    results = []
    for action in actions:
        module_name = action["module"]
        params = action.get("params", {})
        print(f"  → {module_name}({', '.join(f'{k}={v!r}' for k, v in params.items())})")

        if dry_run:
            print(f"    DRY RUN: skipped")
            results.append({"module": module_name, "result": {"dry_run": True}})
            continue

        try:
            module_fn = ftl
            for part in module_name.split("."):
                module_fn = getattr(module_fn, part)
            result = await module_fn(**params)
            changed = result.get("changed", False) if isinstance(result, dict) else False
            print(f"    ok (changed={changed})")
            results.append({"module": module_name, "result": result})
        except Exception as e:
            print(f"    FAILED: {e}")
            results.append({"module": module_name, "result": {"error": str(e)}})

    return results


# --- Main Loop ---


async def reconcile(
    desired_state: str,
    inventory: str | None = None,
    observers: list[dict] | None = None,
    rules_dir: str = "rules",
    max_iterations: int = 10,
    dry_run: bool = False,
    quiet: bool = False,
    secret_bindings: dict | None = None,
    state_file: str | None = None,
):
    """Run the AI reconciliation loop."""
    if observers is None:
        observers = DEFAULT_OBSERVERS

    automation_kwargs = {
        "inventory": inventory,
        "quiet": quiet,
    }
    if secret_bindings:
        automation_kwargs["secret_bindings"] = secret_bindings
    if state_file:
        automation_kwargs["state_file"] = state_file

    async with automation(**automation_kwargs) as ftl:
        rules = load_rules(rules_dir)
        history: list[dict] = []
        extra_observers: list[dict] = []

        print(f"Desired state: {desired_state}")
        print(f"Rules loaded: {len(rules)}")
        print(f"Max iterations: {max_iterations}")
        if dry_run:
            print("DRY RUN — actions will not be executed")
        print()

        for i in range(max_iterations):
            print(f"=== Iteration {i + 1} ===")

            # Observe
            print("Observing...")
            all_observers = observers + extra_observers
            current_state = await observe(ftl, all_observers)

            # Check rules first
            print("Checking rules...")
            if await check_rules(rules, current_state, ftl, dry_run):
                print("Rule handled the situation.\n")
                extra_observers = []
                continue

            # Decide
            print("Asking AI...")
            decision = await decide(current_state, desired_state, rules, history)

            reasoning = decision.get("reasoning", "")
            if reasoning:
                print(f"  Reasoning: {reasoning}")

            if decision.get("converged"):
                print(f"\nConverged after {i + 1} iteration(s).")
                return True

            # Pick up any additional observers the AI requested
            extra_observers = decision.get("observe", [])
            if extra_observers:
                print(f"  AI requested {len(extra_observers)} additional observation(s)")

            # Execute
            actions = decision.get("actions", [])
            if not actions:
                print("  No actions decided.\n")
                continue

            print(f"Executing {len(actions)} action(s)...")
            results = await execute(ftl, actions, dry_run)
            history.append({
                "iteration": i,
                "actions": actions,
                "results": results,
            })

            # Learn
            rule_data = decision.get("rule")
            if rule_data and rule_data.get("name") and rule_data.get("code"):
                print("Learning...")
                save_rule(rule_data, rules_dir)
                # Reload rules
                rules = load_rules(rules_dir)

            print()
            await asyncio.sleep(2)

        print(f"\nDid not converge after {max_iterations} iterations.")
        return False


# --- CLI ---


def cli():
    parser = argparse.ArgumentParser(
        description="ftl2-ai-loop — AI reconciliation loop for infrastructure",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              ftl2-ai-loop "nginx installed and running"
              ftl2-ai-loop "ensure /tmp/demo exists as a directory" --dry-run
              ftl2-ai-loop "PostgreSQL 16 with mydb database" -i inventory.yml
        """),
    )
    parser.add_argument("desired_state", help="Natural language description of desired state")
    parser.add_argument("-i", "--inventory", help="Inventory file for remote hosts")
    parser.add_argument("--max-iterations", type=int, default=10,
                        help="Maximum reconciliation iterations (default: 10)")
    parser.add_argument("--rules-dir", default="rules",
                        help="Directory for generated rules (default: rules/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Observe and decide but don't execute actions")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress FTL2 module output")
    args = parser.parse_args()

    converged = asyncio.run(reconcile(
        desired_state=args.desired_state,
        inventory=args.inventory,
        max_iterations=args.max_iterations,
        rules_dir=args.rules_dir,
        dry_run=args.dry_run,
        quiet=args.quiet,
    ))
    sys.exit(0 if converged else 1)


if __name__ == "__main__":
    cli()
