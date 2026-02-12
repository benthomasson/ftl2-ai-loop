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
import os
import re
import sys
import textwrap
import traceback
from datetime import datetime, timezone
from pathlib import Path

from ftl2 import automation

REPO_URL = "https://github.com/benthomasson/ftl2-ai-loop"
UPDATE_EXIT_CODE = 42

# Global prompt log directory — set by CLI --prompt-log flag
_prompt_log_dir: Path | None = None
_prompt_log_counter = 0


def _log_prompt(label: str, prompt: str, response: str) -> None:
    """Write a prompt/response pair to the prompt log directory."""
    global _prompt_log_counter
    if not _prompt_log_dir:
        return
    try:
        _prompt_log_dir.mkdir(parents=True, exist_ok=True)
        _prompt_log_counter += 1
        n = _prompt_log_counter
        _prompt_log_dir.joinpath(f"{n:03d}-{label}-prompt.txt").write_text(prompt)
        _prompt_log_dir.joinpath(f"{n:03d}-{label}-response.txt").write_text(response)
    except Exception as e:
        print(f"  Failed to write prompt log: {e}")


def _format_duration(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins}m" if mins else f"{hours}h"


# --- Version Check ---


def _get_startup_commit() -> str | None:
    """Get the git commit hash this package was installed from."""
    try:
        from importlib.metadata import metadata
        # uvx installs store the source URL in metadata
        meta = metadata("ftl2-ai-loop")
        # Check direct_url.json for the commit hash
        import json
        from importlib.resources import files
        import pathlib
        # Find the dist-info directory
        import importlib.metadata
        dist = importlib.metadata.distribution("ftl2-ai-loop")
        direct_url_path = dist._path.parent / dist._path.name / "direct_url.json"
        if not direct_url_path.exists():
            # Try alternative path
            for f in dist.files or []:
                if f.name == "direct_url.json":
                    direct_url_path = pathlib.Path(f.locate())
                    break
        if direct_url_path.exists():
            data = json.loads(direct_url_path.read_text())
            return data.get("vcs_info", {}).get("commit_id")
    except Exception:
        pass
    return None


async def _get_latest_commit() -> str | None:
    """Check the latest commit on the remote repo via git ls-remote."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "ls-remote", REPO_URL, "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            # Format: "<hash>\tHEAD\n"
            return stdout.decode().split()[0]
    except Exception:
        pass
    return None


async def _check_for_update(startup_commit: str | None) -> bool:
    """Return True if a newer version is available."""
    if not startup_commit:
        return False
    latest = await _get_latest_commit()
    if not latest:
        return False
    if latest != startup_commit:
        print(f"\nNew version available: {startup_commit[:8]} → {latest[:8]}")
        print(f"Exiting for update (exit code {UPDATE_EXIT_CODE})...")
        return True
    return False


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
            # Host-targeted modules return a list (one result per host).
            # Unwrap single-element lists so rules see a dict, not [dict].
            if isinstance(result, list) and len(result) == 1:
                result = result[0]
            # ExecuteResult objects need to be converted to their output dict
            # so rules can use .get() on them.
            if hasattr(result, 'output') and not isinstance(result, dict):
                result = result.output
            state[obs["name"]] = result

            # Persist OS facts to state file when we learn them
            if host and hasattr(ftl, 'state') and ftl.state:
                _persist_host_facts(ftl, host, module_name, params, result)

        except Exception as e:
            state[obs["name"]] = {"error": str(e)}
            traceback.print_exc()
    return state


def _persist_host_facts(ftl, hostname: str, module: str, params: dict, result: dict):
    """Extract and persist OS facts from observation results to the state file.

    When setup or os-release observations run on a host, save key facts
    (os_family, distribution, pkg_manager) so future runs don't need to
    re-discover them.
    """
    if not isinstance(result, dict):
        return

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

    # Load config to find disabled/broken rules
    config_file = rules_path / "rules.json"
    config = {}
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    disabled = set(config.get("disabled", []))
    broken = config.get("broken", {})
    skip = disabled | set(broken)

    rules = []
    config_changed = False
    for rule_file in sorted(rules_path.glob("*.py")):
        if rule_file.stem in skip:
            continue
        try:
            spec = importlib.util.spec_from_file_location(rule_file.stem, rule_file)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if hasattr(module, "condition") and hasattr(module, "action"):
                    rules.append({
                        "name": rule_file.stem,
                        "condition": module.condition,
                        "action": module.action,
                        "observe": getattr(module, "observe", []),
                        "doc": module.__doc__ or "",
                        "path": str(rule_file),
                    })
        except Exception as e:
            print(f"  Warning: skipping broken rule {rule_file.stem}: {e}")
            broken[rule_file.stem] = str(e)
            config_changed = True

    if config_changed:
        config["broken"] = broken
        try:
            config_file.write_text(json.dumps(config, indent=2) + "\n")
        except Exception:
            pass

    return rules


async def _run_rule_observations(rule: dict, ftl, state: dict) -> dict:
    """Run a rule's declared observations and merge results into state.

    Rules can declare an 'observe' list of observations that must be
    collected before the condition is evaluated. This makes rules
    self-contained — they don't depend on the AI having run specific
    observations in a previous iteration.
    """
    rule_obs = rule.get("observe", [])
    if not rule_obs:
        return state
    print(f"  Running {len(rule_obs)} rule observation(s) for {rule['name']}...")
    for obs in rule_obs:
        host = obs.get("host")
        if host:
            print(f"    → {host}: {obs['module']}({', '.join(f'{k}={v!r}' for k, v in obs.get('params', {}).items())}) as '{obs['name']}'")
        else:
            print(f"    → {obs['module']}({', '.join(f'{k}={v!r}' for k, v in obs.get('params', {}).items())}) as '{obs['name']}'")
    obs_state = await observe(ftl, rule_obs)
    merged = {**state, **obs_state}
    return merged


async def check_rules(rules: list[dict], state: dict, ftl, dry_run: bool = False) -> tuple[bool, dict]:
    """Check if any rule matches the current state and execute it.

    Returns (True, rule_obs) if a rule handled the situation successfully.
    Returns (False, rule_obs) if no rule matched or the matching rule's action failed
    (so the AI gets a chance to handle it).

    rule_obs contains any observations collected by rules during evaluation,
    so callers can merge them into the current state for the AI to see.
    """
    all_rule_obs = {}
    for rule in rules:
        try:
            eval_state = await _run_rule_observations(rule, ftl, state)
            # Collect rule observations (keys added beyond original state)
            for k, v in eval_state.items():
                if k not in state:
                    all_rule_obs[k] = v
            if await rule["condition"](eval_state):
                print(f"  Rule matched: {rule['name']}")
                if not dry_run:
                    errors_before = len(ftl.errors) if hasattr(ftl, 'errors') else 0
                    try:
                        await rule["action"](ftl)
                    except Exception as e:
                        print(f"  Rule {rule['name']} action failed: {e}")
                        traceback.print_exc()
                        print(f"  Falling through to AI...")
                        return False, all_rule_obs
                    # Check if any module failures occurred during the action
                    errors_after = len(ftl.errors) if hasattr(ftl, 'errors') else 0
                    if errors_after > errors_before:
                        print(f"  Rule {rule['name']} had module failures, falling through to AI...")
                        return False, all_rule_obs
                else:
                    print(f"  DRY RUN: would execute rule {rule['name']}")
                return True, all_rule_obs
        except Exception as e:
            print(f"  Rule {rule['name']} condition error: {e}")
            traceback.print_exc()
    return False, all_rule_obs


async def find_matching_rule(rules: list[dict], state: dict, ftl) -> dict | None:
    """Find the first rule whose condition matches. Does not execute.

    Runs each rule's declared observations before evaluating its condition.
    """
    for rule in rules:
        try:
            eval_state = await _run_rule_observations(rule, ftl, state)
            if await rule["condition"](eval_state):
                return rule
        except Exception as e:
            print(f"  Rule {rule['name']} condition error: {e}")
            traceback.print_exc()
    return None


async def execute_rule(rule: dict, ftl, dry_run: bool = False) -> tuple[bool, str]:
    """Execute a single rule's action. Returns (success, detail)."""
    if dry_run:
        return True, "dry run"
    errors_before = len(ftl.errors) if hasattr(ftl, 'errors') else 0
    try:
        await rule["action"](ftl)
    except Exception as e:
        traceback.print_exc()
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

    # Write observation dependencies before the code
    observe_section = ""
    observations = rule_data.get("observe", [])
    if observations:
        observe_section = f"observe = {json.dumps(observations, indent=4)}\n\n"

    rule_file.write_text(header + observe_section + code)
    print(f"  Rule saved: {rule_file}")
    return rule_file


# --- Decide (LLM) ---


def _convergence_hint(history: list[dict]) -> str:
    """Hint the AI to converge if the last iteration's actions all returned changed=false."""
    if not history:
        return ""
    last = history[-1]
    actions = last.get("actions", [])
    results = last.get("results", [])
    if not actions or not results:
        return ""
    try:
        # Classify results
        changed_count = 0
        unchanged_count = 0
        failed_count = 0
        for r in results:
            if not isinstance(r, dict) or not isinstance(r.get("result"), dict):
                continue
            res = r["result"]
            if res.get("failed"):
                failed_count += 1
            elif res.get("changed"):
                changed_count += 1
            else:
                unchanged_count += 1

        if failed_count > 0:
            return ""

        total = changed_count + unchanged_count
        if total == 0:
            return ""

        if changed_count == 0:
            return (f"CONVERGENCE HINT: All {total} action(s) last iteration returned "
                    f"changed=false — the system is already in the desired state. "
                    f"You should CONVERGE now.")
        else:
            return (f"CONVERGENCE HINT: All {total} action(s) last iteration SUCCEEDED "
                    f"({changed_count} changed, {unchanged_count} unchanged, 0 failed). "
                    f"The desired state has been applied. You should CONVERGE now "
                    f"unless you need to verify something the modules cannot check.")
    except Exception:
        pass
    return ""


