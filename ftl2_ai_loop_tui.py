"""Textual TUI for ftl2-ai-loop.

Provides a full-screen terminal interface with scrollable log output,
live status bar, and TUI-based ask_user prompts. Launched via --tui flag.
"""

from __future__ import annotations

import asyncio
import io
import re
import sys
import threading
import time

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Input, RichLog, Static


class TUIOutputStream(io.TextIOBase):
    """stdout/stderr replacement that routes print() output to the TUI.

    Buffers partial lines and posts complete lines to the Textual app's
    RichLog widget via call_from_thread. Also parses lines to drive the
    status bar phase display.
    """

    def __init__(self, app: "AILoopApp"):
        super().__init__()
        self._app = app
        self._buf = ""

    def write(self, s: str) -> int:
        if not s:
            return 0
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._app.call_from_thread(self._app.write_log, line)
        return len(s)

    def flush(self) -> None:
        if self._buf:
            line = self._buf
            self._buf = ""
            self._app.call_from_thread(self._app.write_log, line)

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False


# Patterns for extracting phase info from print output
_RE_ITERATION = re.compile(r"=+\s*Iteration\s+(\d+)\s*=+")
_RE_OBSERVING = re.compile(r"Observing\b", re.IGNORECASE)
_RE_ASKING_AI = re.compile(r"Asking AI\b|Deciding\b", re.IGNORECASE)
_RE_EXECUTING = re.compile(r"Executing\b|Running\b", re.IGNORECASE)
_RE_CONVERGED = re.compile(r"Converged\b", re.IGNORECASE)
_RE_NOT_CONVERGED = re.compile(r"NOT converged|did not converge", re.IGNORECASE)
_RE_PLANNING = re.compile(r"Planning\b", re.IGNORECASE)
_RE_INCREMENT = re.compile(r"---\s*Increment\s+(\d+)", re.IGNORECASE)


class AILoopApp(App):
    """Full-screen TUI for ftl2-ai-loop."""

    TITLE = "ftl2-ai-loop"
    CSS = """
    #log {
        height: 1fr;
    }
    #ask-prompt {
        height: auto;
        max-height: 8;
        padding: 0 1;
        display: none;
        color: $warning;
    }
    #ask-input {
        display: none;
    }
    #status-bar {
        height: 1;
        dock: bottom;
        background: $primary-background;
        color: $text;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit_app", "Quit", show=True),
    ]

    def __init__(
        self,
        run_func,
        run_args: tuple,
        run_kwargs: dict,
        desired_state: str = "",
        notify_fn=None,
    ):
        super().__init__()
        self._run_func = run_func
        self._run_args = run_args
        self._run_kwargs = run_kwargs
        self._desired_state = desired_state
        self._notify_fn = notify_fn

        # Status tracking
        self._phase = "Starting"
        self._iteration = 0
        self._start_time = time.monotonic()
        self._finished = False

        # ask_user synchronization
        self._ask_event = threading.Event()
        self._ask_answer: str = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="log", wrap=True, highlight=True, markup=True)
        yield Static("", id="ask-prompt")
        yield Input(placeholder="Type your answer...", id="ask-input")
        yield Static("Starting...", id="status-bar")

    def on_mount(self) -> None:
        if self._desired_state:
            self.sub_title = self._desired_state[:60]
        self._update_status()
        self.set_interval(1.0, self._update_status)
        self._run_worker()

    @property
    def _elapsed(self) -> str:
        seconds = int(time.monotonic() - self._start_time)
        if seconds < 60:
            return f"{seconds}s"
        minutes, secs = divmod(seconds, 60)
        return f"{minutes}m{secs:02d}s"

    def write_log(self, line: str) -> None:
        """Append a line to the RichLog widget. Called from main thread."""
        self._update_phase_from_line(line)
        log = self.query_one("#log", RichLog)
        log.write(line)

    def _update_phase_from_line(self, line: str) -> None:
        """Parse print output to update the status bar phase."""
        m = _RE_ITERATION.search(line)
        if m:
            self._iteration = int(m.group(1))
            self._phase = f"Iter {self._iteration}"
            return
        m = _RE_INCREMENT.search(line)
        if m:
            self._phase = f"Increment {m.group(1)}"
            return
        if _RE_CONVERGED.search(line) and not _RE_NOT_CONVERGED.search(line):
            self._phase = "Converged"
            return
        if _RE_NOT_CONVERGED.search(line):
            self._phase = "Not converged"
            return
        if _RE_OBSERVING.search(line):
            self._phase = f"Iter {self._iteration} | Observing"
            return
        if _RE_ASKING_AI.search(line):
            self._phase = f"Iter {self._iteration} | Asking AI"
            return
        if _RE_EXECUTING.search(line):
            self._phase = f"Iter {self._iteration} | Executing"
            return
        if _RE_PLANNING.search(line):
            self._phase = "Planning"
            return

    def _update_status(self) -> None:
        """Refresh the status bar (called every 1s)."""
        status = self.query_one("#status-bar", Static)
        if self._finished:
            status.update(f"{self._phase} | Done | {self._elapsed}")
        else:
            status.update(f"{self._phase} | {self._elapsed}")

    def _tui_ask_user(self, ask_data: dict) -> str:
        """ask_user backend for TUI mode. Called from worker thread."""
        question = ask_data.get("question", "")
        options = ask_data.get("options", [])

        prompt_parts = [f"? {question}"]
        if options:
            opts = "  ".join(f"[{i}] {opt}" for i, opt in enumerate(options, 1))
            prompt_parts.append(opts)
        prompt_text = "\n".join(prompt_parts)

        self._ask_event.clear()
        self._ask_answer = ""
        self._ask_options = options

        self._app_phase_backup = self._phase
        self._phase = "Waiting for input"

        self.call_from_thread(self._show_ask_input, prompt_text)

        # Block worker thread until user submits
        self._ask_event.wait()

        answer = self._ask_answer
        # Resolve numbered option
        if options and answer.isdigit():
            idx = int(answer) - 1
            if 0 <= idx < len(options):
                answer = options[idx]
        if not answer:
            answer = "(no answer)"

        self.call_from_thread(
            self.write_log, f"  Answer: {answer}"
        )
        return answer

    def _show_ask_input(self, prompt_text: str) -> None:
        """Show the ask prompt and input widgets. Called on main thread."""
        prompt_widget = self.query_one("#ask-prompt", Static)
        input_widget = self.query_one("#ask-input", Input)
        prompt_widget.update(prompt_text)
        prompt_widget.display = True
        input_widget.display = True
        input_widget.value = ""
        # Defer focus until after Textual has re-laid out the newly visible widget
        self.call_after_refresh(input_widget.focus)

    def _hide_ask_input(self) -> None:
        """Hide the ask prompt and input widgets."""
        prompt_widget = self.query_one("#ask-prompt", Static)
        input_widget = self.query_one("#ask-input", Input)
        prompt_widget.display = False
        input_widget.display = False

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user pressing Enter in the ask input."""
        self._ask_answer = event.value.strip()
        self._hide_ask_input()
        self._phase = getattr(self, "_app_phase_backup", "Running")
        self._ask_event.set()

    def _run_worker(self) -> None:
        """Launch the reconcile logic in a worker thread."""

        async def _async_runner():
            try:
                result = await self._run_func(
                    *self._run_args, **self._run_kwargs
                )
                return result
            except Exception as e:
                self.call_from_thread(
                    self.write_log, f"\n[red]Error: {e}[/red]"
                )
                return None

        def _thread_target():
            original_stdout = sys.stdout
            original_stderr = sys.stderr
            stream = TUIOutputStream(self)
            sys.stdout = stream
            sys.stderr = stream
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                result = loop.run_until_complete(_async_runner())
                loop.close()
                stream.flush()
                self._finished = True
                self.call_from_thread(self._on_loop_finished, result)
            except Exception as e:
                stream.flush()
                self._finished = True
                self.call_from_thread(
                    self.write_log, f"\n[red]Error: {e}[/red]"
                )
            finally:
                sys.stdout = original_stdout
                sys.stderr = original_stderr

        t = threading.Thread(target=_thread_target, daemon=True)
        t.start()

    def _on_loop_finished(self, result) -> None:
        """Called on main thread when the worker completes."""
        if self._phase != "Converged" and self._phase != "Not converged":
            self._phase = "Finished"
        self._update_status()
        log = self.query_one("#log", RichLog)
        log.write("")
        log.write("[bold]Run complete. Press q to quit.[/bold]")

    def action_quit_app(self) -> None:
        """Handle q key binding."""
        self.exit()


