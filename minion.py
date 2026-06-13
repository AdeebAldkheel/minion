#!/usr/bin/env python3
"""minion — a deliberately tiny coding agent for self-hosted models.

One file, one dep (`openai`), no TUI framework. Points at any OpenAI-compatible
endpoint (vLLM / llama.cpp / SGLang). Survives models whose native tool-calling
isn't wired up yet by falling back to parsing <tool_call>...</tool_call> tags
out of the text — the convention most open models (Hermes/Qwen/Nemotron) emit.

  pip install openai
  export MINION_BASE_URL=http://localhost:8000/v1   # your served endpoint
  export MINION_MODEL=your-model-name
  export MINION_API_KEY=sk-noop                    # any string; local servers ignore it
  python minion.py

Toggles in-session: /yolo (skip confirms)  /compress  /reset  /quit
"""
import json
import os
import random
import re
import subprocess
import sys
import threading
import time

from openai import OpenAI, APIConnectionError

# --- config -----------------------------------------------------------------
client = OpenAI(
    base_url=os.environ.get("MINION_BASE_URL", "http://localhost:8080/v1"),
    api_key=os.environ.get("MINION_API_KEY", "sk-noop"),
)
YOLO = "--yolo" in sys.argv  # auto-approve writes/bash


def resolve_model():
    """MINION_MODEL if set, else ask the server what it's actually serving."""
    if os.environ.get("MINION_MODEL"):
        return os.environ["MINION_MODEL"]
    try:
        return client.models.list().data[0].id
    except Exception:
        return "local-model"  # server down — main() will report it cleanly


MODEL = resolve_model()

# --- base-level traffic log -------------------------------------------------
# Append-only JSONL record of every byte we ship to / receive from the server.
# Lives next to this script so it's easy to find; rotate by hand if it gets big.
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llamacpp.log")
_llog = open(LOG_PATH, "a", buffering=1)  # line-buffered; flushes per write


def _log_event(direction, payload):
    """direction: 'req' (outgoing) or 'resp' (incoming SSE chunk)."""
    _llog.write(json.dumps({"ts": time.time(), "dir": direction, "data": payload}) + "\n")

# --- ANSI -------------------------------------------------------------------
DIM, CYAN, GREEN, YELLOW, RED, MAGENTA, BOLD, RESET = (
    "\033[2m", "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[35m",
    "\033[1m", "\033[0m",
)
CLEAR_LINE = "\033[2K\r"   # erase entire line, return cursor to col 0
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"


# --- waiting animation (tiny Conway's Game of Life) -------------------------
# A spinner is boring. A 1-row toroidal Game of Life is the same shape on screen
# (one line of cells) but actually does something — patterns glide, blinkers
# flash, gliders crawl. Runs in a background thread; the main loop kills it
# the instant the first token arrives.
_GOL_W = 24
_GOL_ALIVE = "█"
_GOL_DEAD = "·"
_GOL_GLIDER = {(0, 0), (1, 1), (2, 1), (0, 2), (1, 0)}  # 5-cell, period-4


class LifeSpinner:
    def __init__(self, width=_GOL_W, tick_ms=90):
        self.w = width
        self.tick = tick_ms / 1000
        self._stop = threading.Event()
        self._t = None

    def _seed(self):
        row = [0] * self.w
        x = random.randrange(self.w)
        for dx, _ in _GOL_GLIDER:
            row[(x + dx) % self.w] = 1
        for _ in range(2):
            x = random.randrange(self.w)
            row[x] = row[(x + 1) % self.w] = row[(x + 2) % self.w] = 1
        for _ in range(self.w // 6):
            row[random.randrange(self.w)] = 1
        return row

    def _step(self, row):
        # A 1-row GoL is degenerate (cells have only 2 neighbors). Cheat: treat
        # the row as the middle of a 3-row toroidal world where the rows above
        # and below mirror the current one. Gives every cell the standard 8
        # neighbors, so gliders/blinkers/etc. actually work.
        w, above, below, nxt = self.w, row, row, [0] * self.w
        for x in range(w):
            n = (above[(x - 1) % w] + above[x] + above[(x + 1) % w] +
                 row[(x - 1) % w]                   + row[(x + 1) % w] +
                 below[(x - 1) % w] + below[x] + below[(x + 1) % w])
            cur = row[x]
            nxt[x] = 1 if (cur and n in (2, 3)) or (not cur and n == 3) else 0
        return nxt

    def _run(self):
        sys.stdout.write(HIDE_CURSOR)
        try:
            row = self._seed()
            # initial render — also reserve the line so subsequent prints don't shift things
            sys.stdout.write(CLEAR_LINE + "  " + DIM + "thinking " + RESET +
                             "".join(_GOL_ALIVE if c else _GOL_DEAD for c in row))
            sys.stdout.flush()
            while not self._stop.is_set():
                time.sleep(self.tick)
                if self._stop.is_set():
                    break
                row = self._step(row)
                sys.stdout.write(CLEAR_LINE + "  " + DIM + "thinking " + RESET +
                                 "".join(_GOL_ALIVE if c else _GOL_DEAD for c in row))
                sys.stdout.flush()
        finally:
            # wipe the spinner line and restore cursor
            sys.stdout.write(CLEAR_LINE + SHOW_CURSOR)
            sys.stdout.flush()

    def start(self):
        self._stop.clear()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=0.5)
            self._t = None