def _iteration_summary(h: dict) -> str:
    """Compact one-line summary of an iteration's results."""
    if h.get("converged"):
        return "CONVERGED"
    if h.get("asked"):
        return f"asked user: {h['asked']}"
    results = h.get("results", [])
    if not results:
        obs = h.get("observations_requested", 0)
        return f"no actions{f', {obs} observations requested' if obs else ''}"
    changed = failed = ok = 0
    for r in results:
        if isinstance(r, dict) and isinstance(r.get("result"), dict):
            res = r["result"]
            if res.get("failed"):
                failed += 1
            elif res.get("changed"):
                changed += 1
            else:
                ok += 1
    return f"{changed} changed, {ok} ok, {failed} failed"


def _no_action_warning(history: list[dict]) -> str:
    """Warn the AI if the previous iteration(s) had no actions."""
    if not history:
        return ""
    consecutive = 0
    for entry in reversed(history):
        if not entry.get("actions"):
            consecutive += 1
        else:
            break
    if consecutive == 0:
        return ""
    if consecutive == 1:
        return ("WARNING: You took NO ACTIONS last iteration. You have observation "
                "results now — either act on them or converge. Do not request more "
                "observations without acting.")
    return (f"WARNING: You have taken NO ACTIONS for {consecutive} consecutive "
            f"iterations. You are wasting iterations on repeated observations. "
            f"ACT NOW or CONVERGE. If you cannot determine the right actions, "
            f"ask the user for help.")