def run_tui(args, reconcile_kwargs: dict, notify_fn) -> None:
    """Entry point for TUI mode. Determines which function to run and launches the app."""
    from ftl2_ai_loop import (
        reconcile,
        run_continuous,
        run_incremental,
    )

    desired_state = reconcile_kwargs.get("desired_state", "")

    if args.continuous:
        # run_continuous(reconcile_kwargs, delay, ask_user=..., notify=...)
        app = AILoopApp(
            run_func=run_continuous,
            run_args=(reconcile_kwargs, args.delay),
            run_kwargs={"notify": notify_fn},
            desired_state=desired_state,
            notify_fn=notify_fn,
        )
        # Inject TUI ask_user after app is constructed
        app._run_kwargs["ask_user"] = app._tui_ask_user
    elif args.incremental:
        # run_incremental(reconcile_kwargs, plan_file=..., ask_user=..., notify=...)
        app = AILoopApp(
            run_func=run_incremental,
            run_args=(reconcile_kwargs,),
            run_kwargs={
                "plan_file": args.plan,
                "notify": notify_fn,
                "delay": args.delay,
            },
            desired_state=desired_state,
            notify_fn=notify_fn,
        )
        app._run_kwargs["ask_user"] = app._tui_ask_user
    else:
        # Single-shot: reconcile(**reconcile_kwargs, ask_user=...)
        # Wrap reconcile to handle notify after completion
        _original_reconcile = reconcile

        async def _reconcile_with_notify(**kwargs):
            t0 = time.monotonic()
            result = await _original_reconcile(**kwargs)
            duration = time.monotonic() - t0
            if notify_fn:
                total_actions = sum(
                    len(h.get("actions", [])) for h in result.get("history", [])
                )
                notify_fn(
                    desired_state=desired_state,
                    converged=result["converged"],
                    iterations=len(result.get("history", [])),
                    actions_taken=total_actions,
                    duration=duration,
                )
            return result

        app = AILoopApp(
            run_func=_reconcile_with_notify,
            run_args=(),
            run_kwargs=dict(reconcile_kwargs),
            desired_state=desired_state,
            notify_fn=notify_fn,
        )
        app._run_kwargs["ask_user"] = app._tui_ask_user

    app.run()
