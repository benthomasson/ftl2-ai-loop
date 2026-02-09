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
        host: optional hostname to run the observation on (default: localhost)
    """
    state = {}
    for obs in observers:
        module_name = obs["module"]
        params = obs.get("params", {})
        host = obs.get("host")
        try:
            # Start from the host proxy if targeting a remote host
            target = getattr(ftl, host) if host else ftl
            module_fn = target
            for part in module_name.split("."):
                module_fn = getattr(module_fn, part)
            result = await module_fn(**params)
            state[obs["name"]] = result

            # Persist OS facts to state file when we learn them
            if host and hasattr(ftl, 'state') and ftl.state:
                _persist_host_facts(ftl, host, module_name, params, result)

        except Exception as e:
            state[obs["name"]] = {"error": str(e)}
    return state


def _persist_host_facts(ftl, hostname: str, module: str, params: dict, result: dict):
    """Extract and persist OS facts from observation results to the state file.

    When setup or os-release observations run on a host, save key facts
    (os_family, distribution, pkg_manager) so future runs don't need to
    re-discover them.
    """
    facts = {}

    # setup module returns ansible_facts with system, machine, etc.
    if module == "setup":
        af = result.get("ansible_facts", {})
        if af.get("system"):
            facts["system"] = af["system"]
        if af.get("machine"):
            facts["machine"] = af["machine"]

    # shell/command running cat /etc/os-release — parse the output
    if module in ("shell", "command"):
        cmd = params.get("cmd", "")
        stdout = result.get("stdout", "")
        if "os-release" in cmd and stdout:
            parsed = _parse_os_release(stdout)
            if parsed:
                facts.update(parsed)

    if facts and ftl.state.has_host(hostname):
        host_data = ftl.state.get_host(hostname) or {}
        existing_facts = host_data.get("facts", {})
        if facts != {k: existing_facts.get(k) for k in facts}:
            existing_facts.update(facts)
            # Re-add host with updated facts
            ftl.state.add_host(
                hostname,
                ansible_host=host_data.get("ansible_host"),
                ansible_user=host_data.get("ansible_user"),
                ansible_port=host_data.get("ansible_port", 22),
                groups=host_data.get("groups"),
                facts=existing_facts,
            )


def _parse_os_release(text: str) -> dict:
    """Parse /etc/os-release output into useful facts."""
    kv = {}
    for line in text.strip().splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            kv[key.strip()] = val.strip().strip('"')

    facts = {}
    if "ID" in kv:
        distro_id = kv["ID"].lower()
        facts["distribution"] = kv.get("PRETTY_NAME", kv.get("NAME", distro_id))
        facts["distribution_id"] = distro_id

        # Derive package manager from distro
        dnf_distros = {"fedora", "rhel", "centos", "rocky", "alma", "ol"}
        apt_distros = {"debian", "ubuntu", "mint", "pop"}
        if distro_id in dnf_distros:
            facts["pkg_manager"] = "dnf"
        elif distro_id in apt_distros:
            facts["pkg_manager"] = "apt"

    return facts


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

    Returns True if a rule handled the situation successfully.
    Returns False if no rule matched or the matching rule's action failed
    (so the AI gets a chance to handle it).
    """
    for rule in rules:
        try:
            if await rule["condition"](state):
                print(f"  Rule matched: {rule['name']}")
                if not dry_run:
                    errors_before = len(ftl.errors) if hasattr(ftl, 'errors') else 0
                    try:
                        await rule["action"](ftl)
                    except Exception as e:
                        print(f"  Rule {rule['name']} action failed: {e}")
                        print(f"  Falling through to AI...")
                        return False
                    # Check if any module failures occurred during the action
                    errors_after = len(ftl.errors) if hasattr(ftl, 'errors') else 0
                    if errors_after > errors_before:
                        print(f"  Rule {rule['name']} had module failures, falling through to AI...")
                        return False
                else:
                    print(f"  DRY RUN: would execute rule {rule['name']}")
                return True
        except Exception as e:
            print(f"  Rule {rule['name']} condition error: {e}")
    return False