def build_prompt(current_state: dict, desired_state: str, rules: list[dict],
                 history: list[dict], user_answers: list[dict] | None = None,
                 rule_results: list[dict] | None = None,
                 iteration: int = 0, max_iterations: int = 10,
                 prior_increments: list[dict] | None = None) -> str:
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
            # Compact summary line
            summary = _iteration_summary(h)
            entries.append(f"Iteration {h['iteration']}: {summary}\n  Actions: {actions_str}\n  Results: {results_str}")
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

    prior_increments_summary = ""
    if prior_increments:
        entries = []
        for inc in prior_increments:
            status = "converged" if inc.get("converged") else "did not converge"
            entries.append(f'  - Increment {inc["n"]}: "{inc["desired_state"]}" ({status})')
        prior_increments_summary = (
            f"\nPrevious work completed (incremental mode):\n"
            + "\n".join(entries)
            + "\n\nThe current increment builds on this previous work. Do NOT redo previous actions.\n"
        )

    state_json = json.dumps(current_state, indent=2, default=str)

    return textwrap.dedent(f"""\
        You are an infrastructure reconciliation AI. You observe the current state
        of a system and decide what FTL2 module calls to make to achieve the desired state.

        FTL2 uses Ansible modules with the same names and parameters. You MUST only use
        FTL2 modules to take actions — never use curl, pip, or other CLI tools to work
        around modules. If a module exists for the task, use it.

        Action format (for the "actions" list):
        {{"module": "file", "params": {{"path": "/tmp/test", "state": "directory"}}}}
        {{"module": "copy", "params": {{"src": "app.conf", "dest": "/etc/app/app.conf", "mode": "0644"}}}}
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
        - For remote hosts (Linux servers), use host targeting (the "host" field in actions)
          with dnf/apt/service — they work normally on the remote host.
        - The controller machine may be macOS while managed hosts are Linux. Use observations
          to determine the target platform before choosing modules.
        - The "wait_for" module polls a TCP port until it becomes reachable. Use this instead
          of "shell: sleep" when waiting for a server to boot. It returns as soon as the port is
          open, which is faster and more reliable than a fixed sleep.
          Example: {{"module": "wait_for", "params": {{"host": "192.168.1.10", "port": 22, "timeout": 180}}}}
          Parameters: host (str), port (int, required), timeout (int, default 300), delay (int, default 0),
          sleep (int, default 1), state ("started" or "stopped", default "started"), connect_timeout (int, default 5).
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

        FTL2 native modules — these run on the CONTROLLER, not the remote host. They
        use SFTP to transfer files and SSH to run commands. They differ from Ansible:
        - "copy" with "src": reads from the controller filesystem and transfers via SFTP.
          Use "src" for deploying local project files. Relative paths resolve from the
          working directory. Use "content" for inline text.
          {{"host": "web01", "module": "copy", "params": {{"src": "nginx.conf", "dest": "/etc/nginx/nginx.conf", "mode": "0644"}}}}
          {{"host": "web01", "module": "copy", "params": {{"content": "hello", "dest": "/tmp/hello.txt"}}}}
        - "template" with "src": renders a local Jinja2 template on the controller,
          then transfers the result via SFTP. Pass template variables as extra params.
          {{"host": "web01", "module": "template", "params": {{"src": "nginx.conf.j2", "dest": "/etc/nginx/nginx.conf"}}}}
        - "shell" and "command": run commands on the remote host via SSH directly
          (not via the module system). Use the "cmd" parameter.
        - "fetch": downloads a remote file to the controller via SFTP.
        These modules are idempotent — they check before acting and return changed=false
        if no change is needed. You do NOT need Ansible lookups, Jinja2 placeholders, or
        workarounds to read local files. Just use "src" to reference files in the project.

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
        NOTE: When a cloud provisioning module (e.g., linode_v4) returns an instance with
        a label and IP, the framework AUTOMATICALLY registers the host and waits for SSH
        (port 22, up to 180s). You do NOT need to issue a separate wait_for for SSH after
        provisioning — it has already been done. Proceed directly to configuring the host.
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

        Convergence: When idempotent modules (dnf, apt, copy, service, file) return
        "changed": false, they already verified the current state matches the desired
        state. You do NOT need additional observations to confirm — converge immediately.
        Only request verification observations for things modules can't check (e.g., a
        web page returning the right content via curl).

        Current state:
        {state_json}
        {rules_summary}{history_summary}{answers_summary}{rule_results_summary}{prior_increments_summary}
        Iteration budget: {iteration + 1} of {max_iterations} ({"use remaining iterations wisely" if iteration >= max_iterations // 2 else "early iterations, gather information as needed"})
        {_no_action_warning(history)}{_convergence_hint(history)}

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
          "ask": {{
            "question": "Which web server should I install?",
            "options": ["nginx", "apache", "caddy"]
          }}
        }}

        Notes:
        - Set "converged" to true ONLY if the desired state is verified as achieved.
          Do not assume convergence from the existence of unrelated infrastructure.
        - "actions" is the list of module calls to make now. Empty if converged.
        - "observe" is optional: additional observations to run. Use the same format as actions.
          Include "host" to observe a remote host instead of localhost. Without "host",
          observations run on the local controller. When checking state on a remote server,
          you MUST use "host" — otherwise you're checking localhost.
          You can include "observe" even when converged — those observations will run at
          the START of the next run, so the AI has data immediately on iteration 0. Use this
          to specify the checks needed to verify the desired state (e.g., nginx status,
          web page content) so the next run can converge in 1 iteration instead of wasting
          iteration 0 on observation.
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
                 iteration: int = 0, max_iterations: int = 10,
                 prior_increments: list[dict] | None = None) -> dict:
    """Ask the AI what to do via claude -p."""
    prompt = build_prompt(current_state, desired_state, rules, history, user_answers,
                          rule_results, iteration, max_iterations, prior_increments)

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await proc.communicate()
    except (KeyboardInterrupt, asyncio.CancelledError):
        proc.terminate()
        await proc.wait()
        raise KeyboardInterrupt

    raw = stdout.decode().strip()
    _log_prompt(f"decide-iter{iteration}", prompt, raw)

    if proc.returncode != 0:
        error = stderr.decode().strip()
        print(f"  AI error: {error}")
        return {"converged": False, "reasoning": f"AI call failed: {error}", "actions": []}

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


def build_planning_prompt(desired_state: str, user_answers: list[dict] | None = None) -> str:
    """Build prompt for the AI to analyze a desired state and split it into increments."""
    answers_summary = ""
    if user_answers:
        entries = []
        for a in user_answers:
            entries.append(f"  Q: {a['question']}\n  A: {a['answer']}")
        answers_summary = f"\nUser answers to your previous questions:\n" + "\n".join(entries) + "\n"

    return textwrap.dedent(f"""\
        You are a planning assistant for an infrastructure reconciliation system. Analyze the
        desired state below and break it into ordered increments that can each be independently
        verified and converged.

        Each increment should be a self-contained desired state description — a single sentence
        that the reconciliation AI can work toward. Increments execute in order, and each builds
        on the results of the previous ones.

        Guidelines:
        - For simple, single-concern tasks: return a single increment (do NOT split unnecessarily)
        - For complex, multi-step tasks: split into 2-5 ordered increments
        - Each increment must be independently verifiable (you can check if it succeeded)
        - Order increments by dependency (create before configure, configure before verify)
        - Be specific in each increment — include names, versions, paths where known

        You may also specify initial observations for the first increment. These are FTL2 module
        calls that gather state before the AI starts working. Use the same format as actions:
        {{"name": "label", "module": "module_name", "params": {{"key": "value"}}}}
        {{"name": "label", "host": "hostname", "module": "module_name", "params": {{"key": "value"}}}}
        Only include observations if they would genuinely help iteration 0 start faster.

        IMPORTANT: Use fully qualified collection names (FQCN) for non-builtin modules:
        - community.general.linode_v4 (not linode_v4)
        - community.general.homebrew (not homebrew)
        - community.postgresql.postgresql_db (not postgresql_db)
        - ansible.posix.firewalld (not firewalld)
        Builtin modules (command, shell, file, copy, stat, service, dnf, apt) do not need FQCN.

        Secrets (API tokens, passwords) are injected automatically by the framework. Do NOT
        pass credentials in observation params — no Jinja templates, no environment variables,
        no hardcoded tokens. Just call the module normally and the secret will be injected.

        Initial observations should only OBSERVE state, not create or modify resources. Do not
        use "state": "present" or other mutating parameters in observations.

        If the desired state is genuinely ambiguous and you need clarification before planning,
        you may ask up to 2 clarifying questions. Only ask when the ambiguity would change the
        plan structure (e.g., which provider, which OS, which approach). Do NOT ask questions
        for things you can reasonably assume or decide yourself.
        {answers_summary}
        Desired state: {desired_state}

        Respond with ONLY a JSON object (no markdown, no explanation):
        {{
          "increments": ["first desired state", "second desired state"],
          "initial_observations": [
            {{"name": "label", "module": "module_name", "params": {{"key": "value"}}}}
          ],
          "questions": [
            {{"question": "Which SSL provider?", "options": ["Let's Encrypt", "Self-signed"]}}
          ]
        }}

        Notes:
        - "increments" is required and must have at least 1 element
        - "initial_observations" is optional (omit or use empty list if not needed)
        - "questions" is optional — only include if you genuinely need clarification
        - If you include "questions", leave "increments" as your best guess (they will be
          re-planned after the user answers)
    """)


async def plan(desired_state: str, ask_user: "AskUserFunc | None" = None) -> dict:
    """Analyze a desired state and split it into increments via the AI.

    Args:
        desired_state: The desired state description
        ask_user: Callable for prompting the user. Defaults to ask_user_stdin.

    Returns a dict with:
        increments (list[str]): Ordered list of desired state descriptions
        initial_observations (list[dict]): Observations for the first increment
        user_answers (list[dict]): Any Q&A collected during planning
    """
    if ask_user is None:
        ask_user = ask_user_stdin
    user_answers: list[dict] = []
    max_rounds = 3

    for round_n in range(max_rounds):
        prompt = build_planning_prompt(desired_state, user_answers or None)

        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await proc.communicate()
        except (KeyboardInterrupt, asyncio.CancelledError):
            proc.terminate()
            await proc.wait()
            raise KeyboardInterrupt

        raw = stdout.decode().strip()
        _log_prompt(f"plan-round{round_n}", prompt, raw)

        if proc.returncode != 0:
            error = stderr.decode().strip()
            print(f"  Planning error: {error}")
            return {"increments": [desired_state], "initial_observations": [], "user_answers": []}

        try:
            result = json.loads(extract_json(raw))
        except (json.JSONDecodeError, ValueError):
            print(f"  Failed to parse planning response, using single increment.")
            return {"increments": [desired_state], "initial_observations": [], "user_answers": []}

        # Handle clarifying questions
        questions = result.get("questions", [])
        if questions and round_n < max_rounds - 1:
            for q in questions:
                answer = ask_user(q)
                user_answers.append({
                    "question": q["question"],
                    "answer": answer,
                })
            continue

        # Valid plan — return it
        increments = result.get("increments", [desired_state])
        if not increments:
            increments = [desired_state]
        initial_observations = result.get("initial_observations", [])

        return {
            "increments": increments,
            "initial_observations": initial_observations,
            "user_answers": user_answers,
        }

    # Exhausted question rounds — use whatever we got
    return {"increments": [desired_state], "initial_observations": [], "user_answers": user_answers}


async def review_rule(rule: dict, current_state: dict, desired_state: str) -> dict:
    """Ask the AI to review a rule before it fires. Returns approve/deny decision."""
    prompt = build_review_prompt(rule, current_state, desired_state)

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await proc.communicate()
    except (KeyboardInterrupt, asyncio.CancelledError):
        proc.terminate()
        await proc.wait()
        raise KeyboardInterrupt
    raw = stdout.decode().strip()
    _log_prompt("rule-review", prompt, raw)

    if proc.returncode != 0:
        # If review fails, approve by default so we don't block on AI errors
        return {"approve": True, "reasoning": "AI review unavailable, defaulting to approve"}

    try:
        return json.loads(extract_json(raw))
    except json.JSONDecodeError:
        return {"approve": True, "reasoning": "Could not parse review response, defaulting to approve"}


# --- Ask User ---

# Type alias for ask_user callables
AskUserFunc = "Callable[[dict], str]"


def ask_user_stdin(ask_data: dict) -> str:
    """Prompt the user for input via stdin and return their answer."""
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


def ask_user_noninteractive(ask_data: dict) -> str:
    """Non-interactive backend: auto-approve by selecting first option, or return no answer."""
    question = ask_data["question"]
    options = ask_data.get("options", [])
    print(f"\n  AI asks: {question}")
    if options:
        print(f"  (non-interactive mode, auto-selecting: {options[0]})")
        return options[0]
    print(f"  (non-interactive mode, skipping)")
    return "(no answer)"


def _slack_api(method: str, payload: dict, bot_token: str, use_get: bool = False) -> dict:
    """Call a Slack Web API method.

    Args:
        method: Slack API method name (e.g., "chat.postMessage").
        payload: Request payload dict.
        bot_token: Slack bot token for authorization.
        use_get: Use GET instead of POST (for read-only endpoints).
    """
    import urllib.request
    import urllib.parse

    headers = {"Authorization": f"Bearer {bot_token}"}
    if use_get:
        qs = urllib.parse.urlencode(payload)
        req = urllib.request.Request(
            f"https://slack.com/api/{method}?{qs}",
            headers=headers,
        )
    else:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"https://slack.com/api/{method}",
            data=data,
            headers=headers,
        )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(f"Slack API {method} failed: {result.get('error', result)}")
    return result


def _notify_slack(
    channel: str,
    bot_token: str,
    desired_state: str,
    converged: bool,
    iterations: int,
    actions_taken: int,
    duration: float,
    error: Exception | None = None,
    run_number: int | None = None,
    increments: list[dict] | None = None,
) -> None:
    """Post a run summary to Slack. Fire-and-forget — never raises."""
    try:
        if increments is not None:
            # Incremental summary
            total = len(increments)
            converged_count = sum(1 for i in increments if i.get("converged"))
            total_iters = sum(i.get("iterations", 0) for i in increments)
            if converged_count == total:
                emoji = ":white_check_mark:"
                status = f"*Incremental complete* — {converged_count}/{total} converged in {_format_duration(duration)}"
            else:
                emoji = ":warning:"
                status = f"*Incremental partial* — {converged_count}/{total} converged in {_format_duration(duration)}"
            lines = [f"{emoji} {status}", "", f"> {desired_state}", ""]
            for j, inc in enumerate(increments, 1):
                inc_emoji = ":white_check_mark:" if inc.get("converged") else ":x:"
                lines.append(f"{inc_emoji} {j}. {inc['desired_state']}")
            lines.append("")
            lines.append(f"{actions_taken} action(s) taken across {total_iters} iteration(s)")
            text = "\n".join(lines)
        elif error is not None:
            # Error
            run_label = f"Run #{run_number} — " if run_number else ""
            text = f":warning: *{run_label}Error*\n\n> {desired_state}\n\n{error}"
        elif converged:
            # Converged
            text = (
                f":white_check_mark: *Converged* after {iterations} iteration(s) in {_format_duration(duration)}\n\n"
                f"> {desired_state}\n\n"
                f"{actions_taken} action(s) taken"
            )
        else:
            # Did not converge
            text = (
                f":x: *Did not converge* after {iterations} iteration(s) in {_format_duration(duration)}\n\n"
                f"> {desired_state}\n\n"
                f"{actions_taken} action(s) taken"
            )

        _slack_api("chat.postMessage", {
            "channel": channel,
            "text": text,
        }, bot_token=bot_token)
    except Exception as e:
        print(f"  Warning: Slack notification failed: {e}")


def make_ask_user_slack(channel: str, token: str | None = None, poll_interval: int = 30, timeout: int = 0) -> "AskUserFunc":
    """Create a Slack ask_user backend.

    Posts questions to a Slack channel and polls for thread replies.
    Uses the Slack Web API with a bot token.

    Args:
        channel: Slack channel to post to (e.g., "#approvals" or "C01234ABCDE")
        token: Slack bot token. Defaults to SLACK_BOT_TOKEN env var.
        poll_interval: Seconds between polling for replies (default: 30)
        timeout: Max seconds to wait for a reply (0 = no timeout, default: 0)

    Returns:
        A callable suitable for use as an ask_user backend.
    """
    import time

    bot_token = token or os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        raise ValueError(
            "Slack bot token required. Set SLACK_BOT_TOKEN env var "
            "or pass token= to make_ask_user_slack()."
        )

    def ask_user_slack(ask_data: dict) -> str:
        """Post a question to Slack and poll for a thread reply."""
        question = ask_data["question"]
        options = ask_data.get("options", [])

        # Build message text
        text = f":question: *Approval needed*\n\n{question}"
        if options:
            text += "\n\nOptions:"
            for j, opt in enumerate(options, 1):
                text += f"\n  {j}. {opt}"
            text += "\n\nReply with a number or a custom answer."
        else:
            text += "\n\nReply in this thread to answer."

        # Post the question
        print(f"\n  AI asks (via Slack {channel}): {question}")
        result = _slack_api("chat.postMessage", {
            "channel": channel,
            "text": text,
        }, bot_token=bot_token)
        thread_ts = result["ts"]
        post_channel = result["channel"]  # resolved channel ID
        print(f"  Waiting for reply in Slack...")

        # Poll for thread replies
        start = time.time()
        while True:
            if timeout > 0 and (time.time() - start) > timeout:
                print(f"  Slack reply timed out after {timeout}s")
                _slack_api("chat.postMessage", {
                    "channel": post_channel,
                    "thread_ts": thread_ts,
                    "text": ":hourglass: Timed out waiting for reply. Continuing with no answer.",
                }, bot_token=bot_token)
                return "(no answer)"

            elapsed = time.time() - start
            time.sleep(5 if elapsed < 60 else poll_interval)

            replies = _slack_api("conversations.replies", {
                "channel": post_channel,
                "ts": thread_ts,
            }, bot_token=bot_token, use_get=True)
            messages = replies.get("messages", [])
            # First message is the question itself; any after that are replies
            if len(messages) > 1:
                answer = messages[-1]["text"].strip()

                # Resolve numbered option
                if options and answer.isdigit():
                    idx = int(answer) - 1
                    if 0 <= idx < len(options):
                        answer = options[idx]

                if not answer:
                    answer = "(no answer)"

                print(f"  Slack reply: {answer}")
                # Acknowledge in thread
                _slack_api("chat.postMessage", {
                    "channel": post_channel,
                    "thread_ts": thread_ts,
                    "text": f":white_check_mark: Received: {answer}",
                }, bot_token=bot_token)
                return answer

    return ask_user_slack


# --- Execute ---


def _action_sort_key(action: dict) -> int:
    """Sort directory-creation actions before other actions."""
    if action.get("module") == "file" and action.get("params", {}).get("state") == "directory":
        return 0
    return 1


async def execute(ftl, actions: list[dict], dry_run: bool = False) -> list[dict]:
    """Execute the decided actions via FTL2 modules."""
    actions = sorted(actions, key=_action_sort_key)
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
            # Use bracket notation for host targeting — raises KeyError with
            # a clear message if the host isn't in inventory, instead of
            # silently falling through to NamespaceProxy and producing
            # confusing FQCN errors like "Invalid FQCN: hello-ai3.shell".
            if host:
                try:
                    target = ftl[host]
                except KeyError:
                    msg = (
                        f"Host '{host}' is not in the inventory. "
                        f"Did you forget to register it with add_host in state_ops? "
                        f"After creating a server, you must add_host with its IP in the "
                        f"SAME iteration, then target it in the NEXT iteration."
                    )
                    print(f"    FAILED: {msg}")
                    results.append({"module": module_name, "host": host, "result": {"error": msg}})
                    continue
            else:
                target = ftl
            module_fn = target
            for part in module_name.split("."):
                module_fn = getattr(module_fn, part)
            result = await module_fn(**params)
            # Normalize result to a plain dict for serialization.
            if isinstance(result, list):
                result = [
                    r.output if hasattr(r, 'output') and not isinstance(r, dict) else r
                    for r in result
                ]
                if len(result) == 1:
                    result = result[0]
            elif hasattr(result, 'output') and not isinstance(result, dict):
                result = result.output
            changed = result.get("changed", False) if isinstance(result, dict) else False
            print(f"    ok (changed={changed})")
            results.append({"module": module_name, "host": host, "result": result})

            # Auto-register hosts from cloud provisioning results.
            # When a module returns an instance with a label and IP (e.g.,
            # linode_v4), automatically add it to the live inventory so
            # subsequent actions in this iteration can target it.
            if isinstance(result, dict) and not host:
                instance = result.get("instance", {})
                if isinstance(instance, dict):
                    label = instance.get("label")
                    ipv4_list = instance.get("ipv4", [])
                    if label and ipv4_list:
                        ip = ipv4_list[0]
                        try:
                            ftl.add_host(
                                hostname=label,
                                ansible_host=ip,
                                ansible_user="root",
                            )
                            if hasattr(ftl, 'state') and ftl.state:
                                ftl.state.add_resource(label, {
                                    "provider": module_name.split(".")[-1],
                                    "label": label,
                                    "ipv4": ipv4_list,
                                })
                            print(f"    Auto-registered host: {label} ({ip})")
                            # Wait for SSH before allowing subsequent actions
                            # to target this host.
                            print(f"    Waiting for SSH on {ip}...")
                            try:
                                await ftl.wait_for(host=ip, port=22, timeout=180, delay=5, sleep=5)
                                print(f"    SSH ready on {label} ({ip})")
                            except Exception as wait_err:
                                print(f"    Warning: SSH wait failed for {label}: {wait_err}")
                        except Exception as e:
                            print(f"    Warning: failed to auto-register host {label}: {e}")
        except Exception as e:
            print(f"    FAILED: {e}")
            traceback.print_exc()
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
    policy: str | None = None,
    environment: str = "",
    dev: bool = False,
    initial_observations: list[dict] | None = None,
    audit_log: str | None = None,
    review_log: str | None = None,
    script_log: str | None = None,
    prior_increments: list[dict] | None = None,
    skip_rule_generation: bool = False,
    skip_rule_firing: bool = False,
    increments: list[dict] | None = None,
    user_answers: list[dict] | None = None,
    ask_user: "AskUserFunc | None" = None,
):
    """Run the AI reconciliation loop.

    Args:
        ask_user: Callable for prompting the user. Receives a dict with
            "question" (str) and optional "options" (list[str]), returns
            the user's answer as a string. Defaults to ask_user_stdin
            (interactive terminal). Use ask_user_noninteractive for
            headless/CI environments.

    Returns a dict with:
        converged (bool): Whether the desired state was achieved
        next_observations (list): Observations the AI wants run at the start of the next run
        history (list): Action history from this run
    """
    if ask_user is None:
        ask_user = ask_user_stdin
    def _write_audit_log(history, converged, iterations, increments=None):
        """Write the action history to a JSON audit log file."""
        if not audit_log:
            return
        try:
            log_path = Path(audit_log)
            # Load existing log entries if the file exists
            entries = []
            if log_path.exists():
                try:
                    entries = json.loads(log_path.read_text())
                except (json.JSONDecodeError, ValueError):
                    entries = []
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "desired_state": desired_state,
                "converged": converged,
                "iterations": iterations,
                "history": history,
            }
            if increments:
                entry["increments"] = increments
            entries.append(entry)
            log_path.write_text(json.dumps(entries, indent=2, default=str))
            print(f"Audit log written to {audit_log}")
        except Exception as e:
            print(f"Failed to write audit log: {e}")
            traceback.print_exc()

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
    if policy:
        automation_kwargs["policy"] = policy
    if environment:
        automation_kwargs["environment"] = environment

    async with automation(**automation_kwargs) as ftl:
        rules = load_rules(rules_dir)
        history: list[dict] = []
        extra_observers: list[dict] = initial_observations or []
        user_answers: list[dict] = list(user_answers) if user_answers else []
        rule_results: list[dict] = []
        consecutive_rule_runs = 0

        print(f"Desired state: {desired_state}")
        print(f"Rules loaded: {len(rules)}")
        print(f"Max iterations: {max_iterations}")
        if dry_run:
            print("DRY RUN — actions will not be executed")
        if dev:
            print("DEV MODE — AI reviews rules before they fire")
        if initial_observations:
            print(f"Initial observations from previous run: {len(initial_observations)}")
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
                    traceback.print_exc()

            # Check rules first, but don't let rules loop forever.
            # If a rule handled the last iteration too, skip rules and
            # let the AI check for convergence.
            if skip_rule_firing:
                pass  # Rules loaded for context but don't fire
            elif dev:
                # Dev mode: AI reviews rules before they fire and sees results after
                matched = await find_matching_rule(rules, current_state, ftl) if consecutive_rule_runs < 1 else None
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
                rule_handled, rule_obs = await check_rules(rules, current_state, ftl, dry_run)
                if rule_obs:
                    current_state.update(rule_obs)
                if consecutive_rule_runs < 1 and rule_handled:
                    print("Rule handled the situation.\n")
                    extra_observers = []
                    consecutive_rule_runs += 1
                    continue
                consecutive_rule_runs = 0

            # Decide
            print("Asking AI...")
            decision = await decide(current_state, desired_state, rules, history, user_answers,
                                    rule_results, i, max_iterations, prior_increments)

            reasoning = decision.get("reasoning", "")
            if reasoning:
                print(f"  Reasoning: {reasoning}")

            if decision.get("converged"):
                print(f"\nConverged after {i + 1} iteration(s).")
                next_obs = decision.get("observe", [])
                if next_obs:
                    print(f"  ({len(next_obs)} observation(s) queued for next run)")
                history.append({
                    "iteration": i,
                    "reasoning": reasoning,
                    "converged": True,
                    "actions": [],
                    "results": [],
                })
                _write_audit_log(history, converged=True, iterations=i + 1, increments=increments)
                if not skip_rule_generation:
                    await post_convergence_rule_generation(
                        desired_state, history, rules, rules_dir, current_state,
                    )
                await post_convergence_script_generation(
                    desired_state, history,
                    inventory=inventory,
                    script_log=script_log,
                )
                # Skip review when nothing happened — no actions or rule firings
                total_actions = sum(len(h.get("actions", [])) for h in history)
                rules_fired = any(r for r in rule_results if not r.get("denied"))
                fix_increment = None
                if total_actions > 0 or rules_fired:
                    print("Reviewing run...")
                    fix_increment = await post_convergence_review(
                        desired_state, history, i + 1, user_answers, rule_results,
                        converged=True, review_log=review_log,
                    )
                else:
                    print("  Skipping review (no actions taken).")
                return {"converged": True, "next_observations": next_obs, "history": history, "fix_increment": fix_increment}

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
                            traceback.print_exc()
                    else:
                        print(f"  DRY RUN: would {op_type} {name}")

            print()
            await asyncio.sleep(2)

        print(f"\nDid not converge after {max_iterations} iterations.")
        _write_audit_log(history, converged=False, iterations=max_iterations, increments=increments)
        print("Reviewing run...")
        fix_increment = await post_convergence_review(
            desired_state, history, max_iterations, user_answers, rule_results,
            converged=False, review_log=review_log,
        )
        return {"converged": False, "next_observations": [], "history": history, "fix_increment": fix_increment}


# --- Post-Convergence Script Generation ---


def generate_script_from_history(
    history: list[dict],
    desired_state: str,
    inventory: str | None = None,
    increments: list[dict] | None = None,
) -> str:
    """Mechanically generate a FTL2 script from the action history.

    Translates the sequence of successful actions into a standalone
    async Python script that can recreate the same state without the AI.
    """
    lines = [
        '#!/usr/bin/env python3',
        '"""',
        f'FTL2 script generated by ftl2-ai-loop.',
        f'Desired state: {desired_state}',
        f'Generated: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}',
        '"""',
        'import asyncio',
        'from ftl2 import automation',
        '',
        '',
        'async def main():',
    ]

    # Build automation() kwargs
    auto_args = []
    if inventory:
        auto_args.append(f'inventory="{inventory}"')
    auto_args_str = ", ".join(auto_args)
    lines.append(f'    async with automation({auto_args_str}) as ftl:')

    # Track hosts that need registration
    registered_hosts = set()
    had_actions = False
    current_increment = None

    # Build increment lookup by number
    increment_map = {}
    if increments:
        for inc in increments:
            increment_map[inc["n"]] = inc

    for entry in history:
        iteration = entry.get("iteration", "?")
        reasoning = entry.get("reasoning", "")
        actions = entry.get("actions", [])
        results = entry.get("results", [])

        if not actions:
            continue

        # Insert increment comment when we enter a new increment
        entry_increment = entry.get("increment_n")
        if entry_increment and entry_increment != current_increment:
            current_increment = entry_increment
            inc = increment_map.get(entry_increment, {})
            inc_desc = inc.get("desired_state", f"Increment {entry_increment}")
            lines.append(f'')
            lines.append(f'        # --- Increment {entry_increment}: {inc_desc} ---')

        lines.append(f'')
        lines.append(f'        # Iteration {iteration}')
        if reasoning:
            # Truncate long reasoning to first sentence
            short = reasoning.split(". ")[0].rstrip(".")
            lines.append(f'        # {short}')

        for action, result in zip(actions, results):
            module_name = action.get("module", "unknown")
            host = action.get("host")
            params = action.get("params", {})

            # Skip failed actions
            res = result.get("result", {})
            if isinstance(res, dict):
                if res.get("error") or res.get("failed"):
                    lines.append(f'        # SKIPPED (failed): {module_name} on {host or "localhost"}')
                    continue

            had_actions = True

            # Build the await call
            if host:
                target = f'ftl["{host}"]'
            else:
                target = 'ftl'

            # Build module chain for FQCN (e.g., ansible.posix.firewalld)
            parts = module_name.split(".")
            module_chain = ".".join(parts)

            # Format params as keyword arguments
            param_strs = []
            for k, v in params.items():
                param_strs.append(f'{k}={v!r}')
            params_str = ", ".join(param_strs)

            lines.append(f'        await {target}.{module_chain}({params_str})')

    if not had_actions:
        lines.append(f'        pass  # No actions were needed (already converged)')

    lines.extend([
        '',
        '',
        'if __name__ == "__main__":',
        '    asyncio.run(main())',
        '',
    ])

    return "\n".join(lines)


