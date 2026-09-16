# Terminal handoff experiment

## Decision: keep tmux optional, pending native and macOS checks

The Linux prototype demonstrates dashboard → interactive process → dashboard in
one terminal, while async work continues. There is no demonstrated terminal-mechanics
reason to require tmux. This is a provisional decision: real Codex/Claude conversation
continuity and macOS execution remain acceptance gates before the full dashboard.

This experiment implements no forge operations, reviewer sandbox, managed native
sessions, or production cleanup. It does not establish the background privilege
boundary in SPEC.md §13.1. The default fake agent never calls a model or accesses a forge.

## Try it

From an interactive Linux or macOS terminal:

```sh
make setup
make handoff
```

Select a synthetic PR with the arrow keys, press `v`, and type `exit` to return.
Press `v` again: the fake's session ID stays the same and its resume count increases.
Any other text displays a remembered synthetic review marker. The fake session
exists only for this invocation of the prototype; this is not restart persistence.

Try Ctrl+C, `crash`, or `raw-crash` inside the fake. The last deliberately exits
with damaged terminal modes. `f` on the dashboard tries a nonexistent executable.
In each case, the dashboard should return with its selected row intact. Its status
shows how many background ticks occurred while attached. Press `q` on the dashboard
to quit; the temporary fake session and captured background log are removed.

An independent subprocess continuously writes to a log while the heartbeat task
continues on the dashboard's asyncio loop. Neither should print over the agent.
To retain diagnostic events (timestamps, counts, return codes; no agent transcript):

```sh
make handoff ARGS="--events /tmp/diffbutler-handoff-events.jsonl"
```

## Why it works

The UI uses Textual's public [`App.suspend()`](https://textual.textualize.io/api/app/#textual.app.App.suspend)
context manager to release and restore its terminal driver. An asynchronous worker
awaits the child so other tasks continue. No nested tmux instance or embedded terminal
emulator is involved.

The foreground runner creates a separate process group in the same terminal session.
A small launch gate waits on a pipe until `tcsetpgrp` has assigned the terminal to
that group, then replaces itself with the requested command. This prevents an eager
agent from reading before it owns the terminal. Terminal-generated Ctrl+C reaches
the child group, rather than canceling the dashboard's asyncio loop.

On return, a `finally` block removes leftover processes in that owned group,
reclaims the terminal, and restores saved termios settings. Textual then redraws.
The installed Textual 8.2.8 implementation resumes its driver after its context
manager's `yield`, rather than in a `finally`: exceptions must be caught inside
the suspension block and handled or re-raised after it exits normally.

The UI admits only one handoff at a time. This guard is an experiment, not the
durable session/workspace leases required by the production workflow.

## Evidence recorded on Linux

Environment: Python 3.14.7, Textual 8.2.8. `tests/test_handoff_pty.py` starts the real
Textual Linux driver in a fresh controlling pseudo-terminal, rather than using
Textual's headless driver. Tests run offline as part of `make check`.

| Scenario | Result |
|---|---|
| Attach, type, exit, attach again | Passed; fake conversation ID/state retained |
| Select second row before attachment | Passed; same row after return |
| Heartbeat during attachment | Passed; event count advances |
| Independent background subprocess | Passed; log grows, output absent from terminal |
| Terminal-generated Ctrl+C | Passed; agent exits, dashboard survives and can reattach |
| Exit status 23 / raw-mode crash status 24 | Passed; dashboard recovers and can reattach |
| Missing executable | Passed; status 127, dashboard remains usable |
| Runner exception / worker cancellation while suspended | Passed; Textual input resumes |
| Agent terminated with SIGTERM | Passed; dashboard can reattach |
| Final dashboard exit | Passed; original terminal settings restored |
| Native Codex session continuity | Not yet exercised; installed CLI help checked |
| Native Claude session continuity | Not exercised; Claude is not installed here |
| macOS | Not run locally; tests included in the existing macOS CI job |

## Native validation procedure

The prototype accepts literal command arguments after `--`. It inherits the normal
foreground environment and never adds an initial prompt. Resume an explicitly
chosen session; do not use a latest-session shortcut that might attach unrelated work.

Installed Codex CLI: **0.98.0**. Its local `exec --help` and `resume --help` confirm
`exec --json`, `--sandbox read-only`, and `resume SESSION_ID` exist. Help output
does not prove background-to-interactive continuity or credential isolation.

To check an existing synthetic Codex session interactively:

```sh
make handoff ARGS="-- codex resume YOUR_SYNTHETIC_SESSION_ID"
```

The remaining native acceptance test for **each** supported CLI is:

1. Verify installed version, noninteractive invocation, session ID output, and
   interactive resume syntax. Claude syntax must be checked when it is installed.
2. In a disposable source directory, explicitly start a small synthetic source-only
   review with a distinctive conversation marker; capture the actual backend session
   ID. This is a separate manual integration check and may use model credits.
3. Start the prototype with that exact resume argv and the same backend session
   storage. Confirm the previous review is visible and the conversation continues.
4. Exit and reattach; quit/restart the prototype and resume the same backend ID.
   Confirm no new initial review starts merely by resuming.
5. Exercise Ctrl+C, native exit, resize, and a terminated native process. Verify
   restoration, selection, heartbeat progress, and output isolation.
6. Repeat on macOS and record CLI/OS versions and actual outcomes here.

Do not copy personal credentials or transcripts into the repository to perform
these checks. The production permission transition still needs separate isolation
tests; the ability to resume alone does not establish that boundary.

## Remaining terminal limits

Resize behavior with native full-screen agents, SSH terminals, job-control
suspension (Ctrl+Z), and abrupt dashboard termination have not been validated.
The prototype waits for child exit; a stopped child must be continued or terminated
externally. Processes that deliberately detach into another session are outside
its process-group cleanup. Windows is outside the initial platform scope.

If a native terminal ownership/restoration problem cannot be resolved, compare tmux
using the same acceptance cases. A backend's inability to resume the same conversation
is a separate adapter limitation that tmux by itself does not fix.
