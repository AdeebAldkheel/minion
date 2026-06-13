# minion

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

| command      | what it does                                        |
| ------------ | --------------------------------------------------- |
| `/yolo`      | toggle auto-approve for writes and bash             |
| `/compress`  | summarize older turns into one, keep last 2 verbatim |
| `/reset`     | clear conversation, keep system prompt              |
| `/quit`      | exit                                                |

Pass `--yolo` on launch to start in auto-approve mode.

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