async def post_convergence_script_generation(
    desired_state: str,
    history: list[dict],
    inventory: str | None = None,
    script_log: str | None = None,
    increments: list[dict] | None = None,
):
    """Generate a FTL2 script from the action history, then have AI review it."""
    # Only generate scripts when there were actual actions
    total_actions = sum(len(h.get("actions", [])) for h in history)
    if total_actions == 0:
        return

    # Step 1: Mechanically generate the script
    draft = generate_script_from_history(history, desired_state, inventory, increments)

    # Step 2: Have the AI review and improve it
    prompt = textwrap.dedent(f"""\
        Below is a mechanically-generated FTL2 script that recreates an infrastructure
        state. Review it and produce an improved version.

        The script should:
        - Be a standalone, runnable Python script using `from ftl2 import automation`
        - Use `async with automation(...) as ftl:` as the entry point
        - Use bracket notation `ftl["hostname"]` for remote hosts
        - Use FQCN for collection modules (e.g., `ansible.posix.firewalld`)
        - Remove any failed/skipped actions
        - Remove redundant actions (e.g., if shell fallback did same thing as a failed module)
        - Add brief comments explaining each logical step
        - Keep only the actions that actually achieved the desired state
        - Use `ftl.wait_for(host=ip, port=22)` after server creation if applicable
        - Handle action ordering correctly (install before configure, configure before start)

        Desired state: {desired_state}

        Draft script:
        ```python
        {draft}
        ```

        Respond with ONLY the improved Python script, no explanation. Start with #!/usr/bin/env python3
    """)

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await proc.communicate()
    except (KeyboardInterrupt, asyncio.CancelledError):
        proc.terminate()
        await proc.wait()
        raise KeyboardInterrupt
    response = stdout.decode().strip()
    _log_prompt("script-generation", prompt, response)

    # Extract the script from the response (strip markdown fences if present)
    script = response
    if "```python" in script:
        script = script.split("```python", 1)[1]
        script = script.split("```", 1)[0]
    elif "```" in script:
        script = script.split("```", 1)[1]
        script = script.split("```", 1)[0]
    script = script.strip()

    # Fall back to the mechanical draft if AI returned garbage
    if not script or "async" not in script or "automation" not in script:
        print("  AI review returned invalid script, using mechanical draft.")
        script = draft

    if script_log:
        _write_script_log(script_log, script, desired_state)

    print(f"\n--- Generated FTL2 Script ---")
    print(script)
    print(f"--- End Script ---\n")


