# Changelog

All notable changes to `minion.py` from this point forward.

> **Note:** the file was previously called `miniagent.py` and configured via
> `AGENT_BASE_URL` / `AGENT_MODEL` / `AGENT_API_KEY`. Renamed to `minion` /
> `MINION_*` for clarity. The old env vars are silently ignored — set the new
> ones.

## Unreleased

### Added — Esc-to-interrupt during model generation
Press **Esc** while the model is streaming (or during the spinner wait
before the first token) to drop the current stream and return to the
prompt. Partial content is discarded and a synthetic user turn is appended
to context so the model knows what happened on its next turn.

- New `_interrupt_watcher()` daemon: puts stdin in raw mode (ISIG off so
  Ctrl+C still kills the process), polls for bare Esc with a 50ms wait
  for trailing bytes (so it doesn't fire on arrow-key / bracketed-paste
  escape sequences), debounces at 250ms, restores termios on exit.
  Started by `model_turn` before the spinner; signaled to exit in the
  `finally` block. Two events (`_INTERRUPT_EVENT`, `_USER_INTERRUPTED`)
  separate "watcher should exit" from "user actually pressed Esc" so
  cleanup doesn't get confused with a real interrupt.
- `model_turn` checks `_USER_INTERRUPTED` between chunks; on hit it closes
  the stream, prints `↳ interrupted by user (Esc) after N.Ns, N chars
  streamed`, appends a `[User interrupted your previous response with
  Esc. Acknowledge briefly and wait for their next message.]` user turn,
  and returns `False` so the REPL drops to the prompt instead of looping
  into another turn.
- Spinner label changed to `"thinking · esc to interrupt"` so the
  affordance is visible at the wait-for-first-token moment.
- In-flight tool calls (`run_bash`, `write_file`, etc.) are **not**
  cancelled — they run to completion. Hard-stop with Ctrl+C if you need
  one. (Cancel-a-running-tool is a separate follow-up.)

### Added — reasoning-loop guard (`MINION_REASONING_LOOP_SIGNALS`)
Reasoning models sometimes spin in place — they keep saying "let me
implement…" / "start coding…" / "now I'll write the code…" without ever
emitting content or a tool call, burning tokens and stalling the turn.
minion counts how many of those "ready to act" phrases appear during the
reasoning phase and, after the threshold, cuts the stream and appends a
one-shot nudge to the latest user turn telling the model to stop planning
and take a concrete action.

- `REASONING_LOOP_SIGNALS` (tuple of 9 phrases) and
  `REASONING_LOOP_SIGNALS_LIMIT` (default 10, override with
  `MINION_REASONING_LOOP_SIGNALS` env var; `0` disables) module constants.
- New `_ReasoningLoopSignalCounter` class — sliding-window phrase counter
  that scans each streamed `reasoning_content` chunk for new occurrences
  (only counts matches that extend past the previous boundary so we don't
  double-count a phrase split across two chunks).
