#!/usr/bin/env python3
# /// script
# dependencies = [
#     "ftl2 @ git+https://github.com/benthomasson/ftl2",
# ]
# requires-python = ">=3.13"
# ///
"""
Example: Ensure nginx is installed, configured, and running.

This demonstrates the reconciliation loop with custom observers
tailored to checking nginx state.

Usage:
    uv run examples/nginx_example.py
    uv run examples/nginx_example.py --dry-run
    uv run examples/nginx_example.py -i inventory.yml
"""
import asyncio
import sys
from pathlib import Path

# Add parent directory to path for import
sys.path.insert(0, str(Path(__file__).parent.parent))

from ftl2_iac_loop import reconcile


NGINX_OBSERVERS = [
    {"name": "nginx_package", "module": "shell",
     "params": {"cmd": "rpm -q nginx 2>/dev/null || dpkg -l nginx 2>/dev/null || echo 'not installed'"}},
    {"name": "nginx_service", "module": "shell",
     "params": {"cmd": "systemctl is-active nginx 2>/dev/null || echo 'inactive'"}},
    {"name": "nginx_config", "module": "stat",
     "params": {"path": "/etc/nginx/nginx.conf"}},
    {"name": "nginx_port", "module": "shell",
     "params": {"cmd": "ss -tlnp | grep ':80' || echo 'not listening'"}},
]

DESIRED_STATE = "nginx is installed, the service is running and enabled, and it is listening on port 80"


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Ensure nginx is configured and running")
    parser.add_argument("-i", "--inventory", help="Inventory file")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    converged = await reconcile(
        desired_state=DESIRED_STATE,
        inventory=args.inventory,
        observers=NGINX_OBSERVERS,
        max_iterations=5,
        dry_run=args.dry_run,
        quiet=True,
        secret_bindings={
            "community.general.slack": {"token": "SLACK_TOKEN"},
        },
    )
    sys.exit(0 if converged else 1)


if __name__ == "__main__":
    asyncio.run(main())
