# ftl2-ai-loop Roadmap

## Near-term

### Progressive Self-Hardening
The core economic thesis: AI calls are expensive, rules are free. As the system runs, it writes rules for recurring patterns. Over time, routine operations are handled by rules and only novel situations call the AI. Day 1: 100 AI calls. Day 30: 1 AI call for review.

### Drift Detection
Intentionally break infrastructure, verify the loop detects and fixes it without human intervention. Run the same desired state repeatedly, observe rule generation covering more cases each time.

### Policy Denial Feedback
When the policy engine denies an action, surface the denial in the AI prompt so the loop can adapt its approach (use a different module, skip the action) instead of just failing.

## Medium-term

### TUI
Dual-mode Textual TUI with hotkey swap:
- **Scrollback mode**: full-screen scrolling log of all events and actions
- **Status mode**: dashboard showing current run/iteration, active actions, past run list with drill-down, timing and failure counts

### Safety Controls
Module allowlists (restrict what the AI can call). Dry-run first pass before destructive actions. Human approval workflows for specific module types or environments.

### Durable-Rules Engine
Graduate from simple Python condition/action pairs to a durable-rules engine when complexity demands it. Handle: state machines, correlated events across multiple hosts, complex temporal patterns. AI writes rules in either format.

## Longer-term

### Multi-Agent Coordination
Multiple AI loops managing different aspects of the same infrastructure (networking, compute, storage) with shared state and conflict resolution.

### Cost Analytics
Track AI call costs per run, show cost reduction over time as rules accumulate. Budget limits per reconciliation cycle.