def _write_script_log(script_dir: str, script: str, desired_state: str):
    """Write the generated script to a numbered file."""
    try:
        dir_path = Path(script_dir)
        dir_path.mkdir(parents=True, exist_ok=True)

        existing = sorted(dir_path.glob("script-*.py"))
        if existing:
            last = existing[-1].stem
            try:
                n = int(last.split("-")[1]) + 1
            except (IndexError, ValueError):
                n = len(existing) + 1
        else:
            n = 1

        filename = f"script-{n:03d}.py"
        filepath = dir_path / filename
        filepath.write_text(script + "\n")
        print(f"  Script saved to {filepath}")
    except Exception as e:
        print(f"  Failed to write script: {e}")
        traceback.print_exc()


# --- Post-Convergence Rule Generation ---


async def post_convergence_rule_generation(
    desired_state: str,
    history: list[dict],
    rules: list[dict],
    rules_dir: str,
    current_state: dict | None = None,
):
    """Ask the AI to write a deterministic rule based on the converged run."""
    history_json = json.dumps(history, indent=2, default=str)
    state_json = json.dumps(current_state, indent=2, default=str) if current_state else "None"
    existing_rules = [r.get("name", "unknown") for r in rules]

    prompt = textwrap.dedent(f"""\
        You just completed an infrastructure reconciliation run that converged successfully.
        Review the actions taken and decide if any recurring pattern should be codified as
        a deterministic rule.

        Desired state: {desired_state}
        Action history: {history_json}
        Final observation state: {state_json}
        Existing rules: {json.dumps(existing_rules)}

        The "Final observation state" shows the keys and values available in the state dict
        that gets passed to your condition function. Use these exact keys to write a condition
        that checks whether the rule needs to fire.

        A rule replaces the AI for a specific pattern — if the rule's condition matches,
        the rule fires directly without calling the AI. Rules should capture idempotent
        patterns that will recur on every run (e.g., "ensure nginx is installed and running").

        If you identify a pattern worth codifying, respond with a JSON object:
        {{
          "name": "snake_case_name",
          "condition": "human-readable description of when this fires",
          "description": "what this rule does",
          "observe": [
            {{"name": "state_key", "module": "module_name", "params": {{}}, "host": "optional_hostname"}}
          ],
          "code": "the full Python code"
        }}

        The "observe" field is REQUIRED. It declares the observations the rule needs
        before its condition can be evaluated. The rule engine will run these observations
        and inject the results into the state dict before calling your condition function.
        Without this, your condition will see empty state and fire incorrectly.

        Each observation has:
        - "name": the key in the state dict (this is what your condition reads)
        - "module": the FTL2 module to call (e.g., "command", "shell", "service")
        - "params": module parameters (e.g., {{"cmd": "curl -s http://1.2.3.4/"}})
        - "host": optional hostname to run on (omit for localhost)

        The code must define exactly two async functions:

        async def condition(state: dict) -> bool:
            # Return True if this rule should fire.
            # 'state' contains observation results keyed by name.
            # These keys come from the "observe" list above.
            return state.get("nginx_status", {{}}).get("stdout", "").strip() != "active"

        async def action(ftl) -> None:
            # Execute the actions. Call modules as await ftl.module_name(**params).
            await ftl.dnf(name="nginx", state="present")
            await ftl.service(name="nginx", state="started", enabled=True)

        To run modules on a REMOTE HOST, use bracket notation:
            await ftl["hello-ai3"].dnf(name="nginx", state="present")
            await ftl["hello-ai3"].service(name="nginx", state="started", enabled=True)
            await ftl["hello-ai3"].ansible.posix.firewalld(service="http", state="enabled")
        Without bracket notation, modules run on LOCALHOST (the controller).
        Do NOT use _host as a parameter — it does not work.

        CRITICAL rules for the code:
        - For localhost: "await ftl.module_name(**params)"
        - For remote hosts: "await ftl[\"hostname\"].module_name(**params)"
        - For FQCN modules use dot notation: "await ftl[\"hostname\"].community.general.linode_v4(label=..., state='present')"
        - Do NOT use ftl.call(), ftl.run(), subprocess, os.system, curl, or any other method
        - Do NOT use _host as a module parameter — use ftl["hostname"] bracket notation
        - Do NOT read secrets from os.environ — they are injected automatically
        - Your condition MUST only reference state keys declared in "observe"

        If no pattern is worth codifying (e.g., the run was trivial or the pattern already
        exists in the rules list), respond with:
        {{"skip": true, "reasoning": "brief explanation"}}
    """)

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await proc.communicate()
    except (KeyboardInterrupt, asyncio.CancelledError):
        proc.terminate()
        await proc.wait()
        raise KeyboardInterrupt
    raw = stdout.decode().strip()
    _log_prompt("rule-generation", prompt, raw)

    if proc.returncode != 0:
        return

    try:
        result = json.loads(extract_json(raw))
    except (json.JSONDecodeError, ValueError):
        return

    if result.get("skip"):
        print(f"  Rule generation skipped: {result.get('reasoning', 'no reason given')}")
        return

    if result.get("name") and result.get("code"):
        print("Learning...")
        save_rule(result, rules_dir)


