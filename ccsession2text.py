#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["typer"]
# ///
"""ccsession2text.py - Convert a Claude Code session export into condensed Markdown.

Keeps only conversation, tool calls, and code changes; drops images, hook logs,
skill listings, and other noise. The output is meant to be pasted straight into
a new session as context.

    ./ccsession2text.py <export dir|.jsonl|.zip> [-o out.md] [--level balanced] [--thinking] [--stats]

When `--out` is omitted, the generated Markdown is written beside the input
export. For example, running
`ccsession2text.py /Users/me/Downloads/session-export.zip` writes
`/Users/me/Downloads/session-export.md`.

Just run it directly (the shebang uses `uv run --script`, which pulls in typer
from the inline dependency block above automatically - no need to pip install
ahead of time or rely on the current venv having it). You can also run it
explicitly with `uv run ccsession2text.py ...`.

Design note: the export format will change over time, so everything dispatches
through registries, and unknown record types / content blocks / tool names all
have a fallback path - it never crashes. `--stats` reports anything it doesn't
recognize so you know the format moved.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import typer

BIG = 10**9

# ─────────────────────────── condensation levels ───────────────────────────


@dataclass
class Level:
    name: str
    cmd_head: int = 20        # lines of the Bash command itself to keep
    bash_head: int = 10
    bash_tail: int = 4
    res_head: int = 12
    res_tail: int = 0
    res_chars: int = 1200     # char cap for a single tool result
    code_chars: int = 2500    # char cap for Write/Edit body
    write_head: int = 35
    read_body: bool = False
    plan_head: int = 60
    prompt_head: int = 15
    val_chars: int = 300
    thinking_chars: int = 1200
    list_head: int = 12
    keep_unknown_records: bool = False


LEVELS = {
    "minimal": Level(
        "minimal", cmd_head=8, bash_head=5, bash_tail=0, res_head=5, res_tail=0,
        res_chars=400, code_chars=600, write_head=0, read_body=False, plan_head=20,
        prompt_head=8, val_chars=120, thinking_chars=400, list_head=8,
    ),
    "balanced": Level("balanced"),
    "full": Level(
        "full", cmd_head=BIG, bash_head=BIG, bash_tail=0, res_head=BIG, res_tail=0,
        res_chars=BIG, code_chars=BIG, write_head=BIG, read_body=True, plan_head=BIG,
        prompt_head=BIG, val_chars=BIG, thinking_chars=BIG, list_head=BIG,
        keep_unknown_records=True,
    ),
}

# ─────────────────────────── record routing ───────────────────────────

# top-level types that get rendered
RENDERED_RECORDS = {"user", "assistant", "attachment"}
# top-level types explicitly dropped (session-level metadata / content that
# already appears elsewhere)
DROPPED_RECORDS = {
    "system",          # hook execution summary
    "mode",            # permission mode switch
    "custom-title",    # session title (already in metadata.json)
    "last-prompt",     # copy of the last prompt
    "queue-operation", # queued prompts always reappear as a real user message later
    "summary",         # context-compaction summary
    "file-history-snapshot",
}
KNOWN_RECORDS = RENDERED_RECORDS | DROPPED_RECORDS

# attachment.type: keep only these two, everything else is noise
KEPT_ATTACHMENTS = {"edited_text_file", "plan_mode_exit"}

# ─────────────────────────── helpers ───────────────────────────

SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.S)
INTERRUPT = re.compile(r"^\[Request interrupted")

EXT_LANG = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx",
    ".jsx": "jsx", ".css": "css", ".scss": "scss", ".html": "html", ".astro": "astro",
    ".json": "json", ".sh": "bash", ".fish": "bash", ".md": "markdown", ".yml": "yaml",
    ".yaml": "yaml", ".toml": "toml", ".rs": "rust", ".go": "go", ".sql": "sql",
}


def fence(text: str, lang: str = "") -> list[str]:
    """Fence length adapts to the longest backtick run in the content, so nested
    code blocks never break out."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    bar = "`" * max(3, longest + 1)
    return [bar + lang, text.rstrip("\n"), bar]


def head_tail(text: str, head: int, tail: int = 0, max_chars: int = BIG) -> str:
    if not text:
        return ""
    lines = text.rstrip("\n").split("\n")
    if head <= 0 and tail <= 0:
        return f"... ({len(lines)} lines omitted) ..."
    if len(lines) > head + tail:
        omitted = len(lines) - head - tail
        kept = lines[:head] + [f"... ({omitted} lines omitted) ..."] + (lines[-tail:] if tail else [])
        text = "\n".join(kept)
    else:
        text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... ({len(text) - max_chars} more chars omitted) ..."
    return text


def clip(s: str, n: int) -> str:
    s = str(s).replace("\n", "|")
    return s if len(s) <= n else s[:n] + f"...(+{len(s) - n} chars)"


def est_tokens(s: str) -> int:
    # CJK Unified Ideographs block (U+4E00-U+9FFF): count these chars as
    # ~1 token each, everything else at ~3.8 chars/token.
    cjk = sum(1 for ch in s if chr(0x4E00) <= ch <= chr(0x9FFF))
    return int(cjk + (len(s) - cjk) / 3.8)


def fmt_time(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return ts


def lang_for(path: str) -> str:
    return EXT_LANG.get(Path(path or "").suffix.lower(), "")


# ─────────────────────────── data model ───────────────────────────


@dataclass
class ToolResult:
    text: str = ""
    images: int = 0
    is_error: bool = False
    structured: object = None
    missing: bool = True


@dataclass
class Ctx:
    level: Level
    cwd: str = ""
    thinking: bool = False
    unknown_records: Counter = field(default_factory=Counter)
    unknown_tools: Counter = field(default_factory=Counter)
    unknown_blocks: Counter = field(default_factory=Counter)
    tool_calls: Counter = field(default_factory=Counter)

    def short(self, p: str | None) -> str:
        if not p:
            return "?"
        if self.cwd and p.startswith(self.cwd + "/"):
            return p[len(self.cwd) + 1:]
        home = str(Path.home())
        return "~" + p[len(home):] if p.startswith(home) else p


# ─────────────────────────── tool renderers ───────────────────────────
# signature: (ctx, name, inp: dict, res: ToolResult) -> list[str]
# to add a new tool: write a function and register it with @tool(...) or
# @prefix_tool(...) below.

TOOL_RENDERERS: dict[str, callable] = {}
TOOL_PREFIX_RENDERERS: list[tuple[str, callable]] = []


def tool(*names):
    def deco(fn):
        for n in names:
            TOOL_RENDERERS[n] = fn
        return fn
    return deco


def prefix_tool(prefix):
    def deco(fn):
        TOOL_PREFIX_RENDERERS.append((prefix, fn))
        return fn
    return deco


def result_block(ctx: Ctx, res: ToolResult, head: int | None = None,
                 tail: int | None = None, lang: str = "") -> list[str]:
    lv = ctx.level
    head = lv.res_head if head is None else head
    tail = lv.res_tail if tail is None else tail
    out: list[str] = []
    if res.missing:
        return ["-> *(no result: interrupted or never returned)*"]
    body = head_tail(res.text, head, tail, lv.res_chars)
    if res.is_error:
        out.append("-> :warning: **error**")
    if body.strip():
        out += fence(body, lang)
    elif not res.is_error:
        out.append("-> *(empty output)*")
    if res.images:
        out.append(f"-> *({res.images} image(s) omitted)*")
    return out


@tool("Bash", "BashOutput")
def r_bash(ctx, name, inp, res):
    lv = ctx.level
    desc = inp.get("description") or ""
    head = "**Bash**" + (f" - {desc}" if desc else "")
    if inp.get("run_in_background"):
        head += " *(background)*"
    cmd = head_tail(str(inp.get("command", "")), lv.cmd_head, 0, lv.code_chars)
    out = [head] + fence(cmd, "bash")
    return out + result_block(ctx, res, lv.bash_head, lv.bash_tail)


@tool("Read")
def r_read(ctx, name, inp, res):
    path = ctx.short(inp.get("file_path"))
    nlines = res.text.count("\n") + 1 if res.text else 0
    line = f"**Read** `{path}`" + (f" ({nlines} lines)" if nlines else "")
    if not ctx.level.read_body:
        return [line]
    return [line] + result_block(ctx, res, lang=lang_for(inp.get("file_path", "")))


@tool("Write")
def r_write(ctx, name, inp, res):
    path = inp.get("file_path", "")
    content = inp.get("content", "") or ""
    nlines = content.count("\n") + 1
    kind = "created" if (isinstance(res.structured, dict) and res.structured.get("type") == "create") else "written"
    out = [f"**Write** `{ctx.short(path)}` - {kind}, {nlines} lines"]
    if ctx.level.write_head > 0:
        out += fence(head_tail(content, ctx.level.write_head, 0, ctx.level.code_chars), lang_for(path))
    if res.is_error:
        out += result_block(ctx, res, 5, 0)
    return out


def unified_diff(patch: list) -> str:
    chunks = []
    for h in patch or []:
        if not isinstance(h, dict):
            continue
        chunks.append(
            f"@@ -{h.get('oldStart', 0)},{h.get('oldLines', 0)} "
            f"+{h.get('newStart', 0)},{h.get('newLines', 0)} @@"
        )
        chunks += [str(x) for x in h.get("lines", [])]
    return "\n".join(chunks)


@tool("Edit", "MultiEdit", "NotebookEdit")
def r_edit(ctx, name, inp, res):
    path = inp.get("file_path") or inp.get("notebook_path") or ""
    out = [f"**{name}** `{ctx.short(path)}`"]
    patch = res.structured.get("structuredPatch") if isinstance(res.structured, dict) else None
    diff = unified_diff(patch) if patch else ""
    if diff:
        diff_head = ctx.level.write_head * 4 if ctx.level.write_head else 20
        out += fence(head_tail(diff, diff_head, 0, ctx.level.code_chars), "diff")
    else:
        # no structuredPatch (older format / failed edit) - fall back to old/new text
        edits = inp.get("edits") or [inp]
        for e in edits:
            old, new = e.get("old_string", ""), e.get("new_string", "")
            if old:
                out += fence(head_tail(old, 12, 0, 800), "")
                out.append("v")
            out += fence(head_tail(new, 20, 0, 1200), lang_for(path))
    if res.is_error:
        out += result_block(ctx, res, 5, 0)
    return out


@tool("Glob", "Grep")
def r_search(ctx, name, inp, res):
    args = " ".join(
        f"{k}={clip(v, 80)}" for k, v in inp.items()
        if k in ("pattern", "path", "glob", "type", "output_mode", "-n", "-i", "-A", "-B", "-C")
    )
    nlines = res.text.count("\n") + 1 if res.text else 0
    out = [f"**{name}** {args} -> {nlines} result lines"]
    return out + result_block(ctx, res, ctx.level.list_head, 0)


@tool("Task", "Agent")
def r_task(ctx, name, inp, res):
    out = [f"**{name}** (subagent: {inp.get('subagent_type', 'general')}) - {inp.get('description', '')}"]
    p = inp.get("prompt", "")
    if p:
        out += fence(head_tail(p, ctx.level.prompt_head, 0, ctx.level.res_chars))
    return out + result_block(ctx, res, ctx.level.res_head + 10, 0)


@tool("TodoWrite")
def r_todo(ctx, name, inp, res):
    todos = inp.get("todos") or []
    done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
    line = f"**TodoWrite** - {len(todos)} items ({done} completed)"
    if ctx.level.name != "full":
        return [line]
    items = [f"- [{t.get('status', '?')}] {t.get('content', '')}" for t in todos if isinstance(t, dict)]
    return [line] + items


@tool("AskUserQuestion")
def r_ask(ctx, name, inp, res):
    out = ["**AskUserQuestion** - user's choice:"]
    answers = res.structured.get("answers") if isinstance(res.structured, dict) else None
    if isinstance(answers, dict) and answers:
        for q, a in answers.items():
            out.append(f"- Q: {clip(q, 200)}")
            out.append(f"  - **A: {clip(a, ctx.level.val_chars * 2)}**")
    else:
        for q in inp.get("questions") or []:
            if isinstance(q, dict):
                out.append(f"- Q: {clip(q.get('question', ''), 200)}")
        out += result_block(ctx, res, 10, 0)
    return out


@tool("ExitPlanMode", "EnterPlanMode")
def r_plan(ctx, name, inp, res):
    out = [f"**{name}**"]
    plan = inp.get("plan") or ""
    if plan:
        out += fence(head_tail(plan, ctx.level.plan_head, 0, ctx.level.res_chars * 2), "markdown")
    if res.is_error or "doesn't want to proceed" in res.text or "rejected" in res.text:
        # the user's own words on rejection often carry key requirement updates - keep in full
        out.append("-> **user did not approve**, feedback:")
        out += fence(head_tail(res.text, 40, 0, 3000))
    else:
        out.append("-> user approved the plan")
    return out


@tool("ToolSearch")
def r_toolsearch(ctx, name, inp, res):
    return [f"**ToolSearch** `{clip(inp.get('query', ''), 160)}`"]


@tool("WebFetch", "WebSearch")
def r_web(ctx, name, inp, res):
    target = inp.get("url") or inp.get("query") or ""
    out = [f"**{name}** {clip(target, 200)}"]
    return out + result_block(ctx, res, ctx.level.res_head, 0)


@prefix_tool("mcp__Claude_Browser__")
@prefix_tool("mcp__claude-in-chrome__")
def r_browser(ctx, name, inp, res):
    """Browser actions carry almost no signal for the next agent - collapse to one line."""
    short = name.split("__")[-1]
    args = ", ".join(f"{k}={clip(v, 60)}" for k, v in inp.items() if k != "tabId")
    line = f"**Browser.{short}**({clip(args, 200)})"
    if res.is_error:
        line += f" -> :warning: {clip(res.text, 160)}"
    elif res.images:
        line += f" -> *(screenshot x{res.images}, omitted)*"
    elif ctx.level.name == "full" and res.text.strip():
        return [line] + result_block(ctx, res)
    return [line]


@prefix_tool("mcp__code-review-graph__")
def r_graph(ctx, name, inp, res):
    short = name.split("__")[-1]
    args = ", ".join(f"{k}={clip(v, 80)}" for k, v in inp.items())
    return [f"**{short}**({clip(args, 240)})"] + result_block(ctx, res, ctx.level.res_head, 0)


def r_generic(ctx, name, inp, res):
    """Fallback for any tool without a dedicated renderer - works as long as
    input is a dict."""
    out = [f"**{name}**"]
    if isinstance(inp, dict) and inp:
        for k, v in inp.items():
            v = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            out.append(f"- {k}: {clip(v, ctx.level.val_chars)}")
    elif inp:
        out.append(f"- {clip(json.dumps(inp, ensure_ascii=False), ctx.level.val_chars)}")
    return out + result_block(ctx, res)


def render_tool(ctx: Ctx, name: str, inp, res: ToolResult) -> list[str]:
    if not isinstance(inp, dict):
        inp = {"input": inp}
    fn = TOOL_RENDERERS.get(name)
    if fn is None:
        for prefix, pfn in TOOL_PREFIX_RENDERERS:
            if name.startswith(prefix):
                fn = pfn
                break
    if fn is None:
        ctx.unknown_tools[name] += 1
        fn = r_generic
    try:
        return fn(ctx, name, inp, res)
    except Exception as exc:  # a single renderer failing shouldn't nuke the whole output
        return [f"**{name}** *(render failed: {exc})*"] + r_generic(ctx, name, inp, res)[1:]


# ─────────────────────────── input loading ───────────────────────────


def load_records(path: Path, stats: Counter) -> tuple[list[dict], dict, str]:
    """Returns (records, metadata, source filename). Accepts a directory, a
    .jsonl file, or a .zip."""
    tmp = None
    if path.is_file() and path.suffix == ".zip":
        tmp = Path(tempfile.mkdtemp(prefix="ccsession2text-"))
        with zipfile.ZipFile(path) as z:
            z.extractall(tmp)
        path = tmp

    meta: dict = {}
    if path.is_dir():
        candidates = sorted(path.rglob("*.jsonl"))
        if not candidates:
            sys.exit(f"Error: no .jsonl file found under {path}")
        best, best_n = None, -1
        for c in candidates:
            n = sum(1 for line in c.open(encoding="utf-8", errors="replace") if line.strip())
            if n > best_n:
                best, best_n = c, n
        src = best
        for m in path.rglob("metadata.json"):
            try:
                meta = json.loads(m.read_text(encoding="utf-8"))
            except Exception:
                pass
            break
    else:
        src = path

    records = []
    for i, line in enumerate(src.open(encoding="utf-8", errors="replace"), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            stats["bad_lines"] += 1
    return records, meta, src.name


# ─────────────────────────── indexing & summary ───────────────────────────


def extract_result(rec: dict, block: dict) -> ToolResult:
    res = ToolResult(missing=False)
    res.structured = rec.get("toolUseResult")
    res.is_error = bool(block.get("is_error"))
    content = block.get("content")
    if isinstance(content, str):
        res.text = content
    elif isinstance(content, list):
        parts = []
        for c in content:
            if not isinstance(c, dict):
                parts.append(str(c))
                continue
            t = c.get("type")
            if t == "text":
                parts.append(c.get("text", ""))
            elif t == "image":
                res.images += 1          # base64 stops here, never makes it into output
            elif t == "tool_reference":
                parts.append(f"[tool_reference: {c.get('name', '')}]")
            else:
                parts.append(f"[{t}]")
        res.text = "\n".join(p for p in parts if p)
    elif content is not None:
        res.text = json.dumps(content, ensure_ascii=False)
    if not res.text and isinstance(res.structured, str):
        res.text = res.structured
    if isinstance(res.structured, str) and "doesn't want to proceed" in res.structured:
        res.is_error = True
    return res


def index_session(records: list[dict], ctx: Ctx):
    """Single pre-scan: tool-result index + file-change summary."""
    results: dict[str, ToolResult] = {}
    for rec in records:
        msg = rec.get("message")
        if rec.get("type") != "user" or not isinstance(msg, dict):
            continue
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                if tid:
                    results[tid] = extract_result(rec, block)

    changes: dict[str, dict] = {}
    for rec in records:
        msg = rec.get("message")
        if rec.get("type") != "assistant" or not isinstance(msg, dict):
            continue
        for block in msg.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name, inp = block.get("name", ""), block.get("input") or {}
            if name not in ("Write", "Edit", "MultiEdit", "NotebookEdit") or not isinstance(inp, dict):
                continue
            path = inp.get("file_path") or inp.get("notebook_path")
            if not path:
                continue
            res = results.get(block.get("id"), ToolResult())
            if res.is_error:
                continue
            e = changes.setdefault(path, {"writes": 0, "edits": 0, "created": False, "lines": 0})
            if name == "Write":
                e["writes"] += 1
                e["lines"] = (inp.get("content") or "").count("\n") + 1
                if isinstance(res.structured, dict) and res.structured.get("type") == "create":
                    e["created"] = True
            else:
                e["edits"] += 1
    return results, changes


# ─────────────────────────── main render pass ───────────────────────────


def render(records: list[dict], meta: dict, ctx: Ctx, source: str, stats: Counter) -> str:
    results, changes = index_session(records, ctx)

    cwds = [r.get("cwd") for r in records if r.get("cwd")]
    ctx.cwd = meta.get("cwd") or (cwds[0] if cwds else "")
    times = sorted(r["timestamp"] for r in records if r.get("timestamp"))
    branches = {r.get("gitBranch") for r in records if r.get("gitBranch")}

    body: list[str] = []
    turn = 0
    speaker = None
    n_tools = 0

    def flush(lines: list[str]):
        body.append("")
        body.extend(lines)

    for rec in records:
        rtype = rec.get("type")
        stats[f"rec:{rtype}"] += 1
        if rtype not in KNOWN_RECORDS:
            ctx.unknown_records[rtype] += 1
            if ctx.level.keep_unknown_records:
                flush([f"*(unknown record type `{rtype}`)*"])
            continue
        if rtype in DROPPED_RECORDS:
            continue

        if rtype == "attachment":
            att = rec.get("attachment") or {}
            atype = att.get("type")
            stats[f"att:{atype}"] += 1
            if atype not in KEPT_ATTACHMENTS:
                continue
            if atype == "edited_text_file":
                flush([f"> :memo: *user hand-edited* `{ctx.short(att.get('filename'))}` *in an editor*"])
            else:
                flush(["> *(exited plan mode)*"])
            continue

        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        side = "  |> " if rec.get("isSidechain") else ""

        if rtype == "user":
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
            texts, images, only_results = [], 0, True
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    only_results = False
                    t = SYSTEM_REMINDER.sub("", b.get("text", "")).strip()
                    if t:
                        texts.append(t)
                elif bt == "image":
                    only_results = False
                    images += 1
                elif bt != "tool_result":
                    only_results = False
                    ctx.unknown_blocks[f"user:{bt}"] += 1
            if only_results:
                continue  # pure tool result, already rendered at the tool-call site
            joined = "\n\n".join(texts)
            if joined and INTERRUPT.match(joined):
                flush(["*(user interrupted execution)*"])
                speaker = "user"
                continue
            turn += 1
            speaker = "user"
            head = f"### [{turn}] {side}User - {fmt_time(rec.get('timestamp'))}"
            out = ["", "---", "", head, ""]
            if joined:
                out.append(joined)
            if images:
                out.append(f"\n*({images} image(s) attached, omitted)*")
            body.extend(out)
            continue

        # assistant
        for b in content or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            stats[f"block:{bt}"] += 1
            if bt == "text":
                t = (b.get("text") or "").strip()
                if not t:
                    continue
                if speaker != "assistant":
                    flush([f"**{side}Assistant**", ""])
                    speaker = "assistant"
                flush([t])
            elif bt == "thinking":
                if not ctx.thinking:
                    continue
                t = (b.get("thinking") or "").strip()
                if t:
                    flush(["<details><summary>thinking</summary>", "",
                           clip(head_tail(t, BIG, 0, ctx.level.thinking_chars), BIG).replace("|", "\n"),
                           "", "</details>"])
            elif bt == "tool_use":
                name = b.get("name", "?")
                n_tools += 1
                ctx.tool_calls[name] += 1
                if speaker != "assistant":
                    flush([f"**{side}Assistant**", ""])
                    speaker = "assistant"
                res = results.get(b.get("id"), ToolResult())
                flush(render_tool(ctx, name, b.get("input") or {}, res))
            else:
                ctx.unknown_blocks[f"assistant:{bt}"] += 1

    # ── header ──
    title = meta.get("title") or "Claude Code session"
    header = [f"# Session: {title}", ""]
    facts = []
    if ctx.cwd:
        facts.append(f"cwd `{ctx.cwd}`")
    if branches:
        facts.append("branch " + "/".join(sorted(b for b in branches if b)))
    if meta.get("model"):
        facts.append(f"model {meta['model']}")
    header.append(" - ".join(facts))
    span = f"{fmt_time(times[0])} -> {fmt_time(times[-1])}" if times else ""
    header.append(f"{span} - {turn} user turns - {n_tools} tool calls")
    header.append(f"*source `{source}` - level={ctx.level.name} - thinking={'on' if ctx.thinking else 'off'} "
                  f"- all images omitted*")

    if changes:
        header += ["", "## File change summary", ""]
        for path, e in sorted(changes.items()):
            bits = []
            if e["created"]:
                bits.append(f"created, {e['lines']} lines")
            elif e["writes"]:
                bits.append(f"overwritten x{e['writes']} ({e['lines']} lines)")
            if e["edits"]:
                bits.append(f"{e['edits']} edit(s)")
            header.append(f"- `{ctx.short(path)}` - {', '.join(bits)}")

    header += ["", "## Conversation"]
    return "\n".join(header + body).replace("\n\n\n", "\n\n") + "\n"


# ─────────────────────────── CLI ───────────────────────────

LevelName = Enum("LevelName", {k: k for k in LEVELS}, type=str)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Convert a Claude Code session export into condensed Markdown.",
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.command()
def main(
    path: Path = typer.Argument(..., exists=True, help="Export directory / .jsonl file / .zip"),
    out: Optional[str] = typer.Option(
        None, "-o", "--out",
        help="Output path (default: beside input as <input name>.md; `-` for stdout)",
    ),
    level: LevelName = typer.Option(
        LevelName.balanced.value, "--level", help="Condensation level", case_sensitive=False
    ),
    thinking: bool = typer.Option(False, "--thinking", help="Keep thinking blocks (dropped by default)"),
    stats_flag: bool = typer.Option(False, "--stats", help="Print stats and any unrecognized types"),
):
    stats = Counter()
    records, meta, source = load_records(path, stats)
    ctx = Ctx(level=LEVELS[level.value], thinking=thinking)
    text = render(records, meta, ctx, source, stats)

    if out == "-":
        sys.stdout.write(text)
    else:
        base = path.name.removesuffix(".zip").removesuffix(".jsonl")
        out_path = Path(out) if out else path.resolve().parent / f"{base}.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        typer.echo(f"-> {out_path}  ({len(text):,} chars, ~{est_tokens(text):,} tokens)", err=True)

    if stats_flag:
        raw = sum(len(json.dumps(r, ensure_ascii=False)) for r in records)
        p = lambda *a: typer.echo(" ".join(str(x) for x in a), err=True)
        p("\n-- stats --------------------------")
        p(f"{len(records)} records, {raw:,} raw chars -> {len(text):,} output chars "
          f"({len(text) / max(raw, 1):.1%} of original, ~{est_tokens(text):,} tokens)")
        if stats["bad_lines"]:
            p(f":warning: {stats['bad_lines']} lines failed to parse as JSON (skipped)")
        p("\nrecord types: " + ", ".join(f"{k[4:]}={v}" for k, v in sorted(stats.items()) if k.startswith("rec:")))
        p("content blocks: " + ", ".join(f"{k[6:]}={v}" for k, v in sorted(stats.items()) if k.startswith("block:")))
        p("attachments: " + ", ".join(f"{k[4:]}={v}" for k, v in sorted(stats.items()) if k.startswith("att:")))
        p("\ntool calls:")
        for name, n in ctx.tool_calls.most_common():
            p(f"  {n:4d}  {name}")
        unknown = [("record type", ctx.unknown_records), ("tool", ctx.unknown_tools), ("content block", ctx.unknown_blocks)]
        found = [(label, c) for label, c in unknown if c]
        if found:
            p("\n:warning: unrecognized (format may have changed - consider adding a renderer):")
            for label, c in found:
                for k, v in c.most_common():
                    p(f"  {label}: {k} x{v}")
        else:
            p("\n:white_check_mark: no unrecognized types")


if __name__ == "__main__":
    app()
