# Changelog

All notable changes to `minion.py` from this point forward.

> **Note:** the file was previously called `miniagent.py` and configured via
> `AGENT_BASE_URL` / `AGENT_MODEL` / `AGENT_API_KEY`. Renamed to `minion` /
> `MINION_*` for clarity. The old env vars are silently ignored — set the new
> ones.

## Unreleased

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