async def review_rules(rules_dir: str = "rules", review_log: str | None = None) -> str | None:
    """Review all rules for conflicts, redundancies, and issues."""
    rules = load_rules(rules_dir)
    if not rules:
        print("No rules to review.")
        return None

    # Read the full source code of each rule file
    rule_sources = []
    for rule in rules:
        try:
            source = Path(rule["path"]).read_text()
        except Exception:
            source = "(could not read source)"
        rule_sources.append({
            "name": rule["name"],
            "doc": rule["doc"],
            "source": source,
        })

    rules_text = ""
    for rs in rule_sources:
        rules_text += f"\n--- Rule: {rs['name']} ---\n"
        if rs["doc"]:
            rules_text += f"Docstring: {rs['doc']}\n"
        rules_text += f"Source:\n{rs['source']}\n"

    prompt = textwrap.dedent(f"""\
        Review the following {len(rules)} infrastructure rules for issues.
        These rules are used in an AI reconciliation loop — when a rule's condition
        matches the current system state, its action fires automatically without AI involvement.

        {rules_text}

        Analyze all rules together and report:

        1. CONFLICTS — Rules with conditions that could match the same situation but take
           different or contradictory actions. For each conflict, name both rules and explain
           the scenario.

        2. REDUNDANCIES — Rules that do the same thing or have overlapping logic that could
           be merged. Name the rules and suggest how to consolidate.

        3. INTERFERENCE — Rules where one rule's action could trigger another rule's condition
           in an unintended loop or cascade. Describe the chain.

        4. BROKEN DEPENDENCIES — Rules whose condition references observation keys that don't
           appear in their observe list, or observe entries that look misconfigured.

        5. RECOMMENDATIONS — Which rules should be disabled or merged, with brief reasoning.

        Be specific. Reference rules by name. If everything looks clean, say so.
    """)

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await proc.communicate()
    except (KeyboardInterrupt, asyncio.CancelledError):
        proc.terminate()
        await proc.wait()
        raise KeyboardInterrupt
    raw = stdout.decode().strip()
    _log_prompt("rule-review", prompt, raw)

    if proc.returncode != 0:
        print("Rule review failed.")
        return None

    print(raw)

    # Write to review log directory
    log_dir = review_log or (str(Path(rules_dir).parent / "reviews") if rules_dir != "rules" else None)
    if log_dir:
        try:
            dir_path = Path(log_dir)
            dir_path.mkdir(parents=True, exist_ok=True)
            existing = sorted(dir_path.glob("rule-review-*.md"))
            if existing:
                try:
                    n = int(existing[-1].stem.split("-")[-1]) + 1
                except (IndexError, ValueError):
                    n = len(existing) + 1
            else:
                n = 1
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            rule_names = [r["name"] for r in rules]
            filepath = dir_path / f"rule-review-{n:03d}.md"
            filepath.write_text(
                f"# Rule Review {n:03d}\n\n"
                f"**Date:** {ts}\n"
                f"**Rules reviewed:** {', '.join(rule_names)}\n\n"
                f"{raw}\n"
            )
            print(f"  Review written to {filepath}")
        except Exception:
            pass

    return raw


async def review_script(script: str, desired_state: str, review_log: str | None = None) -> str | None:
    """Review a generated script for quality issues."""
    prompt = textwrap.dedent(f"""\
        Review the following generated Ansible/FTL2 script for quality issues.

        The script was generated to achieve this desired state:
        {desired_state}

        Script:
        {script}

        Analyze the script and report:

        1. NON-IDEMPOTENT OPERATIONS — Places where shell/command+curl is used instead
           of get_url, or raw shell commands instead of proper modules (apt, yum, copy, etc.).

        2. WRONG MODULE USAGE — Incorrect module parameters, deprecated modules, or
           modules used in ways that won't work as intended.

        3. ORDERING ISSUES — Tasks that depend on earlier tasks but aren't ordered
           correctly, or missing handlers/notifications.

        4. HARDCODED VALUES — Values that should use variables, src= file references,
           or mechanical lookups (like package versions, URLs, paths).

        5. SECURITY ISSUES — Exposed credentials, overly permissive file modes,
           missing become/privilege escalation where needed, or insecure downloads.

        6. ERROR HANDLING — Missing failed_when/changed_when, ignore_errors used
           inappropriately, or missing retries on network operations.

        Be specific. Reference task names or line numbers. If everything looks clean, say so.
    """)

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await proc.communicate()
    except (KeyboardInterrupt, asyncio.CancelledError):
        proc.terminate()
        await proc.wait()
        raise KeyboardInterrupt
    raw = stdout.decode().strip()
    _log_prompt("script-review", prompt, raw)

    if proc.returncode != 0:
        print("Script review failed.")
        return None

    print(raw)

    if review_log:
        try:
            dir_path = Path(review_log)
            dir_path.mkdir(parents=True, exist_ok=True)
            existing = sorted(dir_path.glob("script-review-*.md"))
            if existing:
                try:
                    n = int(existing[-1].stem.split("-")[-1]) + 1
                except (IndexError, ValueError):
                    n = len(existing) + 1
            else:
                n = 1
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            filepath = dir_path / f"script-review-{n:03d}.md"
            filepath.write_text(
                f"# Script Review {n:03d}\n\n"
                f"**Date:** {ts}\n"
                f"**Desired state:** {desired_state}\n\n"
                f"{raw}\n"
            )
            print(f"  Review written to {filepath}")
        except Exception:
            pass

    return raw


# --- Post-Convergence Review ---