- `model_turn` instantiates one per turn; on threshold hit it closes the
  stream and appends the `REASONING_LOOP_NUDGE` text to the most recent
  user turn (via `_nudge_current_user_turn`, which creates one if there
  isn't one yet). Prints `↳ cut reasoning loop after N ready-to-act
  signals; nudging implementation` so the cut is visible in the log.
- Only active during the reasoning phase (skipped once `content` /
  `tool_calls` start arriving), so a model that legitimately says "let me
  implement" once before doing it isn't tripped up.

### Added — tool-running spinner
The same `LifeSpinner` that animates during model streaming now also runs
between the cyan `┌─ name` / `│ args` lines and the cyan `└─ result`
lines, with label `"running"`. Without it the screen freezes for the
duration of a slow `run_bash` / `_assess_risk` round-trip / large
`write_file` — the user just sees the green model output end and then
nothing until the result box pops in.

- `LifeSpinner` gained a `label=` constructor arg (`"thinking"` by
  default; `model_turn` uses `"thinking · esc to interrupt"`; tool bodies
  use `"running"`).
- New `_ACTIVE_SPINNER` module-level pointer set by `run_tool()` around
  the tool body. `_confirm()` pauses/resumes it around its own I/O so the
  auto-allow line / `Y/n` prompt don't get clobbered by an animation tick.

### Added — risk-gated approval (`--approval` / `/approval`)
Sits between today's "ask for everything" default and `--yolo`'s "ask for
nothing". Every write / edit / bash call is risk-classified by a single
cheap non-streaming call to the same model before it runs. Levels:
`low` (read-only or trivially reversible), `medium` (modifies state but
contained/reversible), `high` (destructive, hard to reverse, or broad
scope). `APPROVE_LEVEL` is the minimum level that requires approval:

| flag                    | prompts at        | auto-allows       |
| ----------------------- | ----------------- | ----------------- |
| _(default)_             | low + medium + high | —               |
| `--approval medium`     | medium + high     | low               |
| `--approval high`       | high only         | low + medium      |
| `--yolo`                | _(never)_         | everything        |

In `--approval high` mode, `ls`, `cat`, single-file writes, `pip install`,
etc. run without asking; only `rm -rf`, `git push --force`, broad
destructive ops, etc. need a yes/no. The assessment is shown in brackets
next to the prompt (`[risk: HIGH — recursive force delete in /tmp]`) so
the user has context for the decision, and auto-allowed calls print a
one-liner (`↳ auto-allow [low] ls -la (read-only listing)`).

- New module-level `LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2}` and
  `APPROVE_LEVEL` (string or `None` for yolo). CLI flag `--approval <level>`
  parsed in the config block; `--yolo` overrides it and sets `APPROVE_LEVEL
  = None` (no risk call, no prompt).
- New `RISK_SYSTEM` prompt + `_assess_risk(action)` function. Non-streaming
  call with a 15s timeout; logs to `llamacpp.log` under `_purpose: risk`.
  Defensive parse: tries JSON first, falls back to regex-matching a level
  word, falls back to `("high", "<error>")` on any failure — so a broken
  classifier can never silently auto-approve a dangerous command.
- `_confirm(action)` now takes the assessment, shows it inline at the
  prompt (color-coded: dim/yellow/red), and auto-allows with a one-liner
  when `LEVEL_ORDER[assessed] < LEVEL_ORDER[APPROVE_LEVEL]`. YOLO
  short-circuits before the call (no point paying for a call we won't
  act on).
- New REPL command `/approval [level]` — bare shows current setting,
  with arg sets it (`low`/`medium`/`high`/`yolo`; unknown values print a
  yellow warning and leave state unchanged). `/yolo` now prints both
  `yolo=` and `approval=` so the relationship is obvious.
- README updated with the approval-modes table; banner updated to list
  `/approval`.

### Added — base-level traffic log (`llamacpp.log`)
Append-only JSONL record of every byte shipped to / received from the llama.cpp
endpoint. Lives next to `minion.py`.
- `req` events: full outgoing request body (model, messages, tools, stream
  flag) logged before the HTTP call. Fallback (no-tools) requests are logged
  with a `_fallback` marker.
- `resp` events: every raw SSE chunk captured via `model_dump()` as it
  streams in — preserves `reasoning_content`, tool-call deltas, etc. at the
  literal ground-truth level, before any parsing/rendering.
- File opened once at module load with `buffering=1` (line-buffered) so each
  event is flushed immediately; survives crashes without losing recent turns.
- Stream wrapped in a `_LoggingStream` iterator; logging errors are swallowed
  so a disk-full / permission error can never break the agent's response.
- New `import time`; new `_log_event()` helper and `LOG_PATH` constant.

### Added — terminal UI polish
- `LifeSpinner` — a 1-row Conway's Game of Life that runs on a background
  thread while waiting for the first token. Gliders/blinkers actually evolve
  (rows above/below mirror the current row so each cell gets the standard
  8-neighbor count, otherwise a 1-row CA is degenerate). Uses `\033[2K\r` to
  overwrite its own line and `\033[?25l/h` to hide/show the cursor.
- Stats footer at the end of every turn, pulled from llama.cpp's `timings`
  object on the final SSE chunk: `N tok · X.X tok/s · ctx P+C cached · T.Ts wall`.
  Falls back to wall-clock only if the server doesn't send timings.
- Banner at startup with model name, endpoint, and command hints.
- Tool calls now render as a small box (`┌─ name` / `│ args` / `│ output` /
  `└─`) instead of a bare arrow. Output truncated to 800 chars in the display;
  the model still receives the full result via the messages array.
- New ANSI helpers: `MAGENTA`, `BOLD`, `CLEAR_LINE`, `HIDE_CURSOR`,
  `SHOW_CURSOR`.

### Changed
- `model_turn` now starts the spinner before opening the stream and stops it
  the moment the first chunk arrives (or in a `finally` if the stream errors).
- Tool output is line-wrapped into the box rather than dumped raw.
- `_log_event("resp", ...)` in `compress()` wraps the `model_dump()` call in
  `try/except` to match the streaming path's "never let logging break the
  agent" pattern — a non-pydantic response object won't crash the summary
  call anymore.

### Fixed — `/compress` could leave an orphan tool message in the kept tail
If the last `COMPRESS_KEEP` turns ended with a half-finished tool-call
sequence (e.g. the assistant called a tool but the result was the last turn,
or — more commonly — the assistant tool-call turn landed in `head` and only
its `tool` result made it into `tail`), llama.cpp's chat template would raise
`Message has tool role, but there was no previous assistant message with a
tool call!` on the very next request. `compress()` now walks the front of the
tail and drops any leading `tool` or unmatched `assistant(tool_calls)` turn
before splicing the summary in. The `summarized_n` count is bumped by the
number of extra turns absorbed so the user-visible footer stays honest.

### Added — multi-line chatbox input
Replaces the bare `input()` prompt with a framed, multi-line editor in the
terminal. Prompt, streamed model output, tool confirmations, and the next
prompt all stay in the normal terminal scrollback (no alternate screen) to
avoid garbling the REPL after submit.
- Enter submits; Alt+Enter / Ctrl+J insert newlines.
- Bracketed-paste mode preserves pasted newlines verbatim and strips a
  trailing newline so pasting never accidentally submits.
- Up/Down navigate past submissions; Left/Right move within the current
  line; Home/End jump to line start/end; Ctrl+U clears the line;
  Ctrl+C cancels.
- Long lines word-wrap visually inside the box; the buffer stays one logical
  string (newlines preserved) so the model sees the real text.
- Falls back to plain `input()` when stdin/stdout is not a TTY.
- New `read_multiline()` public entry point and `_chatbox_raw()` /
  `_chatbox_fallback()` helpers; new imports `select`, `shutil`, `termios`.

### Added — `/compress` context summarization
New REPL command. Asks the model to summarize everything except the system
prompt and the last `COMPRESS_KEEP=2` turns, then splices the summary in as a
single labeled user turn. Useful when the context window is filling up but
you want to keep working without `/reset`.
- Non-streaming summary call (spinner would be visual noise for a one-shot;
  one `model_dump` of the response is logged to `llamacpp.log` with a
  `_purpose: compress` marker so the summary is recoverable from the log).
- Confirmation prompt (skipped under `/yolo`).
- One-line stats footer: `compressed N turns → 1 summary (X chars), kept last K verbatim`.
- Header on the summary turn: `[Compressed context — N earlier turns
  summarized; last K turns kept verbatim]` so the model knows what it's
  reading on subsequent turns.
- Nothing-to-compress short-circuit: if the body has ≤ `COMPRESS_KEEP` turns,
  prints `nothing to compress (N turns in context)` and bails.
- Failure modes (APIConnectionError, generic API error, empty summary) leave
  `messages` untouched and print a one-line error — never half-compress.
- Tool-call turns and tool-result turns are rendered into the summary prompt
  with their content truncated to 2k chars each, so a giant `read_file`
  doesn't blow up the summarization call itself. Assistant tool-call turns
  are rendered as `[assistant] → tool_name(args)` for readability.
- New `compress(messages, keep=COMPRESS_KEEP)` function (returns
  `(kept_n, summarized_n, summary_chars)` or `None` on failure).
- New `COMPRESS_KEEP = 2` module constant.
- Banner and module docstring updated to mention `/compress`.