# --- tools ------------------------------------------------------------------
def read_file(path, **_):
    with open(path) as f:
        return f.read()


def write_file(path, content, **_):
    if not _confirm(f"write {path} ({len(content)} bytes)"):
        return "DENIED by user"
    with open(path, "w") as f:
        f.write(content)
    return f"wrote {len(content)} bytes to {path}"


def edit_file(path, old, new, **_):
    with open(path) as f:
        src = f.read()
    if src.count(old) != 1:
        return f"ERROR: `old` matched {src.count(old)} times (need exactly 1)"
    if not _confirm(f"edit {path}"):
        return "DENIED by user"
    with open(path, "w") as f:
        f.write(src.replace(old, new))
    return f"edited {path}"


def list_dir(path=".", **_):
    return "\n".join(sorted(os.listdir(path)))


def run_bash(command, **_):
    if not _confirm(f"run: {command}"):
        return "DENIED by user"
    r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    return f"[exit {r.returncode}]\n{out[:8000]}"


DISPATCH = {
    "read_file": read_file, "write_file": write_file, "edit_file": edit_file,
    "list_dir": list_dir, "run_bash": run_bash,
}

TOOLS = [
    {"type": "function", "function": {"name": "read_file", "description": "Read a file's contents",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write (overwrite) a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "edit_file", "description": "Replace one exact occurrence of `old` with `new` in a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {"name": "list_dir", "description": "List a directory",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "run_bash", "description": "Run a shell command",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
]

SYSTEM = """You are a terminal coding agent working in the user's current directory.
Use the provided tools to inspect and modify code. Take one concrete step at a time.

If your runtime does NOT support native tool calls, emit a call as text exactly like:
<tool_call>{"name": "read_file", "arguments": {"path": "foo.py"}}</tool_call>
Emit nothing after a tool call; wait for the Observation. When the task is done, reply in plain prose."""


def _confirm(action):
    if YOLO:
        return True
    ans = input(f"{YELLOW}  allow {action}? [Y/n] {RESET}").strip().lower()
    return ans != "n"


# --- text-fallback parsing --------------------------------------------------
TOOL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def parse_text_calls(content):
    """Pull <tool_call>{...}</tool_call> blocks out of model text."""
    calls = []
    for m in TOOL_TAG.finditer(content or ""):
        try:
            obj = json.loads(m.group(1))
            calls.append((obj["name"], obj.get("arguments", {})))
        except (json.JSONDecodeError, KeyError):
            pass
    return calls


def run_tool(name, args):
    fn = DISPATCH.get(name)
    if not fn:
        return f"ERROR: unknown tool {name}"
    # newline so the tool arrow gets its own line — streamed text uses end=""
    # and would otherwise run straight into the indicator
    arg_preview = json.dumps(args)
    if len(arg_preview) > 120:
        arg_preview = arg_preview[:117] + "..."
    print(f"\n{CYAN}  ┌─ {name}{RESET}")
    print(f"{CYAN}  │ {RESET}{DIM}{arg_preview}{RESET}")
    try:
        result = fn(**args)
    except Exception as e:  # noqa: BLE001 — surface any tool error back to the model
        result = f"ERROR: {type(e).__name__}: {e}"
    # box the result; truncate absurdly long output for readability (model still
    # gets the full thing via the messages array)
    preview = result if len(result) < 800 else result[:800] + f"\n... [{len(result) - 800} more chars]"
    for line in preview.splitlines():
        print(f"{CYAN}  │ {RESET}{line}")
    print(f"{CYAN}  └─{RESET}")
    return result


def open_stream(messages):
    """Open a streaming completion. Retries without tools= if the server rejects
    that param; returns None (after a friendly message) on connection/API failure."""
    try:
        try:
            _log_event("req", {"model": MODEL, "messages": messages, "tools": TOOLS, "stream": True})
            stream = client.chat.completions.create(
                model=MODEL, messages=messages, tools=TOOLS, stream=True)
        except APIConnectionError:
            raise  # server unreachable — don't bother retrying without tools
        except Exception:  # reachable but rejected tools= → text-protocol fallback
            _log_event("req", {"model": MODEL, "messages": messages, "stream": True, "_fallback": "no-tools"})
            stream = client.chat.completions.create(
                model=MODEL, messages=messages, stream=True)
        # Wrap the stream so every chunk is captured to the log on its way out.
        return _LoggingStream(stream, _llog)
    except APIConnectionError:
        print(f"{RED}  ✗ can't reach {client.base_url} — is the server up? "
              f"Set MINION_BASE_URL (and MINION_MODEL) to point at it.{RESET}")
    except Exception as e:
        print(f"{RED}  ✗ API error: {type(e).__name__}: {e}{RESET}")
    return None


# --- context compression ----------------------------------------------------
# Summarize the older turns of `messages` into a single user-role turn, keeping
# the system prompt and the last K turns verbatim. Frees context without losing
# the model's grip on what it was just doing.
COMPRESS_KEEP = 2  # how many recent turns to leave untouched


def compress(messages, keep=COMPRESS_KEEP):
    """Ask the model to summarize everything except system + last `keep` turns.

    Mutates `messages` in place on success: replaces the middle slice with a
    single user-role summary turn. Returns (kept_n, summarized_n, summary_chars)
    or None on failure (in which case `messages` is untouched).

    Non-streaming on purpose — we want the whole summary before splicing it in,
    and a spinner for a one-shot summary would be visual noise.
    """
    # Layout: [system?, ..., user, assistant, tool, ..., user, assistant(tool_calls)?, ...]
    # We assume messages[0] is the system prompt (matches how main() builds it).
    # Anything before the "tail" we want to summarize; the tail stays verbatim.
    if len(messages) <= 1 + keep:
        return None  # nothing to compress

    sys_msg = messages[0] if messages and messages[0].get("role") == "system" else None
    body = messages[1:] if sys_msg else messages
    if len(body) <= keep:
        return None

    head, tail = body[:-keep], body[-keep:]
    summarized_n = len(head)

    # The tail must start on a turn the chat template can render. A `tool` turn
    # with no preceding assistant(tool_calls) parent — or an assistant(tool_calls)
    # turn whose result got cut off into `head` — makes llama.cpp's Jinja template
    # raise "Message has tool role, but there was no previous assistant message
    # with a tool call!". Walk from the front of the tail and drop any leading
    # tool/half-tool-call turns until we land on something safe (user, plain
    # assistant, or system). Bump `summarized_n` so the user-visible count stays
    # honest about how many turns actually got folded into the summary.
    while tail and tail[0].get("role") in ("tool", "assistant"):
        first = tail[0]
        if first.get("role") == "tool":
            tail = tail[1:]
            summarized_n += 1
            continue
        # assistant: only safe if it has NO tool_calls, OR every tool_call has
        # its matching tool result later in the tail
        if first.get("tool_calls"):
            ids = {tc["id"] for tc in first["tool_calls"]}
            seen = set()
            for m in tail[1:]:
                tcid = m.get("tool_call_id")
                if m.get("role") == "tool" and tcid:
                    seen.add(tcid)
            if ids - seen:
                tail = tail[1:]
                summarized_n += 1
                continue
        break

    # Render the head as plain text the model can summarize. Tool outputs are
    # the bulkiest part of a real session — include them but truncate each one
    # so a single huge read_file doesn't blow up the summary prompt itself.
    def _render(msgs):
        out = []
        for m in msgs:
            role = m.get("role", "?")
            content = m.get("content")
            if content is None and m.get("tool_calls"):
                # assistant tool-call turn — show the calls so the summary knows what ran
                calls = ", ".join(
                    f"{c['function']['name']}({c['function']['arguments']})"
                    for c in m["tool_calls"]
                )
                out.append(f"[{role}] → {calls}")
            elif isinstance(content, list):
                # some servers return content as a list of parts; flatten it
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
                out.append(f"[{role}] {content[:2000]}")
            else:
                out.append(f"[{role}] {(content or '')[:2000]}")
        return "\n\n".join(out)

    summary_prompt = (
        "Summarize the following conversation history for context retention. "
        "Preserve: the original user goal/task, key decisions made, file paths "
        "and identifiers touched, current state of any in-progress work, and "
        "any unresolved questions. Drop: raw tool outputs, full file contents, "
        "and verbose back-and-forth — keep it dense and information-rich. "
        "Write in the same language as the conversation. Output ONLY the "
        "summary, no preamble.\n\n"
        f"---\n{_render(head)}\n---"
    )

    payload = [{"role": "user", "content": summary_prompt}]
    try:
        _log_event("req", {"model": MODEL, "messages": payload, "stream": False, "_purpose": "compress"})
        resp = client.chat.completions.create(model=MODEL, messages=payload, stream=False)
        try:
            _log_event("resp", {"_purpose": "compress", "data": resp.model_dump()})
        except Exception:
            pass  # never let logging break the summary call
    except APIConnectionError:
        print(f"{RED}  ✗ can't reach {client.base_url} — context unchanged{RESET}")
        return None
    except Exception as e:
        print(f"{RED}  ✗ compress failed: {type(e).__name__}: {e}{RESET}")
        return None

    summary = (resp.choices[0].message.content or "").strip()
    if not summary:
        print(f"{RED}  ✗ compress returned empty summary — context unchanged{RESET}")
        return None

    header = f"[Compressed context — {summarized_n} earlier turns summarized; last {keep} turns kept verbatim]"
    new_mid = [{"role": "user", "content": f"{header}\n\n{summary}"}]
    messages[:] = ([sys_msg] if sys_msg else []) + new_mid + tail
    return len(tail), summarized_n, len(summary)


class _LoggingStream:
    """Iterator wrapper that tees each SSE chunk to llamacpp.log before yielding.
    Uses model_dump so we capture the chunk's full structure (incl. reasoning_content)."""
    def __init__(self, inner, log_file):
        self._inner = inner
        self._log = log_file

    def __iter__(self):
        for chunk in self._inner:
            try:
                self._log.write(json.dumps({"ts": time.time(), "dir": "resp",
                                             "data": chunk.model_dump()}) + "\n")
            except Exception:
                pass  # never let logging break the stream
            yield chunk


# --- one model turn (streamed), returns True if it called tools -------------
def model_turn(messages):
    stream = open_stream(messages)
    if stream is None:
        return False  # error already reported; REPL continues

    spinner = LifeSpinner()
    spinner.start()
    t0 = time.time()
    content, tcs, mode = [], {}, None
    timings = None
    try:
        for chunk in stream:
            # first byte in: kill the spinner, let the real output take this line
            if spinner._t is not None:
                spinner.stop()
            d = chunk.choices[0].delta
            # llama.cpp attaches a `timings` object to the final chunk — grab it
            # for the stats footer. It's the only place we get real tok/s numbers
            # (streaming `usage` is always null on llama.cpp).
            extra = getattr(chunk, "model_extra", None) or {}
            if "timings" in extra:
                timings = extra["timings"]
            # reasoning models (e.g. MiniMax-M3) stream a separate reasoning_content
            # field before content/tool_calls. Header + dim text, then a blank line
            # so the green "actual response" always lands on its own row (reasoning
            # from the model often doesn't end in \n — without the gap it would
            # run straight into the answer).
            rc = getattr(d, "reasoning_content", None) or (d.model_extra or {}).get("reasoning_content")
            if rc:
                if mode != "think":
                    print(f"{DIM}  ── reasoning ──{RESET}")
                    mode = "think"
                print(f"{DIM}{rc}{RESET}", end="", flush=True)
            if d.content:
                if mode == "think":
                    # close out the reasoning block; newline guarantees the green
                    # answer starts on a fresh line below the dim text
                    print()  # end the current reasoning line
                    print(f"{DIM}  ──────────────{RESET}")
                print(f"{GREEN}", end="")
                mode = "say"
                print(d.content, end="", flush=True)
                content.append(d.content)
            for tc in (d.tool_calls or []):
                # if we were mid-reasoning when tools kicked in, close it out so
                # the cyan tool box (which starts with its own \n) gets a clean line
                if mode == "think":
                    print()
                    print(f"{DIM}  ──────────────{RESET}")
                    mode = None
                s = tcs.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                if tc.id:
                    s["id"] = tc.id
                if tc.function and tc.function.name:
                    s["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    s["args"] += tc.function.arguments
    finally:
        spinner.stop()
    # reasoning-only turn (no content, no tool_calls) — close out the block so
    # the stats footer doesn't run straight into the dim reasoning text
    if mode == "think":
        print()
        print(f"{DIM}  ──────────────{RESET}")
    print(RESET)
    text = "".join(content)
    elapsed = time.time() - t0

    # stats footer — only if llama.cpp gave us timings; otherwise fall back to wall-clock
    if timings and timings.get("predicted_n"):
        prompt_n = timings.get("prompt_n", 0)
        cache_n = timings.get("cache_n", 0)
        gen_n = timings["predicted_n"]
        tps = timings.get("predicted_per_second", 0)
        ctx = f"ctx {prompt_n}+{cache_n} cached" if cache_n else f"ctx {prompt_n}"
        print(f"{DIM}  └ {gen_n} tok · {tps:5.1f} tok/s · {ctx} · {elapsed:4.1f}s wall{RESET}")
    elif text or tcs:
        print(f"{DIM}  └ {elapsed:4.1f}s wall{RESET}")

    if tcs:  # native tool-calling path
        ordered = [tcs[i] for i in sorted(tcs)]
        messages.append({"role": "assistant", "content": text or None, "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"]}}
            for c in ordered]})
        for c in ordered:
            try:
                args = json.loads(c["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": run_tool(c["name"], args)})
        return True

    calls = parse_text_calls(text)  # text-fallback path
    if calls:
        messages.append({"role": "assistant", "content": text})
        obs = [f"Observation ({n}): {run_tool(n, a)}" for n, a in calls]
        messages.append({"role": "user", "content": "\n".join(obs)})
        return True

    messages.append({"role": "assistant", "content": text})
    return False