async def find_matching_rule(rules: list[dict], state: dict) -> dict | None:
    """Find the first rule whose condition matches. Does not execute."""
    for rule in rules:
        try:
            if await rule["condition"](state):
                return rule
        except Exception as e:
            print(f"  Rule {rule['name']} condition error: {e}")
    return None


async def execute_rule(rule: dict, ftl, dry_run: bool = False) -> tuple[bool, str]:
    """Execute a single rule's action. Returns (success, detail)."""
    if dry_run:
        return True, "dry run"
    errors_before = len(ftl.errors) if hasattr(ftl, 'errors') else 0
    try:
        await rule["action"](ftl)
    except Exception as e:
        return False, str(e)
    errors_after = len(ftl.errors) if hasattr(ftl, 'errors') else 0
    if errors_after > errors_before:
        return False, f"{errors_after - errors_before} module failure(s)"
    return True, "ok"


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
                 history: list[dict], user_answers: list[dict] | None = None,
                 rule_results: list[dict] | None = None,
                 iteration: int = 0, max_iterations: int = 10) -> str:
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

    answers_summary = ""
    if user_answers:
        entries = []
        for a in user_answers:
            entries.append(f"  Q: {a['question']}\n  A: {a['answer']}")
        answers_summary = f"\nUser answers to your previous questions:\n" + "\n".join(entries) + "\n"

    rule_results_summary = ""
    if rule_results:
        entries = []
        for r in rule_results:
            if r.get("denied"):
                entries.append(f"  Rule \"{r['rule']}\" matched but was DENIED by AI review: {r.get('reasoning', '')}")
            elif r.get("success"):
                entries.append(f"  Rule \"{r['rule']}\" fired: {r.get('detail', 'ok')}")
            else:
                entries.append(f"  Rule \"{r['rule']}\" fired but FAILED: {r.get('detail', 'unknown error')}")
        rule_results_summary = f"\nRule execution results from previous iterations:\n" + "\n".join(entries) + "\n"

    state_json = json.dumps(current_state, indent=2, default=str)

    return textwrap.dedent(f"""\
        You are an infrastructure reconciliation AI. You observe the current state
        of a system and decide what FTL2 module calls to make to achieve the desired state.

        FTL2 uses Ansible modules with the same names and parameters. You MUST only use
        FTL2 modules to take actions — never use curl, pip, or other CLI tools to work
        around modules. If a module exists for the task, use it.

        Action format (for the "actions" list):
        {{"module": "file", "params": {{"path": "/tmp/test", "state": "directory"}}}}
        {{"module": "copy", "params": {{"src": "/tmp/source.txt", "dest": "/tmp/dest.txt"}}}}
        {{"module": "shell", "params": {{"cmd": "echo 'hello world' > /tmp/hello.txt"}}}}
        {{"module": "command", "params": {{"cmd": "echo hello"}}}}
        {{"module": "stat", "params": {{"path": "/etc/nginx/nginx.conf"}}}}
        {{"module": "community.general.linode_v4", "params": {{"label": "myserver", "type": "g6-nanode-1", "region": "us-east", "image": "linode/ubuntu22.04", "state": "present"}}}}

        Host targeting — to run a module on a remote host instead of localhost, add "host":
        {{"host": "web01", "module": "dnf", "params": {{"name": "nginx", "state": "present"}}}}
        {{"host": "web01", "module": "service", "params": {{"name": "nginx", "state": "started"}}}}
        {{"host": "web01", "module": "shell", "params": {{"cmd": "cat /etc/os-release"}}}}
        Without "host", the module runs on the local controller (localhost).
        The host must be registered first via add_host in state_ops (see below).
        Do NOT use "ssh user@host 'command'" via shell — use host targeting instead.

        Platform-aware module usage:
        - Package managers: use "dnf" on RedHat/Fedora, "apt" on Debian/Ubuntu,
          "community.general.homebrew" on macOS. Check the OS from observations first.
        - Services: use "service" on Linux (systemd). On macOS there is no service module —
          use "command" or "shell" with launchctl if needed.
        - The "copy" module supports a "content" parameter for writing text to files. Prefer
          copy over shell for file content — it is idempotent (won't report changed if content matches).
          Example: {{"module": "copy", "params": {{"content": "<h1>Hello</h1>", "dest": "/var/www/html/index.html"}}}}
        - For remote hosts (Linux servers), use host targeting (the "host" field in actions)
          with dnf/apt/service — they work normally on the remote host.
        - The controller machine may be macOS while managed hosts are Linux. Use observations
          to determine the target platform before choosing modules.
        - CRITICAL: The shell/command modules BLOCK until the process exits. To start
          background/daemon processes, you MUST fully detach them so the shell returns:
          {{"module": "shell", "params": {{"cmd": "setsid python3 -m http.server 8000 > /tmp/server.log 2>&1 < /dev/null &"}}}}
          Using just "nohup ... &" is NOT enough — the module will still hang.

        IMPORTANT: Use fully qualified collection names (FQCN) for non-builtin modules:
        - community.general.linode_v4 (not linode_v4)
        - community.general.slack (not slack)
        - community.general.homebrew (not homebrew)
        - community.postgresql.postgresql_db (not postgresql_db)
        - ansible.posix.firewalld (not firewalld)

        Secrets (API tokens, passwords) are injected automatically via secret_bindings.
        Do NOT read secrets from environment variables or pass them as parameters.
        Just call the module — the secret is injected by the framework.

        State and host management: If "_state_file" appears in the current state, it shows
        resources and hosts from previous runs. Use "state_ops" to manage them:
        "state_ops": [
          {{"op": "add_resource", "name": "hello-ai", "data": {{"provider": "linode", "label": "hello-ai", "ipv4": ["1.2.3.4"]}}}},
          {{"op": "add_host", "name": "hello-ai", "ansible_host": "1.2.3.4", "ansible_user": "root", "groups": ["webservers"]}},
          {{"op": "remove", "name": "old-server"}}
        ]
        IMPORTANT: "add_host" registers the host in the LIVE INVENTORY so you can target
        it with "host" in subsequent actions. After creating a server (e.g., via linode_v4),
        you MUST add_host with its IP before you can run modules on it. Example workflow:
        1. Action: create server via community.general.linode_v4 → get IP from result
        2. State op: add_host with the IP and ansible_user
        3. Next iteration: use {{"host": "hello-ai", "module": "dnf", ...}} to run on it
        ALWAYS specify "ansible_user" in add_host (usually "root" for cloud servers).
        Without it, FTL2 defaults to the local username which will fail on remote hosts.
        You can re-register a host to fix parameters — add_host overwrites existing entries.
        If SSH authentication fails, re-register the host with the correct ansible_user
        before retrying module calls. Do NOT keep retrying the same failing module.
        Check "_state_file" before creating resources to avoid duplicates.
        Hosts in "_state_file" may include a "facts" field with cached OS information
        (distribution, pkg_manager, system, machine). Use these facts to choose the
        right package manager and avoid running setup/os-release checks unnecessarily.

        Efficiency: You can submit "observe" and "actions" in the SAME response. When
        the state file tells you a host exists (and especially when it has cached facts),
        act immediately — don't waste an iteration on pure observation. Request
        observations to verify results, and include actions for what you already know
        needs to happen. For example, if the state file shows a Fedora host and the
        desired state mentions nginx, submit both a verification observation AND the
        dnf/service/copy actions in iteration 0.

        Current state:
        {state_json}
        {rules_summary}{history_summary}{answers_summary}{rule_results_summary}
        Iteration budget: {iteration + 1} of {max_iterations} ({"use remaining iterations wisely" if iteration >= max_iterations // 2 else "early iterations, gather information as needed"})

        Desired state: {desired_state}

        Respond with ONLY a JSON object (no markdown, no explanation outside the JSON):
        {{
          "converged": true/false,
          "reasoning": "brief explanation of what you see and what needs to change",
          "actions": [
            {{"module": "module_name", "params": {{"key": "value"}}}},
            {{"host": "hostname", "module": "module_name", "params": {{"key": "value"}}}}
          ],
          "observe": [
            {{"name": "label", "module": "module_name", "params": {{"key": "value"}}}},
            {{"name": "label", "host": "hostname", "module": "module_name", "params": {{"key": "value"}}}}
          ],
          "rule": {{
            "name": "snake_case_name",
            "condition": "when this is true",
            "description": "what this rule does",
            "code": "async def condition(state):\\n    ...\\nasync def action(ftl):\\n    ..."
          }},
          "ask": {{
            "question": "Which web server should I install?",
            "options": ["nginx", "apache", "caddy"]
          }}
        }}

        Notes:
        - Set "converged" to true ONLY if the desired state is verified as achieved.
          Do not assume convergence from the existence of unrelated infrastructure.
        - "actions" is the list of module calls to make now. Empty if converged.
        - "observe" is optional: additional observations to make next iteration
          (to gather state you need but don't have yet). Use the same format as actions.
          Include "host" to observe a remote host instead of localhost. Without "host",
          observations run on the local controller. When checking state on a remote server,
          you MUST use "host" — otherwise you're checking localhost.
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
        - "ask" is optional: use it when you need information from the user before
          proceeding. The loop will pause, show your question, and feed the answer back
          to you on the next iteration. Use this when:
          - The desired state is ambiguous (e.g., "set up a web server" — which one?)
          - You need information you can't observe (credentials, domain names, preferences)
          - You want to confirm before a destructive action (deleting data, overwriting config)
          - You're stuck after multiple failed attempts and need guidance
          - There are multiple valid approaches and the user should choose
          "options" is optional — omit it for free-form questions. When present, the user
          can pick a numbered option or type a custom answer.
          When you use "ask", set "actions" to [] — don't act and ask in the same response.
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
                 history: list[dict], user_answers: list[dict] | None = None,
                 rule_results: list[dict] | None = None,
                 iteration: int = 0, max_iterations: int = 10) -> dict:
    """Ask the AI what to do via claude -p."""
    prompt = build_prompt(current_state, desired_state, rules, history, user_answers,
                          rule_results, iteration, max_iterations)

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


# --- Dev Mode: Rule Review ---


def build_review_prompt(rule: dict, current_state: dict, desired_state: str) -> str:
    """Build prompt for the AI to review a rule before it fires."""
    state_json = json.dumps(current_state, indent=2, default=str)

    rule_source = ""
    if rule.get("path"):
        try:
            rule_source = Path(rule["path"]).read_text()
        except Exception:
            rule_source = "(could not read source)"

    return textwrap.dedent(f"""\
        You are reviewing a rule that is about to fire in an infrastructure reconciliation loop.
        The rule's condition matched the current state. You must decide whether to approve it.

        Rule name: {rule['name']}
        Rule source:
        ```python
        {rule_source}
        ```

        Current state:
        {state_json}

        Desired state: {desired_state}

        Review the rule and decide:
        - Is the condition correct, or is it matching spuriously (e.g., referencing a
          nonexistent key that defaults to a truthy/falsy value)?
        - Will the action move the system toward the desired state?
        - Is the action targeting the right host (local vs remote)?
        - Are the module calls correct (right module name, right parameters)?
        - Could this action cause harm (data loss, service disruption)?

        Respond with ONLY a JSON object:
        {{
          "approve": true/false,
          "reasoning": "brief explanation of your decision"
        }}
    """)


async def review_rule(rule: dict, current_state: dict, desired_state: str) -> dict:
    """Ask the AI to review a rule before it fires. Returns approve/deny decision."""
    prompt = build_review_prompt(rule, current_state, desired_state)

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        # If review fails, approve by default so we don't block on AI errors
        return {"approve": True, "reasoning": "AI review unavailable, defaulting to approve"}

    raw = stdout.decode().strip()
    try:
        return json.loads(extract_json(raw))
    except json.JSONDecodeError:
        return {"approve": True, "reasoning": "Could not parse review response, defaulting to approve"}


# --- Ask User ---


def ask_user(ask_data: dict) -> str:
    """Prompt the user for input and return their answer."""
    question = ask_data["question"]
    options = ask_data.get("options", [])

    print(f"\n  AI asks: {question}")
    if options:
        for j, opt in enumerate(options, 1):
            print(f"    {j}. {opt}")
        print(f"    Or type a custom answer.")

    try:
        answer = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
        print()

    # If they picked a number and there are options, resolve it
    if options and answer.isdigit():
        idx = int(answer) - 1
        if 0 <= idx < len(options):
            answer = options[idx]

    if not answer:
        answer = "(no answer)"

    print(f"  Answer: {answer}")
    return answer


# --- Execute ---


async def execute(ftl, actions: list[dict], dry_run: bool = False) -> list[dict]:
    """Execute the decided actions via FTL2 modules."""
    results = []
    for action in actions:
        module_name = action["module"]
        params = action.get("params", {})
        host = action.get("host")

        if host:
            print(f"  → {host}: {module_name}({', '.join(f'{k}={v!r}' for k, v in params.items())})")
        else:
            print(f"  → {module_name}({', '.join(f'{k}={v!r}' for k, v in params.items())})")

        if dry_run:
            print(f"    DRY RUN: skipped")
            results.append({"module": module_name, "host": host, "result": {"dry_run": True}})
            continue

        try:
            # Start from the host proxy if targeting a remote host
            target = getattr(ftl, host) if host else ftl
            module_fn = target
            for part in module_name.split("."):
                module_fn = getattr(module_fn, part)
            result = await module_fn(**params)
            changed = result.get("changed", False) if isinstance(result, dict) else False
            print(f"    ok (changed={changed})")
            results.append({"module": module_name, "host": host, "result": result})
        except Exception as e:
            print(f"    FAILED: {e}")
            results.append({"module": module_name, "host": host, "result": {"error": str(e)}})

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
    dev: bool = False,
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
        user_answers: list[dict] = []
        rule_results: list[dict] = []
        consecutive_rule_runs = 0

        print(f"Desired state: {desired_state}")
        print(f"Rules loaded: {len(rules)}")
        print(f"Max iterations: {max_iterations}")
        if dry_run:
            print("DRY RUN — actions will not be executed")
        if dev:
            print("DEV MODE — AI reviews rules before they fire")
        print()

        for i in range(max_iterations):
            print(f"=== Iteration {i + 1} ===")

            # Observe
            print("Observing...")
            all_observers = observers + extra_observers
            current_state = await observe(ftl, all_observers)

            # Include state file contents so the AI knows what resources exist
            if hasattr(ftl, 'state') and ftl.state:
                try:
                    state_contents = {}
                    resources = ftl.state.resources()
                    if resources:
                        state_contents["resources"] = resources
                    hosts = ftl.state.hosts()
                    if hosts:
                        state_contents["hosts"] = {
                            name: ftl.state.get_host(name) for name in hosts
                        }
                    if state_contents:
                        current_state["_state_file"] = state_contents
                except Exception:
                    pass

            # Check rules first, but don't let rules loop forever.
            # If a rule handled the last iteration too, skip rules and
            # let the AI check for convergence.
            print("Checking rules...")
            if dev:
                # Dev mode: AI reviews rules before they fire and sees results after
                matched = await find_matching_rule(rules, current_state) if consecutive_rule_runs < 1 else None
                if matched:
                    print(f"  Rule matched: {matched['name']}")
                    print("  Reviewing rule...")
                    review = await review_rule(matched, current_state, desired_state)
                    review_reasoning = review.get("reasoning", "")
                    if review_reasoning:
                        print(f"  Review: {review_reasoning}")

                    if review.get("approve"):
                        print(f"  Approved — executing rule {matched['name']}")
                        success, detail = await execute_rule(matched, ftl, dry_run)
                        print(f"  Result: {detail}")
                        rule_results.append({
                            "rule": matched["name"],
                            "success": success,
                            "detail": detail,
                        })
                        if success:
                            consecutive_rule_runs += 1
                            extra_observers = []
                            print()
                            continue
                        print(f"  Rule failed, falling through to AI...")
                    else:
                        print(f"  Denied — skipping rule {matched['name']}")
                        rule_results.append({
                            "rule": matched["name"],
                            "denied": True,
                            "reasoning": review_reasoning,
                        })
                consecutive_rule_runs = 0
            else:
                # Normal mode: rules fire without AI review
                if consecutive_rule_runs < 1 and await check_rules(rules, current_state, ftl, dry_run):
                    print("Rule handled the situation.\n")
                    extra_observers = []
                    consecutive_rule_runs += 1
                    continue
                consecutive_rule_runs = 0

            # Decide
            print("Asking AI...")
            decision = await decide(current_state, desired_state, rules, history, user_answers,
                                    rule_results, i, max_iterations)

            reasoning = decision.get("reasoning", "")
            if reasoning:
                print(f"  Reasoning: {reasoning}")

            if decision.get("converged"):
                print(f"\nConverged after {i + 1} iteration(s).")
                history.append({
                    "iteration": i,
                    "reasoning": reasoning,
                    "converged": True,
                    "actions": [],
                    "results": [],
                })
                await post_convergence_review(
                    desired_state, history, i + 1, user_answers, rule_results,
                )
                return True

            # Ask the user a question if the AI needs input
            ask_data = decision.get("ask")
            if ask_data and ask_data.get("question"):
                answer = ask_user(ask_data)
                user_answers.append({
                    "question": ask_data["question"],
                    "answer": answer,
                })
                history.append({
                    "iteration": i,
                    "reasoning": reasoning,
                    "asked": ask_data["question"],
                    "actions": [],
                    "results": [],
                })
                print()
                continue

            # Pick up any additional observers the AI requested
            extra_observers = decision.get("observe", [])
            if extra_observers:
                print(f"  AI requested {len(extra_observers)} additional observation(s)")

            # Execute
            actions = decision.get("actions", [])
            if not actions:
                history.append({
                    "iteration": i,
                    "reasoning": reasoning,
                    "actions": [],
                    "results": [],
                    "observations_requested": len(extra_observers),
                })
                print("  No actions decided.\n")
                continue

            print(f"Executing {len(actions)} action(s)...")
            results = await execute(ftl, actions, dry_run)
            history.append({
                "iteration": i,
                "reasoning": reasoning,
                "actions": actions,
                "results": results,
            })

            # State operations
            state_ops = decision.get("state_ops", [])
            if state_ops:
                for op in state_ops:
                    op_type = op.get("op")
                    name = op.get("name", "")
                    if not dry_run:
                        try:
                            if op_type == "add_resource":
                                if hasattr(ftl, 'state') and ftl.state:
                                    ftl.state.add_resource(name, op.get("data", {}))
                                print(f"  State: added resource {name}")
                            elif op_type == "add_host":
                                # Use ftl.add_host() which registers in live
                                # inventory AND persists to state file
                                ftl.add_host(
                                    hostname=name,
                                    ansible_host=op.get("ansible_host"),
                                    ansible_user=op.get("ansible_user", "root"),
                                    groups=op.get("groups"),
                                )
                                print(f"  Host added: {name} ({op.get('ansible_host', name)})")
                            elif op_type == "remove":
                                if hasattr(ftl, 'state') and ftl.state:
                                    ftl.state.remove(name)
                                print(f"  State: removed {name}")
                        except Exception as e:
                            print(f"  State op failed: {e}")
                    else:
                        print(f"  DRY RUN: would {op_type} {name}")

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


# --- Post-Convergence Review ---


async def post_convergence_review(
    desired_state: str,
    history: list[dict],
    iterations: int,
    user_answers: list[dict],
    rule_results: list[dict],
):
    """Ask the AI to review its own performance and suggest feature requests."""
    history_json = json.dumps(history, indent=2, default=str)
    answers_json = json.dumps(user_answers, indent=2) if user_answers else "None"
    rules_json = json.dumps(rule_results, indent=2) if rule_results else "None"

    prompt = textwrap.dedent(f"""\
        You just completed an infrastructure reconciliation run. Review your performance
        and suggest improvements to the tool.

        Desired state: {desired_state}
        Iterations to converge: {iterations}
        Action history: {history_json}
        User questions asked: {answers_json}
        Rule results: {rules_json}

        Please provide:

        1. PERFORMANCE REVIEW — Be honest and specific:
           - What went well?
           - What was inefficient? (unnecessary iterations, wrong approaches, wasted steps)
           - Did you make mistakes? What caused them?
           - Could you have converged faster? How?

        2. FEATURE REQUESTS — What changes to ftl2-ai-loop would make your job easier?
           Think about what frustrated you, what information you were missing, what
           capabilities you wished you had. Be specific and practical.

        Write your response as plain text for the user to read. Be concise and direct.
    """)

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        return

    review = stdout.decode().strip()
    if review:
        print(f"\n--- AI Self-Review ---")
        print(review)
        print(f"--- End Review ---\n")


# --- Continuous Mode ---


async def run_continuous(reconcile_kwargs: dict, delay: int):
    """Run the reconciliation loop continuously with a delay between runs."""
    run_count = 0
    print(f"Continuous mode: reconciling every {delay}s (Ctrl+C to stop)\n")
    try:
        while True:
            run_count += 1
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"{'=' * 60}")
            print(f"Run #{run_count} — {timestamp}")
            print(f"{'=' * 60}")

            try:
                converged = await reconcile(**reconcile_kwargs)
                status = "converged" if converged else "did not converge"
            except Exception as e:
                print(f"\nRun #{run_count} failed: {e}")
                status = "error"

            print(f"\nRun #{run_count} {status}. Next run in {delay}s...\n")
            await asyncio.sleep(delay)
    except KeyboardInterrupt:
        print(f"\nStopped after {run_count} run(s).")


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
    parser.add_argument("-s", "--secret", action="append", default=[], metavar="MODULE.PARAM=ENV_VAR",
                        help="Bind a secret: community.general.linode_v4.access_token=LINODE_TOKEN")
    parser.add_argument("--state-file", help="FTL2 state file for tracking resources")
    parser.add_argument("--dev", action="store_true",
                        help="Dev mode: AI reviews rules before they fire and sees results after")
    parser.add_argument("--continuous", action="store_true",
                        help="Run continuously, re-reconciling after each delay period")
    parser.add_argument("--delay", type=int, default=60,
                        help="Seconds between reconciliation runs in continuous mode (default: 60)")
    args = parser.parse_args()

    # Parse secret bindings: "module.param=ENV_VAR" → {"module": {"param": "ENV_VAR"}}
    secret_bindings: dict[str, dict[str, str]] = {}
    for binding in args.secret:
        if "=" not in binding:
            parser.error(f"Invalid secret binding: {binding!r} (expected MODULE.PARAM=ENV_VAR)")
        module_param, env_var = binding.rsplit("=", 1)
        if "." not in module_param:
            parser.error(f"Invalid secret binding: {binding!r} (MODULE.PARAM must contain a dot)")
        # Split on last dot to get module pattern and param name
        module_pattern, param = module_param.rsplit(".", 1)
        if module_pattern not in secret_bindings:
            secret_bindings[module_pattern] = {}
        secret_bindings[module_pattern][param] = env_var

    reconcile_kwargs = dict(
        desired_state=args.desired_state,
        inventory=args.inventory,
        max_iterations=args.max_iterations,
        rules_dir=args.rules_dir,
        dry_run=args.dry_run,
        quiet=args.quiet,
        secret_bindings=secret_bindings or None,
        state_file=args.state_file,
        dev=args.dev,
    )

    if args.continuous:
        asyncio.run(run_continuous(reconcile_kwargs, args.delay))
    else:
        converged = asyncio.run(reconcile(**reconcile_kwargs))
        sys.exit(0 if converged else 1)


if __name__ == "__main__":
    cli()
