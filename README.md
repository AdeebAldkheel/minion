# minion

> Entirely written by minion running on MiniMax-M3 — including this README.
> The agent also manages this repo: commits, pushes, and edits are made
> through its own `write_file` / `edit_file` / `run_bash` tools.

A tiny single-file coding agent focused on MiniMax-M3. Talks to any OpenAI-compatible server
(llama.cpp, vLLM, SGLang). Built for self-hosted models whose native tool
calling isn't fully wired up — falls back to parsing `<tool_call>…</tool_call>`
tags out of the text if the server doesn't expose them.

Designed against [llama.cpp serving MiniMax-M3](https://github.com/ggml-org/llama.cpp/pull/24523#issuecomment-4697838784),
which streams a separate `reasoning_content` field before the answer.

Model in use: Unsloth's: MiniMax-M3-UD-Q4_K_XL

```
pip install openai
export MINION_BASE_URL=http://localhost:8080/v1
export MINION_MODEL=your-model-name
export MINION_API_KEY=sk-noop        # any string; local servers ignore it
python minion.py
```

If `MINION_MODEL` is unset, minion asks the server what it's serving.

## Commands

| command             | what it does                                        |
| ------------------- | --------------------------------------------------- |
| `/yolo`             | toggle auto-approve for writes and bash             |
| `/approval [level]` | show or set risk threshold (`low`/`medium`/`high`/`yolo`) |
| `/compress`         | summarize older turns into one, keep last 2 verbatim |
| `/reset`            | clear conversation, keep system prompt              |
| `/quit`             | exit                                                |

## Interrupting the model

Press **Esc** at any point during generation to stop the model and drop
back to the prompt. The current stream is closed, partial output is
discarded, and a synthetic `"you were interrupted"` user turn is appended
to context so the model knows what happened when you send your next
message. In-flight tool calls (e.g. `run_bash`) are **not** cancelled —
they run to completion; Ctrl+C kills the whole process if you need a
hard stop.

## Reasoning-loop guard

Reasoning models sometimes spin in place: they keep saying "let me
implement…" / "start coding…" / "now I'll write the code…" without
actually emitting content or a tool call. minion counts how many of those
"ready to act" phrases show up during the reasoning phase and, after
`MINION_REASONING_LOOP_SIGNALS` (default **10**) of them, closes the
stream and appends a one-shot nudge to the latest user turn telling the
model to stop planning and take a concrete action. Set the env var to `0`
to disable, or to a smaller number (e.g. `5`) for a more aggressive cut.
The cut prints `↳ cut reasoning loop after N ready-to-act signals; nudging
implementation` so it's visible in the log.

## Approval modes

Every write / edit / bash call is risk-classified by a single cheap model
call before it runs. Levels: `low` (read-only or trivially reversible),
`medium` (modifies state but contained/reversible), `high` (destructive,
hard to reverse, or broad scope). The threshold is the minimum level that
requires approval:

| flag                    | prompts at        | auto-allows       |
| ----------------------- | ----------------- | ----------------- |
| _(default)_             | low + medium + high | —               |
| `--approval medium`     | medium + high     | low               |
| `--approval high`       | high only         | low + medium      |
| `--yolo`                | _(never)_         | everything        |

In `--approval high` mode, `ls`, `cat`, single-file writes, `pip install`,
etc. run without asking; only `rm -rf`, `git push --force`, broad
destructive ops, etc. need a yes/no. The risk assessment is shown in
brackets next to the prompt so you have context for the decision
(`allow rm -rf /tmp/foo? [risk: HIGH — recursive force delete in /tmp] [Y/n]`),
and auto-allowed calls print a one-liner
(`↳ auto-allow [low] ls -la (read-only listing)`).

The classifier is the same model you run the agent on, called with a short
JSON-output prompt. If the call fails or returns garbage, the action is
treated as `high` (always prompts) so we err on the side of asking. YOLO
mode skips the classifier entirely — no point paying for a call we won't
act on.

Pass `--yolo` on launch to start in never-prompt mode, or
`--approval <low|medium|high>` to start with a non-default threshold
(`--approval high` is the common one — auto-allows `ls`, `cat`, single-file
writes, `pip install`, etc., only prompts on `rm -rf`-class actions).

## Tools

| tool        | args                  | notes                       |
| ----------- | --------------------- | --------------------------- |
| `read_file` | `path`                |                             |
| `write_file`| `path`, `content`     | requires confirmation       |
| `edit_file` | `path`, `old`, `new`  | `old` must match exactly once |
| `list_dir`  | `path`                | defaults to `.`             |
| `run_bash`  | `command`             | requires confirmation       |

Every request and streamed SSE chunk is appended to `llamacpp.log` next to
the script (JSONL). Useful for debugging what the model actually saw.