# --- repl -------------------------------------------------------------------
BANNER = f"""{BOLD}minion{RESET} {DIM}·{RESET} {CYAN}{MODEL}{RESET}
{DIM}  {client.base_url}  ·  /yolo /compress /reset /quit  ·  log → llamacpp.log{RESET}"""


def main():
    print(BANNER)
    print()
    messages = [{"role": "system", "content": SYSTEM}]
    while True:
        try:
            user = input(f"{CYAN}you ›{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user == "/quit":
            break
        if user == "/yolo":
            globals()["YOLO"] = not YOLO
            print(f"{DIM}  yolo={YOLO}{RESET}")
            continue
        if user == "/reset":
            messages = [{"role": "system", "content": SYSTEM}]
            print(f"{DIM}  context cleared{RESET}")
            continue
        if user == "/compress":
            # nothing to compress if we're under (system + KEEP) turns
            body_len = len(messages) - (1 if messages and messages[0].get("role") == "system" else 0)
            if body_len <= COMPRESS_KEEP:
                print(f"{DIM}  nothing to compress ({body_len} turn{'s' if body_len != 1 else ''} in context){RESET}")
                continue
            if not _confirm(f"compress {body_len - COMPRESS_KEEP} older turns (keep last {COMPRESS_KEEP})"):
                print(f"{DIM}  cancelled{RESET}")
                continue
            print(f"{DIM}  compressing…{RESET}")
            result = compress(messages)
            if result is None:
                continue  # error already printed
            kept_n, summarized_n, summary_chars = result
            print(f"{DIM}  └ compressed {summarized_n} turns → 1 summary "
                  f"({summary_chars} chars), kept last {kept_n} verbatim{RESET}")
            continue
        print()  # breathing room before the spinner/text starts
        messages.append({"role": "user", "content": user})
        steps = 0
        while model_turn(messages) and steps < 25:  # cap runaway tool loops
            steps += 1


if __name__ == "__main__":
    main()