async def post_convergence_review(
    desired_state: str,
    history: list[dict],
    iterations: int,
    user_answers: list[dict],
    rule_results: list[dict],
    converged: bool = True,
    review_log: str | None = None,
) -> str | None:
    """Ask the AI to review its own performance and suggest feature requests.

    Returns a fix increment desired-state string if the review identifies an
    unresolved failure or false convergence, otherwise None.
    """
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

           NOTE: The following features ALREADY EXIST — do not request them:
           - Dry-run mode (--dry-run flag)
           - Observations and actions in the same response (use "observe" and "actions" together)
           - OS/platform facts cached in the state file (_state_file → hosts → facts)
           - State file for tracking resources and hosts across runs (--state flag)
           - wait_for module for polling TCP ports (instead of shell: sleep)
           - Auto-SSH-wait after cloud provisioning (framework waits for port 22 automatically)
           - Host targeting via "host" field in actions (no need for ssh user@host)
           - Secret injection via secret_bindings (no env vars or hardcoded tokens)
           - Asking the user questions via "ask" in the response JSON
           Only request features that are NOT in this list.

        3. FIX INCREMENT (optional) — If you identified an unresolved failure or
           false convergence above, provide a fix as a desired state string.

           Format (end of response):
           <<<FIX_INCREMENT>>>
           the desired state string for the fix
           <<<END_FIX_INCREMENT>>>

           Omit entirely if no fix is needed. Only for genuine unresolved problems.

        Write your response as plain text for the user to read. Be concise and direct.
    """)

    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await proc.communicate()
    except (KeyboardInterrupt, asyncio.CancelledError):
        proc.terminate()
        await proc.wait()
        raise KeyboardInterrupt
    review = stdout.decode().strip()
    _log_prompt("self-review", prompt, review)

    if proc.returncode != 0:
        return None

    fix_increment = None
    if review:
        # Parse for fix increment sentinel
        fix_match = re.search(
            r"<<<FIX_INCREMENT>>>\s*\n(.*?)\n\s*<<<END_FIX_INCREMENT>>>",
            review, re.DOTALL,
        )
        if fix_match:
            fix_increment = fix_match.group(1).strip() or None
            # Strip the sentinel block from displayed review
            review = review[:fix_match.start()].rstrip()

        print(f"\n--- AI Self-Review ---")
        print(review)
        if fix_increment:
            print(f"  [Fix increment identified: {fix_increment}]")
        print(f"--- End Review ---\n")

        if review_log:
            _write_review_log(
                review_log, review, desired_state, history,
                iterations, converged, user_answers, rule_results,
            )

    return fix_increment


def _write_review_log(
    review_dir: str,
    review: str,
    desired_state: str,
    history: list[dict],
    iterations: int,
    converged: bool,
    user_answers: list[dict],
    rule_results: list[dict],
):
    """Write a self-review to a numbered markdown file with run metadata."""
    try:
        dir_path = Path(review_dir)
        dir_path.mkdir(parents=True, exist_ok=True)

        # Find next review number
        existing = sorted(dir_path.glob("review-*.md"))
        if existing:
            last = existing[-1].stem  # e.g., "review-003"
            try:
                n = int(last.split("-")[1]) + 1
            except (IndexError, ValueError):
                n = len(existing) + 1
        else:
            n = 1

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # Summarize actions: count total, changed, failed per host
        action_count = 0
        changed_count = 0
        failed_count = 0
        hosts_used = set()
        modules_used = []
        for entry in history:
            for act, res in zip(entry.get("actions", []), entry.get("results", [])):
                action_count += 1
                host = act.get("host", "localhost")
                hosts_used.add(host)
                modules_used.append(act.get("module", "unknown"))
                result = res.get("result", {})
                if isinstance(result, dict):
                    if result.get("error"):
                        failed_count += 1
                    elif result.get("changed"):
                        changed_count += 1

        # Build markdown
        lines = [
            f"# Self-Review #{n}",
            f"",
            f"**Date:** {ts}",
            f"**Converged:** {'Yes' if converged else 'No'}",
            f"**Iterations:** {iterations}",
            f"**Actions:** {action_count} total, {changed_count} changed, {failed_count} failed",
            f"**Hosts:** {', '.join(sorted(hosts_used)) or 'none'}",
            f"",
            f"## Desired State",
            f"",
            f"{desired_state}",
            f"",
            f"## Review",
            f"",
            review,
            f"",
            f"## Action Log",
            f"",
        ]

        for entry in history:
            iteration = entry.get("iteration", "?")
            reasoning = entry.get("reasoning", "")
            lines.append(f"### Iteration {iteration}")
            if reasoning:
                lines.append(f"")
                lines.append(f"**Reasoning:** {reasoning}")
            actions = entry.get("actions", [])
            results = entry.get("results", [])
            if not actions:
                lines.append(f"")
                lines.append(f"No actions.")
            else:
                lines.append(f"")
                lines.append(f"| # | Module | Host | Changed | Error |")
                lines.append(f"|---|--------|------|---------|-------|")
                for j, (act, res) in enumerate(zip(actions, results), 1):
                    mod = act.get("module", "?")
                    host = act.get("host", "localhost")
                    result = res.get("result", {})
                    if isinstance(result, dict):
                        ch = "yes" if result.get("changed") else "no"
                        err = result.get("error", "")
                    else:
                        ch = "?"
                        err = ""
                    err_short = (err[:60] + "...") if len(err) > 60 else err
                    lines.append(f"| {j} | `{mod}` | {host} | {ch} | {err_short} |")
            lines.append(f"")

        if user_answers:
            lines.append(f"## User Questions")
            lines.append(f"")
            for qa in user_answers:
                lines.append(f"- **Q:** {qa.get('question', '?')}")
                lines.append(f"  **A:** {qa.get('answer', '?')}")
            lines.append(f"")

        if rule_results:
            lines.append(f"## Rule Results")
            lines.append(f"")
            for rr in rule_results:
                lines.append(f"- {json.dumps(rr, default=str)}")
            lines.append(f"")

        filename = f"review-{n:03d}.md"
        (dir_path / filename).write_text("\n".join(lines))
        print(f"Review written to {review_dir}/{filename}")

    except Exception as e:
        print(f"Failed to write review log: {e}")


# --- Continuous Mode ---


async def run_continuous(reconcile_kwargs: dict, delay: int, ask_user: "AskUserFunc | None" = None, notify=None):
    """Run the reconciliation loop continuously with a delay between runs."""
    import time as _time

    if ask_user is None:
        ask_user = ask_user_noninteractive
    run_count = 0
    next_observations: list[dict] = []
    startup_commit = _get_startup_commit()
    if startup_commit:
        print(f"Running commit: {startup_commit[:8]}")
    print(f"Continuous mode: reconciling every {delay}s (Ctrl+C to stop)\n")
    try:
        while True:
            run_count += 1
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"{'=' * 60}")
            print(f"Run #{run_count} — {timestamp}")
            print(f"{'=' * 60}")

            _run_error = None
            _t0 = _time.monotonic()
            try:
                result = await reconcile(
                    **reconcile_kwargs,
                    initial_observations=next_observations,
                    ask_user=ask_user,
                )
                converged = result["converged"]
                next_observations = result.get("next_observations", [])
                status = "converged" if converged else "did not converge"
            except Exception as e:
                print(f"\nRun #{run_count} failed: {e}")
                traceback.print_exc()
                next_observations = []
                status = "error"
                _run_error = e
                result = None
            _duration = _time.monotonic() - _t0

            if notify:
                if _run_error is not None:
                    notify(
                        desired_state=reconcile_kwargs["desired_state"],
                        converged=False,
                        iterations=0,
                        actions_taken=0,
                        duration=_duration,
                        error=_run_error,
                        run_number=run_count,
                    )
                elif result is not None:
                    _total_actions = sum(len(h.get("actions", [])) for h in result.get("history", []))
                    notify(
                        desired_state=reconcile_kwargs["desired_state"],
                        converged=result["converged"],
                        iterations=len(result.get("history", [])),
                        actions_taken=_total_actions,
                        duration=_duration,
                        run_number=run_count,
                    )

            print(f"\nRun #{run_count} {status}. Next run in {delay}s...\n")

            # Check for updates between runs
            if await _check_for_update(startup_commit):
                sys.exit(UPDATE_EXIT_CODE)

            await asyncio.sleep(delay)
    except KeyboardInterrupt:
        print(f"\nStopped after {run_count} run(s).")


# --- Incremental Mode ---


async def run_incremental(reconcile_kwargs: dict, plan_file: str | None = None, ask_user: "AskUserFunc | None" = None, notify=None, delay: int = 60):
    """Run the reconciliation loop incrementally, prompting for new work after each convergence."""
    import time as _time

    if ask_user is None:
        ask_user = ask_user_stdin
    increments = []
    all_history = []
    next_observations = None
    _t0 = _time.monotonic()

    desired_state = reconcile_kwargs.get("desired_state") or ""
    n = 1

    # Prompt for desired state if not provided
    if not desired_state:
        desired_state = ask_user({"question": "What would you like to do?"})
        if not desired_state or desired_state == "(no answer)":
            print("No desired state provided. Exiting.")
            return
        reconcile_kwargs["desired_state"] = desired_state

    # Load a saved plan if provided
    loaded_plan = None
    if plan_file:
        try:
            loaded_plan = json.loads(Path(plan_file).read_text())
            print(f"Loaded plan from {plan_file}")
        except Exception as e:
            print(f"Failed to load plan from {plan_file}: {e}")
            print("Falling back to AI planning.")

    max_consecutive_fixes = 2
    consecutive_fixes = 0
    fix_increment_states = set()

    try:
        while True:
            # Use loaded plan on first iteration, otherwise run planning
            if loaded_plan is not None:
                plan_result = loaded_plan
                loaded_plan = None  # Only use it once
                planning_answers = plan_result.get("user_answers", [])
            else:
                print(f"\nPlanning...")
                plan_result = await plan(desired_state, ask_user=ask_user)
                planning_answers = plan_result.get("user_answers", [])

            increment_queue = list(plan_result["increments"])
            planning_observations = plan_result.get("initial_observations", [])

            _print_plan(plan_result)

            # Confirm plan before executing — include plan text in the question
            # so Slack users can see what they're approving
            plan_lines = [f"*Plan: {len(increment_queue)} increment(s)*"]
            for j, inc_state in enumerate(increment_queue, 1):
                plan_lines.append(f"  {j}. {inc_state}")
            plan_text = "\n".join(plan_lines)
            answer = ask_user({"question": f"{plan_text}\n\nProceed with this plan?", "options": ["yes", "no"]})
            if answer.lower() not in ("yes", "1"):
                print("  Plan rejected.")
                break

            # Execute each planned increment
            first_in_plan = True
            while increment_queue:
                current_desired = increment_queue.pop(0)
                is_fix = current_desired in fix_increment_states

                print(f"\n=== Increment {n} ===")
                inc_meta = {"n": n, "desired_state": current_desired}
                if is_fix:
                    inc_meta["fix"] = True
                increments.append(inc_meta)

                # Build kwargs for this increment
                kwargs = {
                    **reconcile_kwargs,
                    "desired_state": current_desired,
                    "prior_increments": increments[:-1] if len(increments) > 1 else None,
                    "skip_rule_generation": False,
                    "skip_rule_firing": True,
                    "increments": increments,
                }

                # Inject planning observations into the first increment only
                if first_in_plan and planning_observations:
                    kwargs["initial_observations"] = planning_observations
                elif next_observations:
                    kwargs["initial_observations"] = next_observations

                # Seed planning Q&A into the first increment
                if first_in_plan and planning_answers:
                    kwargs["user_answers"] = planning_answers

                first_in_plan = False

                result = await reconcile(**kwargs)

                increments[-1]["converged"] = result["converged"]
                increments[-1]["iterations"] = len(result.get("history", []))

                # Tag history entries with increment number before accumulating
                for entry in result.get("history", []):
                    entry["increment_n"] = n
                all_history.extend(result.get("history", []))

                next_observations = result.get("next_observations")

                # Check for fix increment from self-review
                fix_desired = result.get("fix_increment")
                if fix_desired and consecutive_fixes < max_consecutive_fixes:
                    print(f"  Injecting fix increment: {fix_desired}")
                    increment_queue.insert(0, fix_desired)
                    fix_increment_states.add(fix_desired)
                    consecutive_fixes += 1
                elif fix_desired:
                    print(f"  Fix increment suppressed (max {max_consecutive_fixes} consecutive fixes reached)")
                    consecutive_fixes = 0
                else:
                    consecutive_fixes = 0

                n += 1

            # Prompt for next increment
            answer = ask_user({"question": "What would you like to do next?", "options": ["done", "continuous"]})
            if not answer or answer in ("(no answer)", "done", "1"):
                break
            if answer in ("continuous", "2"):
                # Review rules before entering continuous mode
                rules = load_rules(reconcile_kwargs.get("rules_dir", "rules"))
                if rules:
                    print("\nReviewing rules before entering continuous mode...")
                    await review_rules(reconcile_kwargs.get("rules_dir", "rules"),
                                       review_log=reconcile_kwargs.get("review_log"))
                print(f"\nSwitching to continuous mode (every {delay}s)...")
                await run_continuous(reconcile_kwargs, delay, ask_user=ask_user_noninteractive, notify=notify)
                return
            desired_state = answer

    except KeyboardInterrupt:
        pass

    print(f"\nDone. {len(increments)} increment(s) completed.")

    if notify and increments:
        _duration = _time.monotonic() - _t0
        _total_actions = sum(len(h.get("actions", [])) for h in all_history)
        notify(
            desired_state=desired_state,
            converged=all(i.get("converged") for i in increments),
            iterations=sum(i.get("iterations", 0) for i in increments),
            actions_taken=_total_actions,
            duration=_duration,
            increments=increments,
        )

    # Generate a combined script covering all increments
    if reconcile_kwargs.get("script_log") and all_history:
        total_actions = sum(len(h.get("actions", [])) for h in all_history)
        if total_actions > 0:
            combined_desired = "Incremental build: " + "; ".join(
                i["desired_state"] for i in increments
            )
            await post_convergence_script_generation(
                desired_state=combined_desired,
                history=all_history,
                inventory=reconcile_kwargs.get("inventory"),
                script_log=reconcile_kwargs["script_log"],
                increments=increments,
            )

            # Review the combined script
            script_dir = Path(reconcile_kwargs["script_log"])
            scripts = sorted(script_dir.glob("script-*.py"))
            if scripts:
                script_text = scripts[-1].read_text()
                print("\nReviewing generated script...")
                await review_script(
                    script_text, combined_desired,
                    review_log=reconcile_kwargs.get("review_log"),
                )

    # Review all rules for conflicts
    rules = load_rules(reconcile_kwargs.get("rules_dir", "rules"))
    if rules:
        print("\nReviewing rules for conflicts...")
        await review_rules(reconcile_kwargs.get("rules_dir", "rules"),
                           review_log=reconcile_kwargs.get("review_log"))


# --- Plan Only Mode ---


def _print_plan(plan_result: dict):
    """Print a plan result to the console."""
    increments = plan_result["increments"]
    initial_observations = plan_result.get("initial_observations", [])

    print(f"\n  Plan: {len(increments)} increment(s)")
    for j, inc_state in enumerate(increments, 1):
        print(f"    {j}. {inc_state}")

    if initial_observations:
        print(f"\n  Initial observations for first increment:")
        for obs in initial_observations:
            host = obs.get("host")
            module = obs.get("module", "?")
            params = obs.get("params", {})
            params_str = ", ".join(f"{k}={v!r}" for k, v in params.items())
            if host:
                print(f"    - {host}: {module}({params_str}) as '{obs.get('name', '?')}'")
            else:
                print(f"    - {module}({params_str}) as '{obs.get('name', '?')}'")

    print()


async def run_plan_only(desired_state: str, output_file: str | None = None):
    """Run the planning phase and print the result without executing."""
    print("Planning...")
    result = await plan(desired_state)
    _print_plan(result)

    if output_file:
        Path(output_file).write_text(json.dumps(result, indent=2))
        print(f"Plan saved to {output_file}")


# --- CLI ---


def _phone_home():
    """Send a single telemetry event to Segment. Fire and forget.

    Sends only the application name and git commit hash.
    No user information. No system information.

    To disable telemetry, set the environment variable FTL2_TELEMETRY=off.
    """
    import os

    if os.environ.get("FTL2_TELEMETRY", "").lower() == "off":
        return
    try:
        import atexit
        import uuid

        import segment.analytics as analytics

        analytics.write_key = "haXw8AZ0x06563tTahJi6kOJxPLqMC79"
        atexit.register(analytics.shutdown)

        # Get git hash from importlib metadata or git rev-parse
        version = "unknown"
        try:
            import importlib.metadata

            dist = importlib.metadata.distribution("ftl2-ai-loop")
            for f in dist.files or []:
                if f.name == "direct_url.json":
                    data = json.loads(f.read_text())
                    commit = data.get("vcs_info", {}).get("commit_id")
                    if commit:
                        version = commit
                        break
        except Exception:
            pass
        if version == "unknown":
            try:
                import subprocess

                result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    version = result.stdout.strip()
            except Exception:
                pass

        analytics.track(
            anonymous_id=str(uuid.uuid4()),
            event="ftl2_ai_loop_run",
            properties={
                "name": "ftl2-ai-loop",
                "version": version,
            },
        )
    except Exception:
        pass  # Never crash the tool for telemetry


def cli():
    _phone_home()
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
    parser.add_argument("desired_state", nargs="?", default=None,
                        help="Natural language description of desired state")
    parser.add_argument("-f", "--file", help="Read desired state from a file instead of the command line")
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
    parser.add_argument("--policy", help="YAML policy file to enforce before each module execution")
    parser.add_argument("--environment", default="",
                        help="Environment label for policy matching (e.g., prod, staging)")
    parser.add_argument("--audit-log", help="JSON file to append action history after each run")
    parser.add_argument("--prompt-log", help="Directory to write prompt/response pairs (one file per call)")
    parser.add_argument("--review-log", help="Directory to write self-review markdown files (one per run)")
    parser.add_argument("--script-log", help="Directory to write generated FTL2 scripts (one per run)")
    parser.add_argument("--dev", action="store_true",
                        help="Dev mode: AI reviews rules before they fire and sees results after")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--continuous", action="store_true",
                            help="Run continuously, re-reconciling after each delay period")
    mode_group.add_argument("--incremental", action="store_true",
                            help="Prompt for additional work after each convergence")
    mode_group.add_argument("--plan-only", action="store_true",
                            help="Run the planning phase and show increments without executing")
    mode_group.add_argument("--review-rules", action="store_true",
                            help="Review all rules for conflicts and issues")
    parser.add_argument("--delay", type=int, default=60,
                        help="Seconds between reconciliation runs in continuous mode (default: 60)")
    parser.add_argument("-o", "--output", help="Output file for --plan-only to save the plan as JSON")
    parser.add_argument("--plan", help="Load a saved plan JSON file for --incremental (skips planning)")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Skip user prompts (for headless/CI/Receptor environments)")
    parser.add_argument("--ask-via-slack", metavar="CHANNEL",
                        help="Post AI questions to a Slack channel and poll for replies (e.g., '#approvals')")
    parser.add_argument("--slack-poll-interval", type=int, default=30,
                        help="Seconds between polling Slack for replies (default: 30)")
    parser.add_argument("--slack-timeout", type=int, default=0,
                        help="Max seconds to wait for a Slack reply (0 = no timeout, default: 0)")
    parser.add_argument("--notify-slack", metavar="CHANNEL",
                        help="Post run summaries to a Slack channel (e.g., '#deploys'). Requires SLACK_BOT_TOKEN.")
    parser.add_argument("--tui", action="store_true",
                        help="Run with full-screen terminal UI")
    args = parser.parse_args()

    # Resolve desired state from file or positional argument
    if args.file:
        try:
            args.desired_state = Path(args.file).read_text().strip()
        except Exception as e:
            parser.error(f"Cannot read file {args.file!r}: {e}")
    if not args.desired_state and not args.review_rules and not args.incremental:
        parser.error("desired_state is required (positional argument or -f/--file)")

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

    global _prompt_log_dir, _prompt_log_counter
    if args.prompt_log:
        _prompt_log_dir = Path(args.prompt_log)
        _prompt_log_counter = 0

    reconcile_kwargs = dict(
        desired_state=args.desired_state,
        inventory=args.inventory,
        max_iterations=args.max_iterations,
        rules_dir=args.rules_dir,
        dry_run=args.dry_run,
        quiet=args.quiet,
        secret_bindings=secret_bindings or None,
        state_file=args.state_file,
        policy=args.policy,
        environment=args.environment,
        dev=args.dev,
        audit_log=args.audit_log,
        review_log=args.review_log,
        script_log=args.script_log,
    )

    # Select ask_user backend
    if args.ask_via_slack:
        _ask_user = make_ask_user_slack(
            channel=args.ask_via_slack,
            poll_interval=args.slack_poll_interval,
            timeout=args.slack_timeout,
        )
    elif args.non_interactive:
        _ask_user = ask_user_noninteractive
    else:
        _ask_user = ask_user_stdin

    # Resolve Slack notification callback
    _notify_fn = None
    if args.notify_slack:
        _notify_token = os.environ.get("SLACK_BOT_TOKEN")
        if not _notify_token:
            parser.error("--notify-slack requires SLACK_BOT_TOKEN env var to be set")
        _notify_channel = args.notify_slack

        def _notify_fn(**kwargs):
            _notify_slack(channel=_notify_channel, bot_token=_notify_token, **kwargs)

    if args.tui:
        from ftl2_ai_loop_tui import run_tui
        run_tui(args, reconcile_kwargs, _notify_fn)
        return

    try:
        if args.review_rules:
            asyncio.run(review_rules(args.rules_dir, review_log=args.review_log))
        elif args.continuous:
            asyncio.run(run_continuous(reconcile_kwargs, args.delay, ask_user=_ask_user, notify=_notify_fn))
        elif args.incremental:
            asyncio.run(run_incremental(reconcile_kwargs, plan_file=args.plan, ask_user=_ask_user, notify=_notify_fn, delay=args.delay))
        elif args.plan_only:
            asyncio.run(run_plan_only(args.desired_state, output_file=args.output))
        else:
            import time as _time
            _t0 = _time.monotonic()
            result = asyncio.run(reconcile(**reconcile_kwargs, ask_user=_ask_user))
            _duration = _time.monotonic() - _t0
            if _notify_fn:
                _total_actions = sum(len(h.get("actions", [])) for h in result.get("history", []))
                _notify_fn(
                    desired_state=args.desired_state,
                    converged=result["converged"],
                    iterations=len(result.get("history", [])),
                    actions_taken=_total_actions,
                    duration=_duration,
                )
            sys.exit(0 if result["converged"] else 1)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)


if __name__ == "__main__":
    cli()
